#!/usr/bin/env python3
# BC-250 dashboard web server. One unit, two ways to run:
#
#   * Always-on (daemon): started directly, this binds the port itself --
#     the same behavior as the old server.py entrypoint, but with quieter
#     access logs and an optional idle exit for on-demand setups.
#   * On demand: run under web/bc250-web.socket so systemd owns the listen
#     socket and hands it to this process on fd 3 (LISTEN_FDS=1); nothing is
#     bound, served or logged until traffic arrives. After BC250_WEB_IDLE
#     seconds without a request the process exits, the socket keeps listening,
#     and the next connection spawns it again.
#
# Successful requests are not logged: the v2 dashboard polls /api/telemetry
# once a second while open, and one journal line per poll is noise. Only
# errors (4xx/5xx), startup and the idle shutdown reach the journal.
import os
import socket
import threading
import time
import http.server

import server as app

# Seconds of inactivity before the server closes itself. 0 = never (always-on).
IDLE_TIMEOUT = float(os.environ.get("BC250_WEB_IDLE", "0"))


class IdleHandler(app.Handler):
    """Dashboard handler: quiet access logs, activity stamp for idle exit."""

    last_activity = time.monotonic()

    def log_message(self, format, *args):
        # http.server sends 4xx/5xx through log_error(); log_message only sees
        # 2xx/3xx -- mostly the v2 dashboard's once-a-second telemetry poll.
        # Successful requests stay out of the journal entirely.
        return

    def handle(self):
        IdleHandler.last_activity = time.monotonic()
        super().handle()


class IdleHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded dashboard server with quiet logging and idle shutdown."""

    def __init__(self, server_address, bind_and_activate):
        super().__init__(server_address, IdleHandler,
                         bind_and_activate=bind_and_activate)


def _adopt_fd():
    # systemd socket activation: the first socket is inherited on fd 3.
    httpd = IdleHTTPServer(("", 0), bind_and_activate=False)
    sock = socket.socket(fileno=3)
    httpd.socket = sock
    httpd.server_address = sock.getsockname()[:2]
    return httpd, "systemd socket activation"


def _bind_self():
    return IdleHTTPServer(("", app.PORT), bind_and_activate=True), \
        f"self-bound port {app.PORT}"


def idle_watchdog(httpd):
    while True:
        time.sleep(5)
        if time.monotonic() - IdleHandler.last_activity > IDLE_TIMEOUT:
            print(f"No requests for {IDLE_TIMEOUT:.0f}s -- closing the "
                  f"dashboard.", flush=True)
            httpd.shutdown()
            return


def main():
    if int(os.environ.get("LISTEN_FDS", "0") or "0") >= 1:
        httpd, mode = _adopt_fd()
    else:
        httpd, mode = _bind_self()
    print(f"Serving BC-250 dashboard ({mode})", flush=True)
    if IDLE_TIMEOUT > 0:
        print(f"Going idle {IDLE_TIMEOUT:.0f}s after the last request.",
              flush=True)
        threading.Thread(target=idle_watchdog, args=(httpd,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.", flush=True)


if __name__ == "__main__":
    main()