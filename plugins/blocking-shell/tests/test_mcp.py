# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1"]
# ///
"""Exercise the actual stdio server and real shell processes."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import shlex

import tiktoken

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CancelledNotification, CancelledNotificationParams, ClientNotification, TextContent
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scratch_space import ScratchSpace
from word_ids import valid_id


async def main():
    encoding = tiktoken.get_encoding("o200k_base")
    server = Path(__file__).resolve().parents[1] / "server.py"
    with tempfile.TemporaryDirectory(dir=sys.argv[1], prefix="blocking-shell-test-") as tmp:
        root = Path(tmp)
        space = ScratchSpace(root, session='shell-tests')
        item = space.create()
        ref, logs = item['scratch_ref'], Path(item['path'])
        env = dict(os.environ, SCRATCH_ROOT=tmp, BLOCKING_SHELL_TEST="inherited $value % value")
        async with stdio_client(StdioServerParameters(command=sys.executable, args=[str(server)], env=env, cwd=tmp)) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                schemas = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
                assert set(schemas) == {"run"}, schemas
                schema = schemas["run"]
                assert set(schema["required"]) == {"cmd", "workdir", "scratch_ref"}, schema
                assert schema["properties"]["timeout_seconds"]["default"] == 21600
                assert schema["properties"]["memory_max_mib"]["default"] == 8192
                assert schema["properties"]["tty"]["default"] is False
                assert schema["properties"]["login"]["default"] is True
                assert not {"env_file", "env", "cwd", "command", "tail_bytes"} & schema["properties"].keys(), schema

                async def run(command, **kwargs):
                    response = await session.call_tool("run", dict(cmd=command, workdir=tmp, scratch_ref=ref, login=False, **kwargs))
                    assert not response.isError, response
                    content = response.content[0]
                    assert isinstance(content, TextContent)
                    result = json.loads(content.text)
                    assert json.loads(Path(result["result_path"]).read_text()) == result
                    assert valid_id(Path(result["result_path"]).parent.name.removeprefix('blocking-shell-')), result
                    assert result["output_token_encoding"] == "o200k_base"
                    assert result["output_tokens"] == len(encoding.encode_ordinary(result["output_tail"]))
                    assert result["output_tokens"] <= kwargs.get("max_output_tokens", 10000)
                    assert result["accounting_error"] is None, result
                    assert result["cleanup_error"] is None, result
                    assert result["timeout_seconds"] == kwargs.get("timeout_seconds", 21600), result
                    assert result["memory_max_bytes"] == kwargs.get("memory_max_mib", 8192) * 1024**2, result
                    assert result["memory_swap_max_bytes"] == 0, result
                    if result["control_group"]:
                        assert not Path("/sys/fs/cgroup", result["control_group"].lstrip("/")).exists(), result
                    return result

                # Read the actual kernel limits, then exceed a small limit in
                # a child. The entire service must stop before its parent can
                # report success. This allocates at most 64 MiB, without swap.
                r = await run("python3 - <<'PY'\nfrom pathlib import Path\np = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split('0::')[1].lstrip('/')\nprint((p/'memory.max').read_text().strip(), (p/'memory.swap.max').read_text().strip(), (p/'memory.oom.group').read_text().strip())\nPY")
                assert r["output_tail"].strip() == "8589934592 0 1", r
                r = await run("python3 -c 'x=bytearray(256*1024*1024)' ; echo escaped-memory-limit", memory_max_mib=64)
                assert r["status"] == "failed" and r["unit_result"] == "oom-kill", r
                assert "escaped-memory-limit" not in r["output_tail"], r
                response = await session.call_tool("run", dict(cmd="touch invalid-memory", workdir=tmp, scratch_ref=ref, memory_max_mib=0))
                assert response.isError and not (root / "invalid-memory").exists()

                r = await run('unit=$(basename "$(sed -n "s/^0:://p" /proc/self/cgroup)"); systemctl --user show "$unit" --property=TimeoutStartUSec --value')
                assert r["output_tail"].strip() == "6h", r
                r = await run("pwd; printf hello; printf error >&2; test -t 0 && test -t 1 && test -t 2 && echo tty-connected", tty=True)
                assert r["status"] == "completed" and r["exit_code"] == 0, r
                assert tmp in r["output_tail"] and "helloerrortty-connected" in r["output_tail"], r
                r = await run('printf "%s\\n" "$BLOCKING_SHELL_TEST"; cat /proc/self/cgroup')
                assert "inherited $value % value" in r["output_tail"], r
                assert r["unit"] in r["output_tail"], r
                r = await run("BLOCKING_SHELL_TEST='overridden $value %' ADDED=new "
                              "bash -c 'printf \"%s|%s\" \"$BLOCKING_SHELL_TEST\" \"$ADDED\"'")
                assert r["output_tail"] == "overridden $value %|new", r
                r = await run('printf "%s|%s" "$BLOCKING_SHELL_TEST" "${ADDED-unset}"')
                assert r["output_tail"] == "inherited $value % value|unset", r
                r = await run('python3 -c "x=bytearray(32*1024*1024); print(sum(range(1000000)))"; '
                              'dd if=/dev/urandom of=io.bin bs=1M count=2 oflag=direct status=none; '
                              'dd if=io.bin of=/dev/null bs=1M iflag=direct status=none; rm io.bin')
                assert r["cpu_seconds"] > 0 and r["memory_peak_bytes"] >= 32*1024*1024, r
                assert r["io_read_bytes"] >= 2*1024*1024 and r["io_write_bytes"] >= 2*1024*1024, r
                r = await run("test ! -t 0 && test ! -t 1 && test ! -t 2 && printf no-tty")
                assert r["exit_code"] == 0 and r["output_tail"] == "no-tty", r
                r = await run("printf '%s' \"$0\"", shell="/bin/sh")
                assert r["output_tail"] == "/bin/sh", r
                r = await run("false | true")
                assert r["exit_code"] == 0, r
                response = await session.call_tool("run", dict(cmd="shopt -q login_shell", workdir=tmp,
                                                             scratch_ref=ref, shell="/bin/bash"))
                assert not response.isError, response
                assert isinstance(response.content[0], TextContent)
                assert json.loads(response.content[0].text)["exit_code"] == 0, response
                r = await run("exit 7")
                assert r["status"] == "failed" and r["exit_code"] == 7, r
                r = await run("kill -TERM $$")
                assert r["status"] == "failed" and r["exit_code"] == -15, r
                r = await run("setsid bash -c 'trap \"\" TERM; echo ready > detached-ready; sleep 4; touch detached-escaped' & sleep 0.1")
                assert (root / "detached-ready").exists(), r
                r = await run("set -o pipefail; false | true")
                assert r["exit_code"] == 1, r
                r = await run("head -c 1000000 /dev/zero", max_output_tokens=8)
                assert r["output_bytes"] == 1000000 and r["output_tokens"] == 8 and r["output_truncated"], r
                for value in ["hello world", "日本語の出力です。😀🧑🏽‍💻", "<|endoftext|>", " " * 1024]:
                    total = len(encoding.encode_ordinary(value))
                    for budget in [0, 1, 2, total, total + 1]:
                        r = await run("printf %s " + shlex.quote(value), max_output_tokens=budget)
                        assert value.endswith(r["output_tail"]), r
                        assert "�" not in r["output_tail"], r
                        assert r["output_truncated"] == (total > budget), r
                        if budget >= total:
                            assert r["output_tail"] == value, r
                r = await run("printf '\\377a'", max_output_tokens=2)
                assert r["output_tail"] == "�a", r
                r = await run("echo quiet", max_output_tokens=0)
                assert r["output_tail"] == "" and r["output_bytes"] == 6, r
                r = await run("(trap '' TERM; sleep 4; touch escaped) & wait", timeout_seconds=1)
                assert r["status"] == "timeout" and r["exit_code"] != 0, r
                # Send the protocol cancellation explicitly: cancelling an SDK
                # client task alone does not notify the server.
                request_id = session._request_id
                task = asyncio.create_task(run("echo started; (sleep 4; touch cancelled-escaped) & wait"))
                await asyncio.sleep(0.5)
                assert any(p.read_bytes() == b"started\n" for p in logs.glob("*/output.log"))
                await session.send_notification(ClientNotification(CancelledNotification(
                    method="notifications/cancelled",
                    params=CancelledNotificationParams(requestId=request_id))))
                try:
                    await task
                except McpError as error:
                    assert "cancel" in str(error).lower(), error
                else:
                    raise AssertionError("cancelled request unexpectedly succeeded")
                await asyncio.sleep(4)
                assert not (root / "escaped").exists()
                assert not (root / "cancelled-escaped").exists()
                assert not (root / "detached-escaped").exists()
                reports = [json.loads(p.read_text()) for p in logs.glob("*/result.json")]
                assert any(r["status"] == "cancelled" for r in reports), reports
                response = await session.call_tool("run", dict(cmd="touch invalid", workdir=tmp, scratch_ref=ref, timeout_seconds=0))
                assert response.isError and not (root / "invalid").exists()

                task = asyncio.create_task(run("echo active > scratch-ready; sleep 1; echo finished"))
                while not (root / 'scratch-ready').exists():
                    await asyncio.sleep(0.01)
                assert space.delete()['skipped_active'] == [ref]
                await task
                assert space.delete()['deleted'] == [ref]
                assert not logs.exists()
    print("PASS: real MCP execution, failure, pipefail, bounded output, timeout, cancellation, validation and scratch leases")


if __name__ == "__main__":
    asyncio.run(main())
