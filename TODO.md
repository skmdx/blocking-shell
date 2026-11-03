# Unresolved problems

- Account for privileged descendants that leave the execution unit. A hardware
  probe launched through `sudo` remained alive after `run` reported a timeout
  and its unit was inactive. The child still held the device; a targeted kill
  remained pending in device IO. Keep such descendants contained, or report
  incomplete cleanup instead of implying the entire command group stopped.

- Retry generated execution identifiers on database uniqueness collisions. A
  foreground `run` failed before execution with `UNIQUE constraint failed:
  executions.run_ref`; `result()` still identified the preceding completed run,
  and resubmitting the command succeeded. A collision must not require the
  caller to decide whether the command was accepted or risk repeating it.
