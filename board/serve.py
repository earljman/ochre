#!/usr/bin/env python3
"""Read-only static server for the rendered board (no sign-in, no write API).

For a main home reachable only on loopback or a Tailnet. Binds `bind` (default 127.0.0.1) on `port`
from the board config; refuses a wildcard address so the page is never exposed on every interface.
Serves the directory that holds the generated page (config `out`).
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import config as C
import functools, ipaddress, os, socket, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    server_version = "ochre/1"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        super().end_headers()

    def list_directory(self, path):  # never list the output folder
        self.send_error(404)

    def log_message(self, *a):
        pass


def check_bind(addr):
    """The address to bind, or a ValueError when it would listen on every interface."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        raise ValueError(f"bind must be an IP address, not {addr!r}")
    if ip.is_unspecified:
        raise ValueError("refusing to bind a wildcard address; use 127.0.0.1 or a Tailnet address")
    return addr


def main():
    try:
        addr, port = check_bind(C.get("bind")), int(C.get("port"))
    except ValueError as e:
        sys.exit(f"serve.py: {e}")
    root = os.path.dirname(os.path.abspath(C.get("out")))
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in addr else socket.AF_INET

    Server((addr, port), functools.partial(Handler, directory=root)).serve_forever()


if __name__ == "__main__":
    main()
