# Upstream source

Source: https://github.com/pan-Rijovich/bc250-memory-temperature

Pinned commit: `b7e6bffcb5d592fc03edde375b7598ddc79aa846`.
The upstream MIT notice is preserved in `LICENSE.upstream`.

`bc250_smu/` and `unlock.py` are adapted from that revision. Only the API
operations needed by this collector are exposed. Changes include strict status
and transfer-length checks, one Q3 argument, shared transaction locking,
validated input ranges, no further commands after transport failure, full
DMA table readback using a cleared buffer, and restoring saved unlock regions
instead of hardcoded contents. No automatic rollback is attempted after a failed
SMU transaction because firmware may still be executing it.

SRAM reads now transfer the staged source twice into differently filled host
buffers and require matching results. This detects missing/partial DMA writes
that would otherwise be mistaken for zero-valued SRAM.

The payload binary, ELF, C source, linker script and Makefile in `payload/`
are unmodified upstream files. Binary SHA-256:
`b31908460e932a615d9eafb6b3112e6448994f9f6fa656d1d80a8616ac1df4df`.
The binary is 176 bytes; `.text` starts at `0x3AA9C`, and the function
`umc_read_temp_per_chip` is at `0x3AAC4` in the bundled ELF.

The original firmware polling loops are still unbounded. We deliberately do
not change firmware behavior without rebuilding and validating it on P3.0.
The host timeout does not cancel firmware execution. This service stops on SMU
errors and retains a per-boot guard instead of retrying automatically.
