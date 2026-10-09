# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1"]
# ///
"""Run foreground shell commands in one blocking MCP request."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import time
import tomllib
from typing import Annotated

import anyio
import tiktoken
from pydantic import Field
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from scratch_space import ScratchSpace
from word_ids import create_directory
from session_state import SessionState

mcp = FastMCP("blocking-shell")
encoding = tiktoken.get_encoding("o200k_base")
max_token_bytes = max(map(len, encoding.token_byte_values()))


def session_state(ctx: Context) -> SessionState:
    meta = ctx.request_context.meta
    values = meta.model_dump() if meta else {}
    return SessionState(values.get('threadId', os.environ.get('CODEX_THREAD_ID')))


def command_overrides(state: SessionState) -> dict[str, str]:
    # MCP processes do not inherit Codex's shell_environment_policy.set.
    config = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex')) / 'config.toml'
    try:
        with config.open('rb') as stream:
            settings = tomllib.load(stream)
    except FileNotFoundError:
        settings = {}
    bash_env = settings.get('shell_environment_policy', {}).get('set', {}).get('BASH_ENV')
    overrides = {}
    if bash_env is not None:
        if not isinstance(bash_env, str) or '\0' in bash_env:
            raise ValueError('shell_environment_policy.set.BASH_ENV must be a string without NUL')
        overrides['BASH_ENV'] = bash_env
    overrides.update(state.list())
    return overrides


@mcp.tool(structured_output=False, annotations=ToolAnnotations(openWorldHint=False))
def set_bashrc(script: str, ctx: Context, ref: str | None = None) -> dict:
    """Register Bash source text for this conversation, or replace it by ref.

    Omit ref to append a script and receive its ID; editing preserves its order.
    Each run/rerun sources scripts in registration order after normal Bash startup,
    before cmd. Nonzero source status stops execution. Survives reconnects and
    compaction; other conversations and running jobs are unaffected.
    """
    return {'ref': session_state(ctx).set_script(script, ref)}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_bashrc(ref: str, ctx: Context) -> dict:
    """Read a registered Bash script by ref in this conversation."""
    return session_state(ctx).scripts(ref)[0]


@mcp.tool(structured_output=False, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_bashrc(ctx: Context) -> dict:
    """List this conversation's Bash script refs in execution order, without source text."""
    return {'refs': [item['ref'] for item in session_state(ctx).scripts()]}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
def delete_bashrc(ref: str, ctx: Context) -> dict:
    """Delete a Bash script by ref from this conversation; unknown refs are errors."""
    session_state(ctx).delete_script(ref)
    return {'deleted': ref}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(openWorldHint=False))
def set_env(values: dict[str, str], ctx: Context) -> dict:
    """Set or replace environment overrides for this conversation's subsequent runs.

    Values are literal strings. Other variables stay unchanged. Survives MCP
    reconnects and compaction; applies only to blocking-shell, not other tools.
    Stored values are injected into model context after automatic compaction.
    Returns all tool-configured overrides. Running commands are unaffected.
    """
    return {'variables': session_state(ctx).update(values, [])}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
def unset_env(names: list[str], ctx: Context) -> dict:
    """Remove named overrides from this conversation; absent names are harmless.

    Subsequent runs fall back to Codex BASH_ENV configuration or inherited values.
    Does not unset the server's own environment or change running commands.
    Returns remaining overrides.
    """
    return {'variables': session_state(ctx).update({}, names)}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_env(ctx: Context) -> dict:
    """Return this conversation's tool-configured environment names and values.

    Excludes inherited variables and per-command assignments.
    """
    return {'variables': session_state(ctx).list()}


@contextmanager
def result_directory(scratch_ref: str):
    with ScratchSpace().lease(scratch_ref) as logs:
        out = create_directory(logs, prefix="blocking-shell-")
        yield out


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


async def control(environment: dict[str, str], *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=environment)
    output, _ = await proc.communicate()
    return proc.returncode or 0, output.decode(errors="replace")


def counter(properties: dict[str, str], name: str) -> int | None:
    value = properties.get(name, "")
    if not value.isdecimal() or int(value) == 2**64 - 1:
        return None
    return int(value)


