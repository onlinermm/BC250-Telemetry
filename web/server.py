import http.server
import socketserver
import json
import os

# CLAUDE.md documents port 8090 as the canonical port for this service.
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

    def do_GET(self):
        # Our own API endpoint — serves the JSON straight from memory
        if self.path == '/api/telemetry':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                with open('/run/apu_telemetry.json', 'r') as f:
                    self.wfile.write(f.read().encode())
            except Exception as e:
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == '/' and DEFAULT_DASHBOARD == 'v2':
            self.send_response(302)
            self.send_header('Location', '/v2/')
            self.end_headers()
        else:
            # Fall through to serving regular static files (index.html, style.css)
            super().do_GET()

print("=========================================")
print(f" Web server running on port {PORT}")
print(f" Open in your browser: http://localhost:{PORT}")
print(f" Default dashboard at \"/\": {DEFAULT_DASHBOARD}")
print("=========================================")

# Allow instant server restarts instead of waiting for the kernel to release the port
socketserver.TCPServer.allow_reuse_address = True

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
