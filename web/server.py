import http.server
import socketserver
import json
import os

import topology
import overclock
import telemetry

PORT = int(os.environ.get("BC250_WEB_PORT", 8090))
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

# Which dashboard to serve at "/": v1 (classic, default) or v2
# (animated board diagram). We check this ONCE at startup, not on every
# request — and deliberately verify that v2/index.html actually exists on
# disk: if the env var is garbled, stale (an old build without v2/), or
# just unrecognized, we quietly fall back to v1 instead of 404ing or
# crashing on "/".
_requested_dashboard = os.environ.get("BC250_DEFAULT_DASHBOARD", "v1").strip().lower()
if _requested_dashboard == "v2":
    if os.path.isfile(os.path.join(DIRECTORY, "v2", "index.html")):
        DEFAULT_DASHBOARD = "v2"
    else:
        print(f"[WARN] BC250_DEFAULT_DASHBOARD=v2, but {DIRECTORY}/v2/index.html was not found — falling back to v1.")
        DEFAULT_DASHBOARD = "v1"
elif _requested_dashboard == "v1":
    DEFAULT_DASHBOARD = "v1"
else:
    print(f"[WARN] Unrecognized BC250_DEFAULT_DASHBOARD={_requested_dashboard!r} — falling back to v1.")
    DEFAULT_DASHBOARD = "v1"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def _send_json(self, payload, status=200, *, allow_nan=True):
        # Serialize BEFORE sending headers: if json.dumps raises (e.g. a
        # non-finite value under allow_nan=False), we must not have already
        # committed a 200 with no body. Sending Content-Length also lets
        # clients detect a truncated response.
        try:
            body = json.dumps(payload, allow_nan=allow_nan).encode()
        except ValueError:
            status = 503
            body = json.dumps({'error': 'telemetry contained non-finite values'}).encode()
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # The daemon owns the complete snapshot, including memory telemetry.
        if self.path == '/api/telemetry':
            snapshot = telemetry.read_telemetry()
            # A missing/corrupt snapshot degrades to an error object; report
            # it as 503 so monitoring can tell a broken daemon from a healthy
            # one. The frontend already treats any error body as a failure.
            self._send_json(snapshot, 503 if 'error' in snapshot else 200, allow_nan=False)
        # Core/CU counts: queried fresh per request, not cached — the
        # frontend only calls this once per page load, so there's no need to
        # cache it server-side, and a fresh read means a live WGP/core
        # unlock shows up correctly if the page happens to be reloaded.
        elif self.path == '/api/topology':
            self._send_json(topology.read_topology())
        # Same once-per-page-load cadence as /api/topology — this is config
        # state (an OC file edit, a service (de)activation), not telemetry.
        elif self.path == '/api/overclock':
            self._send_json(overclock.read_overclock())
        elif self.path == '/' and DEFAULT_DASHBOARD == 'v2':
            self.send_response(302)
            self.send_header('Location', '/v2/')
            self.end_headers()
        else:
            # Fall through to serving regular static files (index.html, style.css)
            super().do_GET()

def main():
    print("=========================================")
    print(f" Web server running on port {PORT}")
    print(f" Open in your browser: http://localhost:{PORT}")
    print(f" Default dashboard at \"/\": {DEFAULT_DASHBOARD}")
    print("=========================================")

    # Allow instant server restarts instead of waiting for the kernel to release the port
    socketserver.TCPServer.allow_reuse_address = True
    # Threaded so a slow endpoint (topology/overclock shell out to systemctl)
    # cannot stall the fast /api/telemetry poll for every other client.
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


if __name__ == '__main__':
    main()
