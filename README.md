# BC-250 Telemetry

A telemetry daemon and web dashboard for the AMD BC-250 (Oberon / Cyan
Skillfish, `gfx1013`, PCI ID `1002:13fe`) — the decommissioned mining-board
APU carved out of the PS5. Runs on Bazzite, SteamOS, and CachyOS from the
same binary.

It merges two data sources into one JSON snapshot every ~700 ms:

- **Hardware (PMBus over I2C):** per-rail voltage, current, power, and
  temperature for the CPU and GPU rails, read straight from the VRM/PMIC.
  The chip address (`0x60`) is fixed in hardware; the I2C bus number isn't,
  so the daemon scans `/dev/i2c-*` and finds it automatically.
- **Software (Linux hwmon/sysfs):** die temperatures, clocks, power draw
  (PPT), and fan RPM/PWM, via the `amdgpu`, `k10temp`, `nct6686`, and `nvme`
  hwmon directories.

Everything is written atomically to `/run/apu_telemetry.json`, served by a
small bundled web server (`web/server.py`) on port 8090 with two dashboard
variants — `/` (classic HUD) and `/v2/` (animated board diagram), both
always reachable regardless of which one is the default.

If the I2C bus isn't found, the daemon doesn't crash-loop — it logs the
issue, retries every ~10 s, and keeps serving everything that doesn't depend
on I2C (CPU/GPU clocks, temperatures, fans).

## Quick start

Download the latest release from
[GitHub Releases](../../releases/latest), extract it, and run:

```bash
cd bc250-telemetry
sudo ./install.sh
```

`install.sh` compiles the daemon (or falls back to the prebuilt
`apu_telemetry` binary next to it), sets up the `nct6683` fan-controller
module, installs both systemd units, and asks which dashboard to serve by
default at `/` — classic (`v1`) or the animated diagram (`v2`); the other
stays reachable either way. To skip the prompt (e.g. for a scripted
install):

```bash
sudo ./install.sh --dashboard=v2   # or v1
```

Check that it's running:

```bash
sudo systemctl status apu-telemetry bc250-web.service
cat /run/apu_telemetry.json | jq .
```

Web UI:

```
http://<board-ip>:8090/       — classic HUD
http://<board-ip>:8090/v2/    — animated board diagram
```

Update (re-run over an already-running install):

```bash
sudo ./install.sh
```

Uninstall:

```bash
sudo ./uninstall.sh
```

<details>
<summary>Manual install, step by step</summary>

```bash
# Fan controller module (not autodetected on this board)
sudo sh -c 'echo "nct6683" > /etc/modules-load.d/99-sensors.conf'
sudo sh -c 'echo "options nct6683 force=true" > /etc/modprobe.d/sensors.conf'
sudo modprobe nct6683 force=true

# Daemon
g++ -O2 -static bc250_telemetry.cpp -o ./apu_telemetry
sudo cp apu_telemetry /usr/local/bin/
sed "s|TELEMETRY_BIN_PATH|/usr/local/bin/apu_telemetry|" apu-telemetry.service \
    | sudo tee /etc/systemd/system/apu-telemetry.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now apu-telemetry
```
</details>

## I2C bus override

The daemon scans `/dev/i2c-*` on its own and uses whichever one answers at
the PMIC's address (`0x60`). To force a specific bus instead:

```bash
./apu_telemetry --bus 4
# or
BC250_I2C_BUS=4 ./apu_telemetry
```

## JSON output format

```json
{
  "hardware": {
    "cpu": { "valid": true, "vin": 12.22, "vout": 0.780, "iout": 2.8, "pout": 2.2, "temp": 45.0 },
    "gpu": { "valid": true, "vin": 12.22, "vout": 0.646, "iout": 12.0, "pout": 7.8, "temp": 48.0 },
    "total_power": 10.0,
    "total_power_valid": true
  },
  "software": {
    "cpu_temp_c": 51.4,
    "cpu_freq_mhz": 3400,
    "gpu_temp_c": 47.0,
    "gpu_sclk_mhz": 400,
    "gpu_ppt_w": 32.1,
    "nvme_temp_c": 38.0,
    "nct_t14_c": -1.0,
    "nct_t15_c": -1.0
  },
  "cooling": {
    "fan_rpm": 945,
    "fan_pwm_pct": 28
  }
}
```

- `cpu_freq_mhz` is the average across all CPU cores, not one arbitrary core.
- `total_power_valid` is `false` when the CPU or GPU VRM reading is currently
  invalid — `total_power` then reflects only the working side, not a real zero.
- Any field can be `-1` if that particular sensor isn't available; everything
  else keeps working independently.
</content>
