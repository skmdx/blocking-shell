---
name: blocking-shell
description: Run long builds, test suites, and other foreground shell commands through a single blocking MCP call. Blocking until completion avoids polling and can reduce token use compared with exec_command for long-running tasks.
---

Use the blocking-shell `run` MCP tool directly, outside `functions.exec` or
other code-mode cells. It waits for completion and returns no poll handle.
Tell the user what is running before the call; the call may remain pending
for the full command duration.

Pass the authorized shell text as `cmd`, the command's existing absolute
`workdir`, and an existing absolute scratch directory as `log_dir`.
Use the workspace's temporary directory when specified.
`timeout_seconds` defaults to six hours and permits up to 24 hours.
No environment capture or preparation file is needed.

Shared arguments follow exec_command: `cmd`, `workdir`, `max_output_tokens`,
`shell`, `login`, and `tty`. Unlike exec_command, `workdir` is required because
MCP does not receive the turn's cwd. `shell` defaults to the user's default shell;
`login` defaults to true and `tty` to false. `max_output_tokens` defaults to 10000;
the returned `output_tail` is measured with tiktoken `o200k_base` and kept within
that limit. `output_tokens` reports its exact count and `output_token_encoding`
names the encoding. JSON metadata is excluded. Invalid UTF-8 is replaced before
counting. This is an encoding-specific count, not the model's billing usage.
There is no `yield_time_ms`, sandbox or approval argument. Calls block until
completion and use the MCP server's host permissions.

Commands run in a systemd user service with the MCP server's startup environment.
The plugin forwards user-bus, terminal, display and SSH-agent variables in addition
to Codex's default environment. It does not inherit later changes in another shell.
Put assignments in `cmd`, e.g. `CC=clang make`; they do not affect later calls.
Values in commands appear in tool-call history; keep credentials in the startup
environment. `tty=true` allocates a new 80-column, 24-row PTY connected to all three
standard streams. Otherwise stdin is closed and stdout/stderr go to the combined
log. Interactive input is not exposed; use noninteractive options and keep jobs
in the foreground.

Read `status` and `exit_code`; a timeout is not a successful build. Full combined
output and `result.json` remain at the returned paths. Inspect only the necessary
log excerpts and remove scratch results when no longer needed. After a transport
failure, inspect the process and saved result before deciding to rerun a command.

Results include `cpu_seconds`, `memory_peak_bytes`, `io_read_bytes` and
`io_write_bytes` for the unit cgroup, including descendants. Unavailable counters
are `null`. IO means block-device traffic, not application read/write sizes or
cached IO. Accounting is captured before unit cleanup; on cancellation this is
before stopping the command. `elapsed_seconds` includes setup and cleanup.
`systemd_log_path` contains launcher diagnostics separately from command output.

The MCP server runs with its own host permissions, not the shell tool's sandbox
or approval mechanism. Use it only for commands already authorized in that
environment. Requires a running systemd user manager and user-bus access in
Codex's startup environment (`XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS`).
The unit uses memory and IO accounting, enforces the timeout, and stops all
remaining cgroup processes on cleanup. Check `accounting_error` and
`cleanup_error` before relying on measurement or cleanup success.
