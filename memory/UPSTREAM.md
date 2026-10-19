# Upstream source

Source: https://github.com/pan-Rijovich/bc250-memory-temperature

Pinned commit: `4a2221d1da387fa2e738bb9368b39594e242ba4b`.
The upstream MIT notice is preserved in `LICENSE.upstream`.

`bc250_smu/` and `unlock.py` are adapted from that revision. Only the API
operations needed by this collector are exposed. Changes include strict status
and transfer-length checks, one Q3 argument, shared transaction locking,
validated input ranges, no further commands after transport failure, full
DMA table readback using a cleared buffer, and restoring saved unlock regions
instead of hardcoded contents. No automatic rollback is attempted after a failed
SMU transaction because firmware may still be executing it.

The transport holds an exclusive `flock` on the PCI config file across each
SMN address/value pair (`0xB8`/`0xBC`), matching the lock other BC-250 SMU
tools take on that file, so a pair cannot be torn by them (issue #16). The
previously selected address is restored before the lock is released, because
those tools lock each access separately rather than the pair.

SRAM reads now transfer the staged source twice into differently filled host
buffers and require matching results. This detects missing/partial DMA writes
that would otherwise be mistaken for zero-valued SRAM.

The payload binary, symbol list, C source, linker script and Makefile in
`payload/` are unmodified upstream files. Binary SHA-256:
`32ab902bb07a5a00cdb54f8686b8944fed8cb30abf394c395d5e8d421f756529`.
The binary is 764 bytes at `0x3AA9C`, followed by 144 bytes of BSS that
upstream's installer does not initialize; this service uploads them as zeros.
Unlike upstream, it installs only into an entirely zero region and never
overwrites existing SRAM contents.
Handlers: Q3/5 `0x3AB34`, Q3/6 `0x3AC34`, Q3/7 `0x3AD54`. Like upstream, the
SMU firmware version word must be `0x00580600` before unlocking.

The firmware polling loops are bounded (100000 iterations) and report
timeouts in the result. The host timeout does not cancel firmware execution.
This service stops on SMU errors and retains a per-boot guard instead of
retrying automatically.

The previously bundled 176-byte build (`b7e6bff`, SHA-256
`b31908460e932a615d9eafb6b3112e6448994f9f6fa656d1d80a8616ac1df4df`) and the
`bounded` build are still recognized when a BIOS installs them, but are no
longer installed from here.
