"""Local web dashboard: ``GET /`` serves the page, ``GET /api/state`` the live JSON state.

Standard library only. Bound to ``0.0.0.0`` by default so a phone on the same Wi-Fi can open
``http://<this PC's IP>:8765``. It is read-only: nothing on the page can change the scanner.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("dashboard.html")
NEAR_PAGE = Path(__file__).with_name("near_misses.html")


def make_handler(
    get_state: Callable[[], dict[str, Any]],
    page: bytes,
    get_history: Callable[[], Any] | None = None,
    get_near_misses: Callable[[], Any] | None = None,
    near_page: bytes | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "odds-scanner"

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/near-misses" and near_page is not None and get_near_misses is not None:
                self._send(200, "text/html; charset=utf-8", near_page)
            elif path in ("/api/state", "/api/history") or (path == "/api/near-misses" and get_near_misses is not None):
                if path == "/api/state":
                    source = get_state
                elif path == "/api/history":
                    source = get_history or (lambda: [])
                else:
                    source = get_near_misses  # type: ignore[assignment]
                try:
                    body = json.dumps(source(), ensure_ascii=False).encode("utf-8")
                except Exception:  # noqa: BLE001
                    log.exception("building dashboard state failed")
                    self._send(500, "text/plain; charset=utf-8", b"internal error")
                    return
                self._send(200, "application/json; charset=utf-8", body)
            elif path == "/favicon.ico":
                self._send(204, "text/plain", b"")
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")

        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet access log
            log.debug("dashboard %s - %s", self.address_string(), format % args)

    return Handler


class Dashboard:
    def __init__(
        self,
        get_state: Callable[[], dict[str, Any]],
        host: str = "0.0.0.0",
        port: int = 8765,
        get_history: Callable[[], Any] | None = None,
        get_near_misses: Callable[[], Any] | None = None,
    ) -> None:
        near_page = NEAR_PAGE.read_bytes() if get_near_misses is not None else None
        self._server = ThreadingHTTPServer(
            (host, port), make_handler(get_state, PAGE.read_bytes(), get_history, get_near_misses, near_page)
        )
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def lan_ip() -> str | None:
    """This machine's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 9))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None
