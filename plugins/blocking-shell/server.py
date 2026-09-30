# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1"]
# ///
"""Run foreground shell commands in one blocking MCP request."""
import asyncio
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import pwd
import shutil
import tempfile
import termios
import time

import anyio
import tiktoken
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("blocking-shell")
encoding = tiktoken.get_encoding("o200k_base")
max_token_bytes = max(map(len, encoding.token_byte_values()))
result_dirs: set[Path] = set()
active_dirs: set[Path] = set()


@contextmanager
def result_directory(logs: Path):
    out = Path(tempfile.mkdtemp(prefix="blocking-shell-", dir=logs))
    result_dirs.add(out)
    active_dirs.add(out)
    try:
        yield out
    finally:
        active_dirs.remove(out)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
async def cleanup() -> dict:
    """Delete result directories created by run in this MCP server session.

    Call directly, outside code-mode, when saved logs and results are no longer
    needed. Takes no paths; covers all log_dir locations used in this session.
    Running commands are skipped. Other sessions and unrelated files are untouched.
    Returns deleted, missing, skipped_active paths and errors by path. Failed
    deletions stay tracked for retry. Tracking ends when the MCP server exits.
    """
    result: dict = dict(deleted=[], missing=[], skipped_active=[], errors={})
    for out in sorted(result_dirs):
        path = str(out)
        if out in active_dirs:
            result["skipped_active"].append(path)
            continue
        try:
            shutil.rmtree(out)
        except FileNotFoundError:
            result["missing"].append(path)
        except OSError as error:
            result["errors"][path] = str(error)
            continue
        else:
            result["deleted"].append(path)
        result_dirs.remove(out)
    return result


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


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
async def run(cmd: str, workdir: str, log_dir: str,
              max_output_tokens: int = 10000, shell: str | None = None,
              login: bool = True, tty: bool = False,
              timeout_seconds: int = 21600) -> dict:
    """Run an authorized shell command and block until it exits; no polling handle.

    Call directly, outside code-mode. Shared exec_command arguments: cmd, workdir,
    max_output_tokens, shell, login and tty. workdir and log_dir must be existing
    absolute directories. workdir is required because MCP cannot see the turn cwd.
    Uses systemd-run --user and the user's default shell unless shell is supplied.
    login defaults to true; tty defaults to false (true allocates a new PTY).
    Interactive input is not exposed. Put environment assignments in cmd.
    max_output_tokens limits output_tail using tiktoken o200k_base, excluding JSON
    metadata. output_tokens is its exact count; output_token_encoding names the
    encoding. Full logs are retained. Invalid UTF-8 is replaced before counting.
    Saves combined stdout/stderr and result.json in a new
    directory under log_dir. Returns exit code, status, a bounded output tail,
    elapsed seconds, CPU seconds, peak memory bytes and block IO read/write bytes.
    Unavailable accounting counters are null. Statistics cover the unit cgroup
    before cleanup (before stopping the command on cancellation).
    timeout_seconds is 1..86400, default six hours. Omit it for builds unless an
    explicit deadline is required; do not shorten it from an estimated duration.
    The deadline stops the command, not just the wait. The result reports the
    effective timeout_seconds. max_output_tokens is nonnegative.
    No yield_time_ms or sandbox/approval arguments: this MCP blocks until completion
    and runs with the MCP server's permissions. On timeout or cancellation,
    stops the unit cgroup. Foreground jobs only; no detached daemons.
    """
    work = Path(workdir)
    if not work.is_absolute() or not work.is_dir():
        raise ValueError("workdir must be an existing absolute directory")
    logs = Path(log_dir)
    if not logs.is_absolute() or not logs.is_dir():
        raise ValueError("log_dir must be an existing absolute directory")
    if not 1 <= timeout_seconds <= 86400:
        raise ValueError("timeout_seconds must be 1..86400")
    if max_output_tokens < 0:
        raise ValueError("max_output_tokens must be nonnegative")
    executable = shell or pwd.getpwuid(os.getuid()).pw_shell
    environment = dict(os.environ)
    with result_directory(logs) as out:
        log = out / "output.log"
        log.touch()
        unit = out.name + ".service"
        manager_log = out / "systemd.log"
        started = time.monotonic()
        status = "completed"
        result: dict = {}
        master, slave = os.openpty() if tty else (-1, -1)
        if tty:
            termios.tcsetwinsize(slave, (24, 80))
        loop = asyncio.get_running_loop()
        terminal_done = loop.create_future()
        with manager_log.open("wb") as stream, log.open("wb", buffering=0) as output:
            def receive_output():
                try:
                    data = os.read(master, 65536)
                    if data:
                        output.write(data)
                        return
                except OSError as error:
                    if error.errno != errno.EIO:
                        loop.remove_reader(master)
                        terminal_done.set_exception(error)
                        return
                loop.remove_reader(master)
                terminal_done.set_result(None)

            if tty:
                loop.add_reader(master, receive_output)
            io_properties = (
                ["StandardInput=tty", "StandardOutput=tty", "StandardError=tty",
                 "TTYPath=" + os.ttyname(slave)] if tty else
                ["StandardInput=null", "StandardOutput=append:" + str(log).replace("%", "%%"),
                 "StandardError=inherit"])
            proc = await asyncio.create_subprocess_exec(
                "systemd-run", "--user", "--service-type=oneshot", "--remain-after-exit",
                "--unit=" + unit, "--expand-environment=no",
                "--working-directory=" + str(work).replace("%", "%%"),
                "--property=MemoryAccounting=yes", "--property=IOAccounting=yes",
                "--property=TimeoutStartSec=" + str(timeout_seconds),
                "--property=TimeoutStopSec=2", "--property=KillMode=control-group",
                *("--property=" + value for value in io_properties),
                *("--setenv=" + key for key in environment),
                executable, "-lc" if login else "-c", cmd,
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
                        "MemoryPeak", "IOReadBytes", "IOWriteBytes", "ControlGroup")))
                    properties = dict(line.split("=", 1) for line in accounting.splitlines()
                                      if "=" in line) if code == 0 else {}
                    stop_code, stop_output = await control(environment, "stop", unit)
                    await proc.wait()
                    await control(environment, "reset-failed", unit)
                    if tty:
                        os.close(slave)
                        await terminal_done
                        os.close(master)
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
    return result


if __name__ == "__main__":
    mcp.run()
