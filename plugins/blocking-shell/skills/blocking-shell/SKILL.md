---
name: blocking-shell
description: Run long builds, test suites, and other foreground shell commands through a single blocking MCP call. Blocking until completion avoids polling and can reduce token use compared with exec_command for long-running tasks.
---

Call `run` directly, outside code-mode cells, for authorized long-running commands.
Announce the command first; the call waits until completion without a poll handle.
Supply the existing absolute `workdir` and `scratch_ref` from Scratch's `create`.
Keep jobs in the foreground and use noninteractive options.

If a command already started with `exec_command` runs longer than expected,
replace repeated `write_stdin` polling with one direct `run` call using
`cmd: "tail --pid=12345 -f /dev/null"` (GNU tail). Replace `12345` with the
actual OS PID of the running command or its wrapper that waits for all work,
not the `exec_command` session ID or a persistent interactive shell. Obtain the
PID from existing output or one targeted process lookup. Supply `workdir` and
`scratch_ref` as usual; do not restart the original command.
After the wait returns, call `write_stdin` once on the original session to collect
its final output and exit code. A successful tail exit only confirms that the
PID disappeared, not that the command succeeded. The wait does not move the
original job into blocking-shell's cgroup: its limits, timeout, cancellation and
statistics apply only to the waiting process.

The execution deadline is six hours by default, including when omitted.
Set `timeout_seconds` when a different deadline is needed; expiry stops the
command and descendants. Do not shorten it from a guessed build duration.
Normally omit `shell` and `login`. Select `shell` when command syntax requires it;
set `login: false` when login startup files must not change the environment.
Use `set_env(values={...})` for environment overrides shared by this conversation's
subsequent `run` calls; `list_env()` returns them and `unset_env(names=[...])`
removes overrides, restoring inherited values if present. Values survive MCP
reconnects and are injected into model context after automatic compaction.
They apply only to blocking-shell commands, not other tools, other conversations,
or already-running commands. Commands otherwise inherit the server's startup
environment. Put per-command assignments in `cmd`; later changes in another
shell are not inherited. Login startup files can still change the environment.

Assess `status` and `exit_code`. Inspect saved log excerpts only when the returned
tail does not settle the result. After a transport failure, inspect the process
and saved result before rerunning. `accounting_error` and `cleanup_error` appear
only on failure; inspect them before relying on measurements or cleanup. Unavailable counters are null, not
zero; IO counts block-device traffic, including descendants, rather than cached IO.

The log tail defaults to 1000 tokens; `max_output_tokens` changes that limit,
excluding metadata. `result_path` retains full statistics, applied limits, and
diagnostics. Read it only when the returned summary leaves a question unanswered.

Call Scratch's `delete` with the reference after using the results. Active runs
hold a lease and are skipped; failed deletions remain retryable.