def summarize(result: dict) -> dict:
    summary = {key: result[key] for key in (
        "status", "exit_code", "elapsed_seconds", "cpu_seconds", "memory_peak_bytes",
        "io_read_bytes", "io_write_bytes", "output_tail", "output_truncated",
        "log_path", "result_path")}
    for key in ("accounting_error", "cleanup_error"):
        if result[key] is not None:
            summary[key] = result[key]
    if result["status"] != "completed" or result["exit_code"] != 0 or any(
            result[key] is not None for key in ("accounting_error", "cleanup_error")):
        summary.update({key: result[key] for key in ("unit", "unit_result", "systemd_log_path")})
    return summary


@mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
async def run(
    cmd: Annotated[str, Field(
        description="Foreground command, including any per-command environment assignments.")],
    workdir: Annotated[str, Field(description="Existing absolute working directory; accepts $BLOCKING_SHELL_SCRATCH_DIR or ${BLOCKING_SHELL_SCRATCH_DIR}, optionally followed by /subdir.")],
    scratch_ref: Annotated[str, Field(description="ID from scratch.create for saved logs and results; its path is exposed as BLOCKING_SHELL_SCRATCH_DIR.")],
    ctx: Context,
    max_output_tokens: Annotated[int, Field(ge=0, strict=True,
        description="Maximum tokens in the returned log tail (default 1000), excluding metadata; full logs are saved.")] = 1000,
    shell: Annotated[str | None, Field(
        description="Shell executable; omit for the user's default shell. "
                    "Specify when cmd requires a particular shell syntax.")] = None,
    login: Annotated[bool, Field(
        description="Use -lc (login startup files). Set false for -c to skip login "
                    "startup files. Bash still reads BASH_ENV in either mode.")] = True,
    timeout_seconds: Annotated[int, Field(ge=1, le=86400, strict=True,
        description="Execution deadline in seconds, default six hours. Set when a "
                    "different deadline is needed; expiry stops the command and descendants.")] = 21600,
    memory_max_mib: Annotated[int, Field(ge=1, strict=True,
        description="Memory limit in MiB for the command and all descendants; swap is disabled.")] = 8192,
) -> dict:
    """Run a foreground command and wait for completion in one direct MCP call.

    No interactive input. Returns status, exit code, bounded output_tail, saved log
    paths and cgroup resource statistics. accounting_error and cleanup_error appear
    only on failure; unavailable counters remain null. Full metadata and applied
    limits are saved at result_path; abnormal results also include systemd diagnostics.
    Timeout, cancellation and memory exhaustion stop the command group.
    Use scratch.delete after the saved results are no longer needed.
    """
    executable = shell or pwd.getpwuid(os.getuid()).pw_shell
    environment = dict(os.environ)
    state = session_state(ctx)
    overrides = command_overrides(state)
    scripts = state.scripts()
    if scripts:
        if (Path(executable).name == 'sh' or
                Path(shutil.which(executable) or executable).resolve().name != 'bash'):
            raise ValueError('Registered bashrc scripts require Bash; select Bash or delete the scripts')
    with result_directory(scratch_ref) as out:
        expanded_workdir = workdir
        for variable in ("$BLOCKING_SHELL_SCRATCH_DIR", "${BLOCKING_SHELL_SCRATCH_DIR}"):
            if workdir == variable or workdir.startswith(variable + "/"):
                expanded_workdir = str(out.parent) + workdir[len(variable):]
                break
        work = Path(expanded_workdir)
        if not work.is_absolute() or not work.is_dir():
            raise ValueError("workdir must be an existing absolute directory after scratch expansion")
        startup = []
        for item in scripts:
            file = out / (item['ref'] + '.bash')
            file.write_text(item['script'], encoding='utf-8')
            startup.append(f'source {shlex.quote(str(file))} || exit $?\n')
        command = ''.join(startup) + cmd
        state.command(dict(cmd=cmd, workdir=workdir, scratch_ref=scratch_ref,
                           max_output_tokens=max_output_tokens, shell=executable, login=login,
                           timeout_seconds=timeout_seconds, memory_max_mib=memory_max_mib))
        log = out / "output.log"
        log.touch()
        unit = out.name + ".service"
        manager_log = out / "systemd.log"
        started = time.monotonic()
        status = "completed"
        result: dict = {}
        with manager_log.open("wb") as stream:
            proc = await asyncio.create_subprocess_exec(
                "systemd-run", "--user", "--service-type=oneshot", "--remain-after-exit",
                "--unit=" + unit, "--expand-environment=no",
                "--working-directory=" + str(work).replace("%", "%%"),
                "--property=MemoryAccounting=yes", "--property=IOAccounting=yes",
                "--property=MemoryMax=" + str(memory_max_mib * 1024**2),
                "--property=MemorySwapMax=0",
                "--property=OOMPolicy=kill",
                "--property=TimeoutStartSec=" + str(timeout_seconds),
                "--property=TimeoutStopSec=2", "--property=KillMode=control-group",
                "--property=StandardInput=null",
                "--property=StandardOutput=append:" + str(log).replace("%", "%%"),
                "--property=StandardError=inherit",
                *("--setenv=" + key for key in environment),
                *("--setenv=" + key + "=" + value for key, value in overrides.items()),
                "--setenv=BLOCKING_SHELL_SCRATCH_DIR=" + str(out.parent),
                executable, "-lc" if login else "-c", command,
                cwd=work, stdin=asyncio.subprocess.DEVNULL,
                stdout=stream, stderr=stream, env=environment)
            try:
                await proc.wait()
                if proc.returncode:
                    status = "failed"
            except BaseException:
                status = "cancelled"
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    code, accounting = await control(environment, "show", unit, "--property=" + ",".join((
                        "Result", "ExecMainCode", "ExecMainStatus", "CPUUsageNSec",
                        "MemoryPeak", "MemoryMax", "MemorySwapMax",
                        "IOReadBytes", "IOWriteBytes", "ControlGroup")))
                    properties = dict(line.split("=", 1) for line in accounting.splitlines()
                                      if "=" in line) if code == 0 else {}
                    stop_code, stop_output = await control(environment, "stop", unit)
                    await proc.wait()
                    await control(environment, "reset-failed", unit)
                    if status != "cancelled" and properties.get("Result") == "timeout":
                        status = "timeout"
                    exit_code = counter(properties, "ExecMainStatus")
                    if properties.get("ExecMainCode") in ("2", "3") and exit_code is not None:
                        exit_code = -exit_code
                    if status == "cancelled" and properties.get("ExecMainCode") == "0":
                        exit_code = None
                    cpu_ns = counter(properties, "CPUUsageNSec")
                    size = log.stat().st_size
                    tail, token_count, truncated = output_tail(log, max_output_tokens)
                    result.update(status=status, exit_code=exit_code,
                                  timeout_seconds=timeout_seconds,
                                  cpu_seconds=cpu_ns / 1e9 if cpu_ns is not None else None,
                                  memory_peak_bytes=counter(properties, "MemoryPeak"),
                                  memory_max_bytes=counter(properties, "MemoryMax"),
                                  memory_swap_max_bytes=counter(properties, "MemorySwapMax"),
                                  io_read_bytes=counter(properties, "IOReadBytes"),
                                  io_write_bytes=counter(properties, "IOWriteBytes"),
                                  unit=unit, unit_result=properties.get("Result"),
                                  control_group=properties.get("ControlGroup"),
                                  accounting_error=accounting.strip() if code else None,
                                  cleanup_error=stop_output.strip() if stop_code else None,
                                  systemd_log_path=str(manager_log),
                                  elapsed_seconds=round(time.monotonic() - started, 3),
                                  output_tail=tail, output_bytes=size,
                                  output_tokens=token_count, output_token_encoding=encoding.name,
                                  output_truncated=truncated,
                                  log_path=str(log), result_path=str(out / "result.json"))
                    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return summarize(result)


@mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
async def rerun(
    ctx: Context,
    scratch_ref: Annotated[str | None, Field(
        description="Replacement scratch.create ID; omit to reuse the previous reference.")] = None,
) -> dict:
    """Re-execute this conversation's last accepted command with the same run settings.

    Uses current environment overrides and creates fresh logs. The command survives
    MCP reconnects and compaction, including failed or cancelled runs. Invalid requests
    do not replace it. Concurrent runs are ordered by acceptance, not completion.
    With no previous command, or a deleted scratch reference, fails without executing.
    Inspect the prior result after transport failures before requesting a rerun.
    """
    arguments = session_state(ctx).command()
    if scratch_ref is not None:
        arguments['scratch_ref'] = scratch_ref
    return await run(ctx=ctx, **arguments)


if __name__ == "__main__":
    mcp.run()
