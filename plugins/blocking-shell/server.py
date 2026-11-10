# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1", "tree-sitter>=0.25,<0.26", "tree-sitter-bash>=0.25,<0.26"]
# ///
"""Persistent Bash execution with blocking replies and durable results."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import time
from typing import Annotated

import anyio
import tiktoken
from pydantic import Field
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from scratch_space import ScratchSpace
from session_state import SessionState
from shell_session import ShellSession, command_source, counter, metrics, snapshot

DEFAULT_WORKDIR = Path(os.environ.get('CODEX_WORK', Path.home() / 'codex-work'))
sessions: dict[str, ShellSession] = {}
terminating = False


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*(shell.close() for shell in sessions.values()))
        if terminating:
            # The SDK's stdin reader can be blocked in a worker thread. All
            # requests and shells are finalized here; do not wait for more stdin.
            os._exit(128 + signal.SIGTERM)


mcp = FastMCP("blocking-shell", lifespan=lifespan)
encoding = tiktoken.get_encoding("o200k_base")
max_token_bytes = max(map(len, encoding.token_byte_values()))


def session_state(ctx: Context) -> SessionState:
    meta = ctx.request_context.meta
    values = meta.model_dump() if meta else {}
    return SessionState(values.get('threadId', os.environ.get('CODEX_THREAD_ID')))


def output_tail(log: Path, budget: int) -> tuple[str, int, bool]:
    size = log.stat().st_size
    if budget == 0:
        return "", 0, size > 0
    # No budget-sized token sequence can exceed this vocabulary-derived bound.
    # Extra bytes cover a UTF-8 character crossing the read boundary.
    with log.open("rb") as reader:
        start = max(0, size - budget * max_token_bytes - 3)
        reader.seek(start)
        tail = reader.read().decode("utf-8", errors="replace")
    tokens = encoding.encode_ordinary(tail)
    truncated = start > 0 or len(tokens) > budget
    if len(tokens) > budget:
        tail = encoding.decode(tokens[-budget:], errors="ignore")
        tokens = encoding.encode_ordinary(tail)
        # Decoding a partial character can change tokenization at the boundary.
        while len(tokens) > budget:
            tail = tail[1:]
            tokens = encoding.encode_ordinary(tail)
    return tail, len(tokens), truncated



def summarize(data: dict) -> dict:
    keys = ('run_ref', 'state', 'status', 'exit_code', 'elapsed_seconds',
            'cpu_seconds', 'session_memory_peak_bytes', 'io_read_bytes', 'io_write_bytes',
            'output_tail', 'output_truncated', 'log_path', 'result_path',
            'scratch_ref', 'unit', 'pid', 'workdir')
    summary = {key: data[key] for key in keys if key in data}
    for key in ('state_lost', 'accounting_error', 'cleanup_error', 'suggestion'):
        if data.get(key):
            summary[key] = data[key]
    if data['status'] != 'completed' or data.get('state_lost'):
        summary.update(unit_result=data.get('unit_result'),
                       systemd_log_path=data.get('systemd_log_path'))
    return summary


def work_directory(value: str | None, scratch: Path) -> Path | None:
    if value is None:
        return None
    for variable in ('$SCRATCH_DIR', '${SCRATCH_DIR}'):
        if value == variable or value.startswith(variable + '/'):
            value = str(scratch) + value[len(variable):]
            break
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('workdir must be an existing absolute directory after scratch expansion')
    return path


def default_scratch(shell: ShellSession, state: SessionState) -> str:
    space = ScratchSpace(session=state.session)
    if shell.scratch_ref:
        try:
            with space.lease(shell.scratch_ref):
                return shell.scratch_ref
        except (ValueError, FileNotFoundError):
            pass
    reference = space.create()['scratch_ref']
    shell.scratch_ref = reference
    return reference


@mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
async def run(
    cmd: Annotated[str, Field(description="Bash code; variables, exports, functions and cd persist.")],
    ctx: Context,
    workdir: Annotated[str | None, Field(
        description="Optional cd before execution; absolute path or $SCRATCH_DIR[/subdir].")] = None,
    scratch_ref: Annotated[str | None, Field(
        description="Scratch storage for logs; omitted selects reusable automatic storage.")] = None,
    max_output_tokens: Annotated[int, Field(ge=0, strict=True,
        description="Returned log tail token limit, default 1000; full output is saved.")] = 1000,
    timeout_seconds: Annotated[int, Field(ge=1, le=86400, strict=True,
        description="Foreground execution deadline; default six hours.")] = 21600,
    memory_max_mib: Annotated[int | None, Field(ge=1, strict=True,
        description="Persistent shell and descendants' limit; initially 8192 MiB, then retained.")] = None,
) -> dict:
    """Run Bash and wait for completion. Usually pass only cmd.

    One noninteractive Bash per conversation; calls serialize. Startup files and
    BASH_ENV run once. Use export, source, cd, unset, declare -p and declare -f
    directly. Default cwd is CODEX_WORK or ~/codex-work. No stdin or PTY.
    Returns bounded output, saved logs and CPU/IO deltas; memory peak covers the
    whole shell session. Timeout/cancellation/OOM/exit lose state: call reset.
    Server restart loses shell state; compaction retains it while the server lives.
    Use result after lost responses, Scratch delete when logs are no longer needed.
    """
    if '\0' in cmd:
        raise ValueError('cmd must not contain NUL')
    state = session_state(ctx)
    shell = sessions.setdefault(state.session, ShellSession(DEFAULT_WORKDIR))
    async with shell.lock:
        if shell.lost:
            raise ValueError(f'Bash state lost ({shell.lost}); call reset before run')
        ref = scratch_ref or default_scratch(shell, state)
        with ScratchSpace().lease(ref) as logs:
            work = work_directory(workdir, logs)
            if not shell.unit and work:
                shell.workdir = work
            if not shell.unit and memory_max_mib is not None:
                shell.memory_max_mib = memory_max_mib
            try:
                await shell.start()
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await shell.close()
                raise
            before = await shell.limits(memory_max_mib)
            unit = shell.unit
            assert unit is not None
            started = time.monotonic()
            arguments = dict(cmd=cmd, workdir=workdir, scratch_ref=ref,
                             max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
                             memory_max_mib=shell.memory_max_mib)
            out = state.accept(arguments, logs, dict(scratch_ref=ref, unit=shell.unit,
                                                    started=started, before=before))
            with (out / 'active.lock').open('wb') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX)
                log = out / 'output.log'
                log.touch()
                (out / 'command.bash').write_text(command_source(cmd, work, logs))
                data: dict = dict(run_ref=out.name.removeprefix('blocking-shell-'),
                            state='finished', scratch_ref=ref, unit=shell.unit,
                            log_path=str(log), result_path=str(out / 'result.json'),
                            systemd_log_path=str(out / 'systemd.log'), arguments=arguments)
                status, exit_code, lost = 'completed', None, None
                try:
                    await shell.arm_deadline(timeout_seconds)
                    async with asyncio.timeout(timeout_seconds):
                        exit_code, cwd = await shell.execute(out)
                    shell.workdir = Path(cwd)
                    if exit_code:
                        status = 'failed'
                except TimeoutError:
                    status, lost = 'timeout', 'execution deadline exceeded'
                except (EOFError, ConnectionError, asyncio.IncompleteReadError) as error:
                    status, lost = 'failed', str(error) or 'Bash exited'
                except asyncio.CancelledError:
                    status, lost = 'cancelled', 'execution cancelled'
                    raise
                except Exception:
                    status, lost = 'failed', 'execution control failed'
                    raise
                finally:
                    with anyio.CancelScope(shield=True):
                        after, accounting_error = await snapshot(shell.env, unit)
                        if lost and status == 'failed':
                            code = counter(after, 'ExecMainStatus')
                            if after.get('ExecMainCode') in ('2', '3') and code is not None:
                                code = -code
                            exit_code = code if after.get('ExecMainCode') != '0' else None
                            if after.get('Result') == 'timeout':
                                status = 'timeout'
                        cleanup_error = None
                        if not lost:
                            cleanup_error = await shell.idle()
                            if cleanup_error:
                                lost = 'could not clear execution deadline'
                        if lost:
                            assert shell.runtime is not None
                            shell.lost = lost
                            (out / 'systemd.log').write_text(
                                (shell.runtime / 'systemd.log').read_text() +
                                (shell.runtime / 'startup.log').read_text() +
                                json.dumps(after, indent=2))
                            cleanup_error = await shell.close() or cleanup_error
                        tail, count, truncated = output_tail(log, max_output_tokens)
                        data.update(status=status, exit_code=exit_code, state_lost=lost,
                                    elapsed_seconds=round(time.monotonic() - started, 3),
                                    **metrics(before, after), output_tail=tail,
                                    output_tokens=count, output_truncated=truncated,
                                    workdir=str(shell.workdir), accounting_error=accounting_error,
                                    cleanup_error=cleanup_error, unit_result=after.get('Result'),
                                    properties=after, timeout_seconds=timeout_seconds,
                                    memory_max_bytes=counter(after, 'MemoryMax'),
                                    memory_swap_max_bytes=counter(after, 'MemorySwapMax'),
                                    control_group=after.get('ControlGroup'),
                                    output_bytes=log.stat().st_size, output_token_encoding='o200k_base')
                        data['suggestion'] = state.suggestion(cmd, str(work or shell.workdir), 'bash')
                        pending = out / 'result.pending'
                        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2))
                        pending.replace(out / 'result.json')
                return summarize(data)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def result(ctx: Context, run_ref: str | None = None) -> dict:
    """Recover a result without rerunning; omit run_ref for the last accepted call.

    Finished results match run. Otherwise state is running, unknown or expired.
    History survives server restart; missing logs or ownership never imply success.
    Use after transport failure, not for routine polling.
    """
    record = session_state(ctx).execution(run_ref)
    reference = record['run_ref']
    try:
        with ScratchSpace().lease(record['scratch_ref']) as logs:
            out = logs / ('blocking-shell-' + reference)
            saved = out / 'result.json'
            if not out.is_dir():
                return dict(run_ref=reference, state='expired')
            if saved.exists():
                return summarize(json.loads(saved.read_text()))
            with (out / 'active.lock').open('rb') as owner:
                try:
                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active = True
                else:
                    active = False
            if not active:
                return dict(run_ref=reference, state='unknown',
                            reason='No saved result and no active request owner')
            after, error = await snapshot(dict(os.environ), record['unit'])
            if saved.exists():
                return summarize(json.loads(saved.read_text()))
            tail, _, truncated = output_tail(out / 'output.log',
                record.get('arguments', {}).get('max_output_tokens', 1000))
            data = dict(run_ref=reference, state='running', unit=record['unit'],
                        **metrics(record.get('before', {}), after),
                        elapsed_seconds=round(time.monotonic() - record['started'], 3)
                            if record.get('started') is not None else None,
                        output_tail=tail, output_truncated=truncated,
                        log_path=str(out / 'output.log'), result_path=str(saved))
            if error:
                data['accounting_error'] = error
            return data
    except (ValueError, FileNotFoundError):
        return dict(run_ref=reference, state='expired')


@mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
async def rerun(ctx: Context, run_ref: str | None = None, scratch_ref: str | None = None) -> dict:
    """Repeat the last or selected command in current Bash state, with fresh logs.

    Saved cmd and run settings survive server restart. Repeats side effects.
    scratch_ref can replace deleted storage. Invalid requests do not replace history.
    """
    state = session_state(ctx)
    arguments = state.command() if run_ref is None else state.execution(run_ref).get('arguments')
    if arguments is None:
        raise ValueError('This execution has no saved run settings; cannot rerun by ID')
    # Retained histories may predate the Bash-only API.
    arguments = {key: value for key, value in arguments.items()
                 if key not in ('shell', 'login')}
    if scratch_ref is not None:
        arguments['scratch_ref'] = scratch_ref
    return await run(ctx=ctx, **arguments)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
async def reset(
    ctx: Context,
    workdir: str | None = None,
    login: bool | None = None,
    memory_max_mib: Annotated[int | None, Field(ge=1, strict=True)] = None,
) -> dict:
    """Restart this conversation's Bash and stop its descendants; discard live state.

    Omitted settings retain cwd, login choice and memory limit (initially true,
    8192 MiB). workdir must be absolute. Rereads startup files and BASH_ENV.
    Invalid settings leave the shell intact. Saved results/history remain.
    """
    state = session_state(ctx)
    shell = sessions.setdefault(state.session, ShellSession(DEFAULT_WORKDIR))
    async with shell.lock:
        work = work_directory(workdir, DEFAULT_WORKDIR) if workdir is not None else shell.workdir
        assert work is not None
        if not work.is_dir():
            raise ValueError('workdir must be an existing absolute directory')
        previous, accounting_error = await snapshot(shell.env, shell.unit) if shell.unit else ({}, None)
        error = None
        with anyio.CancelScope(shield=True):
            error = await shell.close()
        if error:
            shell.lost = 'reset cleanup failed'
            raise RuntimeError(error)
        shell.workdir = work
        if login is not None:
            shell.login = login
        if memory_max_mib is not None:
            shell.memory_max_mib = memory_max_mib
        shell.lost = None
        try:
            await shell.start()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await shell.close()
            raise
        data = dict(workdir=str(shell.workdir), pid=shell.pid, unit=shell.unit,
                    login=shell.login, memory_max_mib=shell.memory_max_mib)
        if previous:
            data['previous_io_read_bytes'] = counter(previous, 'IOReadBytes')
            data['previous_io_write_bytes'] = counter(previous, 'IOWriteBytes')
        if accounting_error:
            data['accounting_error'] = accounting_error
        return data


async def serve():
    task = asyncio.current_task()
    assert task is not None

    def terminate():
        global terminating
        terminating = True
        task.cancel()

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, terminate)
    try:
        await mcp.run_stdio_async()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)
    os.chdir(DEFAULT_WORKDIR)
    Path(os.environ.get('SCRATCH_ROOT', DEFAULT_WORKDIR / 'tmp')).mkdir(
        parents=True, exist_ok=True, mode=0o700)
    asyncio.run(serve())
