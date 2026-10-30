# Unresolved problems

- Retry generated execution identifiers on database uniqueness collisions. A
  foreground `run` failed before execution with `UNIQUE constraint failed:
  executions.run_ref`; `result()` still identified the preceding completed run,
  and resubmitting the command succeeded. A collision must not require the
  caller to decide whether the command was accepted or risk repeating it.
