# BC-250 Telemetry Web Monitor

The web frontend for the `apu_telemetry` daemon: a small Python HTTP server
(`serve_socket.py` + `server.py`) that reads `/run/apu_telemetry.json` and
serves its contents as raw JSON plus two dashboard pages.

## How it works

1. `apu-telemetry.service` updates `/run/apu_telemetry.json` roughly every
   700 ms.
2. `bc250-web.service` runs `serve_socket.py` in the background, waiting for
   requests.
3. On each request, the server either returns that JSON as-is or serves a
   static dashboard file — it never touches the daemon directly.

`serve_socket.py` replaces the old `server.py` entrypoint: it binds the port
itself when started directly (identical behavior, quieter logs), or adopts
systemd's already-listening socket when spawned by `bc250-web.socket`.

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

- `BC250_WEB_PORT` — port to listen on, default `8090` (also set the same
  value in `bc250-web.socket` if you use on-demand mode).
- `BC250_DEFAULT_DASHBOARD` — `v1` (classic, default) or `v2`; falls back to
  `v1` if unset, unrecognized, or if `v2/index.html` is missing.
- `BC250_WEB_IDLE` — seconds of inactivity after which the server exits.
  `0` (default) keeps it running forever. Only useful in on-demand mode.

## Always-on vs on-demand

**Always-on** (the default after `install.sh`): the service runs all the
time.

```bash
sudo systemctl status bc250-web.service
```

**On-demand**: the server only runs while the dashboard is being viewed.
`bc250-web.socket` owns the port and spawns `bc250-web.service` on the first
request; after `BC250_WEB_IDLE` seconds of silence the server exits, the
socket keeps listening, and the next request starts it again.

```bash
sudo systemctl disable bc250-web.service
sudo systemctl edit bc250-web.service   # add: Environment=BC250_WEB_IDLE=300
sudo systemctl enable --now bc250-web.socket
```

Go back to always-on with:

```bash
sudo systemctl disable --now bc250-web.socket
sudo systemctl enable bc250-web.service
```

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

Successful requests are not logged — the v2 dashboard polls
`/api/telemetry` once a second while open, and one journal line per poll is
noise. Only errors (4xx/5xx), server startup and the idle shutdown are
written to the journal.