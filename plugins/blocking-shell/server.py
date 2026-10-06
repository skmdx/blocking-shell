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
import time
from typing import Annotated

import anyio
import tiktoken
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from scratch_space import ScratchSpace
from word_ids import create_directory

mcp = FastMCP("blocking-shell")
encoding = tiktoken.get_encoding("o200k_base")
max_token_bytes = max(map(len, encoding.token_byte_values()))


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


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
async def run(
    cmd: Annotated[str, Field(
        description="Foreground command, including any per-command environment assignments.")],
    workdir: Annotated[str, Field(description="Existing absolute working directory.")],
    scratch_ref: Annotated[str, Field(description="ID from scratch.create for saved logs and results.")],
    max_output_tokens: Annotated[int, Field(ge=0, strict=True,
        description="Maximum tokens in the returned log tail; full logs are saved.")] = 10000,
    shell: Annotated[str | None, Field(
        description="Shell executable; omit for the user's default shell. "
                    "Specify when cmd requires a particular shell syntax.")] = None,
    login: Annotated[bool, Field(
        description="Use -lc (login startup files). Set false for -c when startup "
                    "files must not alter the command environment.")] = True,
    timeout_seconds: Annotated[int, Field(ge=1, le=86400, strict=True,
        description="Execution deadline in seconds, default six hours. Set when a "
                    "different deadline is needed; expiry stops the command and descendants.")] = 21600,
    memory_max_mib: Annotated[int, Field(ge=1, strict=True,
        description="Memory limit in MiB for the command and all descendants; swap is disabled.")] = 8192,
) -> dict:
    """Run a foreground command and wait for completion in one direct MCP call.

    No interactive input. Returns status, exit code, bounded output_tail, saved log
    paths and cgroup resource statistics. Inspect accounting_error and cleanup_error
    before relying on statistics or cleanup; unavailable counters are null.
    Timeout, cancellation and memory exhaustion stop the command group.
    Use scratch.delete after the saved results are no longer needed.
    """
    work = Path(workdir)
    if not work.is_absolute() or not work.is_dir():
        raise ValueError("workdir must be an existing absolute directory")
    executable = shell or pwd.getpwuid(os.getuid()).pw_shell
    environment = dict(os.environ)
    with result_directory(scratch_ref) as out:
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
    return result


if __name__ == "__main__":
    mcp.run()
