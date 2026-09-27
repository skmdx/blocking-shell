"""Check the installed plugin through Codex with a real delayed make build.

Usage: python3 tests/smoke_codex.py --codex /path/to/codex --out-dir /absolute/scratch
The output directory must not exist. Uses the current Codex account.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.out_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ, TERM="xterm-256color")
    (root / "hello.c").write_text('#include <stdio.h>\nint main(void) { puts("BUILD_OK"); }\n')
    (root / "Makefile").write_text('all:\n\ttest -t 0 && test -t 1 && test -t 2\n\ttest "$$TERM" = xterm-256color\n\tsleep 65\n\t$(CC) hello.c -o hello\n\t./hello\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    prompt = f'''Test the installed blocking-shell plugin. This is an authorized test build.
First use functions.exec to print typeof tools.mcp__blocking_shell__run and whether
ALL_TOOLS contains that name. Do not run shell commands from functions.exec.
Then call the direct blocking-shell run MCP tool exactly once with cmd="make",
log_dir={str(root)!r}, workdir={str(root)!r}, tty=true, login=false.
Use the plugin's default timeout without overriding it.
Do not use exec_command, polling, or another command runner. After it returns,
report the tool result briefly. Do not edit files or invoke other tools.'''
    with (root / "events.jsonl").open("w") as out, (root / "stderr.log").open("w") as err:
        result = subprocess.run([args.codex, "exec", "--enable", "code_mode", "--enable",
                                 "code_mode_only", "--json",
                                 "--dangerously-bypass-approvals-and-sandbox", prompt],
                                cwd=root, stdout=out, stderr=err, env=environment)
    assert result.returncode == 0, root / "stderr.log"
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    thread = next(e["thread_id"] for e in events if e["type"] == "thread.started")
    sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    log = next(sessions.rglob(f"*{thread}*.jsonl"))
    records = [json.loads(line) for line in log.read_text().splitlines()]
    calls = [(i, r) for i, r in enumerate(records)
             if r.get("payload", {}).get("type") in ("function_call", "custom_tool_call")]
    runs = [(i, r) for i, r in calls if r["payload"].get("namespace") == "mcp__blocking_shell"
            and r["payload"].get("name") == "run"]
    assert len(runs) == 1, calls
    index, call = runs[0]
    assert "timeout_seconds" not in json.loads(call["payload"]["arguments"]), call
    finish_index, finish = next((i, r) for i, r in enumerate(records) if i > index
                               and r.get("payload", {}).get("call_id") == call["payload"]["call_id"]
                               and r["payload"].get("type") == "function_call_output")
    duration = (datetime.fromisoformat(finish["timestamp"]) - datetime.fromisoformat(call["timestamp"])).total_seconds()
    assert duration >= 65, duration
    assert not any(index < i < finish_index for i, _ in calls)
    assert not any(r["payload"].get("name") in ("wait", "write_stdin", "exec_command") for _, r in calls)
    outputs = "\n".join(str(r["payload"].get("output", "")) for r in records
                        if r.get("payload", {}).get("type", "").endswith("call_output"))
    assert "undefined" in outputs and "false" in outputs, outputs
    assert "Script running with cell ID" not in outputs
    report, = [json.loads(p.read_text()) for p in root.glob("blocking-shell-*/result.json")]
    assert report["status"] == "completed" and report["exit_code"] == 0, report
    assert report["timeout_seconds"] == 21600, report
    assert "BUILD_OK" in report["output_tail"], report
    assert report["cpu_seconds"] > 0 and report["memory_peak_bytes"] > 0, report
    assert report["io_read_bytes"] is not None and report["io_write_bytes"] is not None, report
    assert report["accounting_error"] is None and report["cleanup_error"] is None, report
    if report["control_group"]:
        assert not Path("/sys/fs/cgroup", report["control_group"].lstrip("/")).exists(), report
    assert subprocess.check_output([str(root / "hello")], text=True).strip() == "BUILD_OK"
    summary = dict(passed=True, direct_calls=1, polling_calls=0,
                   timeout_seconds=report["timeout_seconds"],
                   duration_seconds=duration, session_log=str(log),
                   cpu_seconds=report["cpu_seconds"], memory_peak_bytes=report["memory_peak_bytes"],
                   io_read_bytes=report["io_read_bytes"], io_write_bytes=report["io_write_bytes"])
    (root / "verification.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
