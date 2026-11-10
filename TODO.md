# Unresolved problems

- Account for privileged descendants that leave the execution unit. A hardware
  probe launched through `sudo` remained alive after `run` reported a timeout
  and its unit was inactive. The child still held the device; a targeted kill
  remained pending in device IO. Keep such descendants contained, or report
  incomplete cleanup instead of implying the entire command group stopped.
