# BC-250 Telemetry Web Monitor

The web frontend for the `apu_telemetry` daemon: a small Python HTTP server
(`server.py`) that reads `/run/apu_telemetry.json` and serves it as raw JSON
plus two dashboard pages.

## How it works

1. `apu-telemetry.service` updates `/run/apu_telemetry.json` roughly every
   700 ms.
2. `bc250-web.service` runs `server.py` in the background, waiting for
   requests.
3. On each request, the server either returns that JSON as-is or serves a
   static dashboard file — it never touches the daemon directly.

## Endpoints and dashboards

- `/` — classic HUD (`index.html`), or a redirect to `/v2/` if
  `BC250_DEFAULT_DASHBOARD=v2` is set.
- `/v2/` — animated board diagram; always reachable regardless of the
  default.
- `/api/telemetry` — the raw contents of `/run/apu_telemetry.json`, served
  with CORS enabled.

## Install

Handled automatically by the top-level `./install.sh` (see the main
[README](../README.md)) — it fills in `bc250-web.service`'s user, working
directory, and default dashboard, then enables and starts the service.

## Configuration

Set as `Environment=` lines in `/etc/systemd/system/bc250-web.service` by
`install.sh`. Edit the unit and run `sudo systemctl daemon-reload` to change:

- `BC250_WEB_PORT` — port to listen on, default `8090`.
- `BC250_DEFAULT_DASHBOARD` — `v1` (classic, default) or `v2`; falls back to
  `v1` if unset, unrecognized, or if `v2/index.html` is missing.

## Checking it's running

```bash
sudo systemctl status bc250-web.service
```

Then open `http://localhost:8090` (or `http://<board-ip>:8090` from another
machine).

## Logs

```bash
journalctl -u bc250-web.service -f
```
