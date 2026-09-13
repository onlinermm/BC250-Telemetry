# GDDR6 temperature service

An optional root service patches SMU at startup and reads eight memory chips
every three seconds. It is separate from `apu_telemetry` and the web server.
It uses Python's standard library only; no pip packages or runtime downloads.
The v2 dashboard displays all eight chip temperatures, average and hotspot.
Unavailable/stale readings show dashes, and sensor saturation is marked with
`≥`. The board illustration shows temperature halos and outlines all tied
hotspots. Halos disappear when readings are unavailable or stale.
The classic dashboard is unchanged.

## Installation

Run `sudo ./install.sh` for a separate memory-monitoring question on the first
installation step. Enter keeps the existing setting, defaulting to off on a
fresh install. The collector requires the supported stock P3.0 BIOS.
For an unattended install, choose explicitly:

```sh
sudo ./install.sh --memory-temp
journalctl -u bc250-memory.service -f
curl -s http://localhost:8090/api/telemetry
cat /run/apu_telemetry.json
```

Without a terminal or flag, the existing setting is preserved. Use
`--no-memory-temp` to disable it without a question. Choosing no also stops
and disables a previously installed memory service. Files are installed
root-owned in `/opt/bc250-memory`.
Stopping/disabling it is independent of the main telemetry services:

```sh
sudo systemctl disable --now bc250-memory.service
```

For a platform/payload check that does not access SMU:

```sh
sudo python3 -B memory/collector.py --check
```

The platform gate accepts DMI board `AMD BC-250`/`BC-250`/`BC250`, BIOS `P3.0`/`P3.00`,
an AMD PCI root, and GPU `1002:13fe`. These identifiers do not authenticate
firmware contents; custom firmware with the same identifiers is unsupported.
Stop other SMU patching/overclock tools before enabling this service. Its file
lock coordinates instances of this collector only, not arbitrary utilities or
kernel drivers.

## Startup and failure behavior

Before opening PCI config space, the collector validates the pinned payload
size/hash and platform. It checks for an already-installed matching patch, or
unlocks SMU, uploads the payload, reads it back, then switches and verifies the
Q3/5 handler. Successful restarts reuse the existing matching patch.
An initially empty Q3/5 slot (`0x00000000`) is accepted. It is filled only after
the uploaded payload passes readback verification.

The firmware payload retains upstream's unbounded UMC waits. Python stops
waiting after five seconds, but cannot cancel firmware execution. The service
has `Restart=no`. SMU/transport failures or interruption during a transaction
leave `/run/bc250-memory/patch-state.json` blocking subsequent attempts in the
same boot. Reboot (a cold power cycle may be needed for a wedged SMU) before
retrying; do not delete the guard to force a retry. Uninstall preserves the
guard. A clean stop leaves it ready. Stopping/uninstalling does not undo the
in-memory firmware patch; firmware reload on reboot removes it.

One earlier-version guard is migrated automatically: the exact error
`unexpected original Q3/5 handler: 0x00000000`. That version stopped after
completed SRAM reads, before unlocking or writing the patch. The updated
service repeats validation and can install into the empty slot. This exception
does not allow retrying timeouts, interrupted operations or failed writes.

The C payload and firmware calling conventions have not been validated on
hardware by this integration. See `UPSTREAM.md` for provenance and local fixes.

## Data contract

The C++ daemon is the sole writer of **`/run/apu_telemetry.json`**, including
the top-level `memory` object below. **GET `/api/telemetry`** serves that same
snapshot without merging files. The SMU collector supplies the daemon with
an atomic intermediate snapshot at `/run/bc250-memory/telemetry`; the daemon
only reads that file and never accesses SMU.

```json
{
  "memory": {
    "valid": true,
    "status": "ok",
    "error": null,
    "age_ms": 120,
    "chips_c": [36, 34, 44, 36, 36, 42, 42, 38],
    "average_c": 38.5,
    "hotspot_c": 44,
    "hotspot_chip": 2,
    "saturated": false,
    "saturated_chips": [],
    "raw": [9766, 9509, 10794, 9766, 9766, 10537, 10537, 10023]
  }
}
```

Indices 0..7 are upstream UMC/chip indices. The v2 illustration uses the board
owner's supplied top/X-ray mapping: 0=A/U27 (lower left), 1=B/U29 (bottom left
center), 2=C/U31 (bottom right center), 3=D/U33 (lower right), 4=E/U43 (upper
right), 5=F/U41 (top right center), 6=G/U39 (top left center), 7=H/U37 (upper
left). This mapping has not been independently verified on hardware. Halo
colors interpolate from blue at 20 °C through amber at 70 °C to pink at
120 °C; these are visualization anchors, not hardware alarm thresholds.

The JEDEC temperature code is the low byte, in 0..80. Code 80
means **at least 120 °C**: the corresponding chip, average and hotspot values
are lower bounds, indicated by `saturated` and `saturated_chips`. A zero code
is valid (-40 °C); invalid codes invalidate the whole sample, not just one chip.

`memory` is present even when the service is not installed. On missing, invalid,
stopped, failed or stale data, `valid` is false, chip temperatures are eight
nulls, and the aggregate temperatures and hotspot index are null. `status`
is `unavailable`, `invalid_data`, `starting`, `stopped`, `error`,
`invalid_reading`, or `stale`. Age uses Linux boot time (including suspend);
samples older than 15 seconds are stale. The existing hardware/software/cooling
objects remain available even when the memory collector fails.

The internal snapshot is five UTF-8 lines: `BC250_MEMORY_V1`, boot timestamp
in seconds, status, eight decimal raw words (empty for unavailable data), and
an error message (empty on success). The daemon validates its version, size,
timestamp and words, then generates JSON itself. This keeps the static C++
build free of a JSON parser dependency and prevents malformed input from
breaking the shared JSON. Consumers should read `/run/apu_telemetry.json`.

## Offline tests

```sh
python3 -m unittest discover -s tests -p 'test_memory.py' -v
bash tests/test_install_lib.sh
```

Tests use fake SMU/PCI/DMA interfaces and never patch real hardware.
