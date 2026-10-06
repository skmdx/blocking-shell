---
name: blocking-shell
description: Run long builds, test suites, and other foreground shell commands through a single blocking MCP call. Blocking until completion avoids polling and can reduce token use compared with exec_command for long-running tasks.
---

Call `run` directly, outside code-mode cells, for authorized long-running commands.
Announce the command first; the call waits until completion without a poll handle.
Supply the existing absolute `workdir` and `scratch_ref` from Scratch's `create`.
Keep jobs in the foreground and use noninteractive options.

The execution deadline is six hours by default, including when omitted.
Set `timeout_seconds` when a different deadline is needed; expiry stops the
command and descendants. Do not shorten it from a guessed build duration.
Normally omit `shell` and `login`. Select `shell` when command syntax requires it;
set `login: false` when login startup files must not change the environment.
Commands inherit the MCP server's startup environment. Put per-command environment
assignments in `cmd`; later changes in another shell are not inherited.

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
