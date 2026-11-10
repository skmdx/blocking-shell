---
name: blocking-shell
description: Reuse Bash variables, functions and cwd across blocking MCP calls. Use for successive shell snippets and long builds or tests without polling.
---

## Run in the persistent Bash

Call `run` directly, outside code-mode cells, for authorized commands.
Announce long work first. Usually pass only `cmd`; the call waits for completion.

Use `export`, `unset`, `source`, functions and `cd` normally. State persists within
this conversation, and calls serialize. Use `declare -p NAME`, `declare -f NAME`
or `compgen -v` to inspect it. Enable `shopt -s expand_aliases` for aliases.
Keep work noninteractive and in the foreground when its completion matters.

An optional `workdir` performs cd before execution. Initial cwd is CODEX_WORK or
~/codex-work. Bash login files and the user's configured BASH_ENV run once at
startup; reset rereads them. Use `reset(login=false)` to skip login files.
There is no environment or bashrc registration API.
The initial cwd and scratch root are created if missing; explicit workdir must exist.

Scratch storage is automatic and reused. Pass `scratch_ref` only to select a
specific existing Scratch directory. `SCRATCH_DIR` is exported on every call;
`workdir="$SCRATCH_DIR"` and its subdirectories are supported.
Use the returned scratch_ref with Scratch delete when logs are no longer needed.
An active execution holds a lease. Move cwd out of storage before deleting it.

## Assess and recover

Check status and exit_code. Finished means final, not necessarily successful.
Only read log_path or result_path when the returned tail leaves a question.
Full output stays on disk. CPU/IO are cgroup deltas over the execution interval,
including background jobs; session_memory_peak_bytes covers the entire Bash
lifetime. Missing counters are null. Investigate accounting_error or cleanup_error
before relying on measurements or cleanup.

Normally retain the timeout and memory settings. Default timeout is six hours
per call; initial memory limit is 8 GiB for Bash and descendants. A changed memory
limit persists. Do not automatically retry or raise limits after exhaustion.

Ordinary nonzero exit status retains state. Shell exit (including exec or errexit),
timeout, cancellation and OOM lose it. After state_lost, call reset and restore
only needed setup. Reset stops descendants and clears Bash state, retaining
omitted cwd/login/memory settings and all saved results. Invalid paths do not
discard a live shell. Server restart loses Bash state; compaction alone does not.

After a lost response, use result() before deciding to repeat side effects.
Omit run_ref for the last accepted execution. Finished summaries match run;
running, unknown and expired do not establish success. Use this for exceptional
recovery, not polling. History survives server restart.

rerun() repeats the last accepted cmd and settings in current Bash state.
Select earlier work with run_ref; replace deleted storage with scratch_ref.
It repeats side effects and requires the same authorization as run.
An optional suggestion about repeated long commands is advisory.

Background jobs persist until they end, reset or server shutdown. Commands have
no interactive stdin or PTY. FD 197/198 and names beginning __blocking_shell_
are reserved for the driver. Do not modify them.

## Wait for an already running command

One run call with `tail --pid=PID -f /dev/null` (GNU tail) can wait for an existing
OS process without polling. Use the actual process PID, not an exec session ID
or persistent shell PID. Then read the original session's output once.
The wait's success confirms only that the PID vanished; its limits and resource
statistics apply to the waiting process, not the original work.
