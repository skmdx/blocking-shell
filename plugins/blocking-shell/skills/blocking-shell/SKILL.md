---
name: blocking-shell
description: Run long builds, test suites, and other foreground shell commands through a single blocking MCP call. Blocking until completion avoids polling and can reduce token use compared with exec_command for long-running tasks.
---

## Run and assess

Call `run` directly, outside code-mode cells, for authorized long-running commands.
Announce the command first. Supply `cmd`, an existing `workdir`, and `scratch_ref`
from Scratch's `create`. Keep commands in the foreground and noninteractive.
The call waits for completion; normal use needs no subsequent status request.

Prefer paths relative to `workdir`. For work inside the selected Scratch directory,
use `$BLOCKING_SHELL_SCRATCH_DIR` as `workdir`, optionally with a `/subdir`.
The braced form also works; other variables and shell expressions are not expanded.
In `cmd`, use `"$BLOCKING_SHELL_SCRATCH_DIR"` to access that directory.
The tool supplies this variable on every run, overriding environment settings.

Assess `status` and `exit_code`. `state: finished` means the result is final,
not that the command succeeded. Read `log_path` or `result_path` only when the
returned summary leaves a question unanswered. Log truncation preserves the full
log on disk. Unavailable counters are null, not zero; IO measures block-device
traffic including descendants. Inspect `accounting_error` or `cleanup_error`
when present before relying on measurements or cleanup.

Normally keep the default execution deadline and memory limit. Change them for
known execution requirements, not a guessed build duration. Memory exhaustion,
timeout and cancellation stop the command group. Do not automatically retry
memory exhaustion or bypass its limit.
After using the results, call Scratch's `delete`. Active leases prevent deletion;
retry skipped or failed cleanup after the work has ended.

## Recover a result or repeat a command

After a lost response or reconnect, call `result()` before considering a rerun.
It returns the last accepted execution in this conversation. With concurrent
runs, acceptance order differs from completion order; inspect the returned
`run_ref`. Use `result(run_ref=...)` for a known execution.

A finished result has the same summary as `run`. Otherwise `state` distinguishes
`running`, `finishing`, `unknown` and `expired`. Missing units or saved results
do not establish success. No command is restarted and no job recovery occurs.
Use this for exceptional recovery, not periodic polling.

`rerun()` repeats the last accepted command with its saved execution conditions,
current environment settings and fresh logs. Pass `scratch_ref` to replace a
deleted storage reference. History survives reconnects and compaction; invalid
requests do not replace it. Re-execution repeats side effects and requires the
same authorization as `run`.

## Configure commands

Keep shared aliases, functions and environment settings in the Bash file selected
by top-level `shell_environment_policy.set.BASH_ENV` in the user
`$CODEX_HOME/config.toml` (default `~/.codex/config.toml`). Each run rereads this
setting, falling back to inherited `BASH_ENV` when absent. Project, profile and
CLI overrides are not read. Bash reads the file with either login setting.

Use per-command assignments in `cmd`. For conversation overrides, use `set_env`
and `unset_env`; their responses identify changed names. `list_env` reads all
configured values. Overrides survive reconnects and apply to subsequent
`run`/`rerun` only. They take precedence over inherited values and the user
configuration, but shell startup files may change them. Later changes in another
shell are not inherited. Compaction restores configured names, not their values;
commands still receive the stored values without an extra read.

For conversation-specific Bash startup code, use `set_bashrc(script=...)`.
Keep its returned `ref` to replace or delete it. Scripts run in registration order,
after ordinary Bash startup and before `cmd`; a nonzero source status stops the
remaining scripts and command. Registered scripts require Bash. Use
`shopt -s expand_aliases` for aliases; do not introduce interactive prompts.

`list_bashrc` lists refs in execution order. `get_bashrc(refs=[...])` reads selected
bodies together, preserving input order and errors per ref. If `next_cursor` is
non-null, repeat the same selection with that cursor and concatenate each body's
fragments by character offset. A changed selection rejects continuation: restart
without the cursor. Scripts persist across reconnects and compaction; changes
affect subsequent commands, not running jobs.

## Wait for work started with exec_command

If an existing command runs longer than expected, one direct `run` call with
`cmd: "tail --pid=12345 -f /dev/null"` (GNU tail) can replace repeated polling.
Use the actual OS PID of the command or a wrapper that waits for all its work,
not an exec session ID or persistent shell. Obtain it from existing output or
one targeted lookup, and supply `workdir` and `scratch_ref` normally.
Do not restart the original command.

After the wait returns, call `write_stdin` once on the original session for its
output and exit code. A successful tail exit only confirms that the PID vanished.
The original job stays outside blocking-shell's cgroup; the wait's limits,
cancellation and statistics apply only to the waiting process.
