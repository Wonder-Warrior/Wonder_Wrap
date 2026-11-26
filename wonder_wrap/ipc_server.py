"""Localhost HTTP IPC server (127.0.0.1:47821) bridging the daemon and widget."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
from typing import Any, Callable

log = logging.getLogger("wonder_wrap.ipc")

DEFAULT_IPC_HOST = "127.0.0.1"
DEFAULT_IPC_PORT = 47821


class _LocalThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalIpcServer:
    """Background localhost HTTP JSON server bridging the replay daemon and UI widgets."""

    def __init__(
        self,
        get_status: Callable[[], dict[str, Any]],
        trigger_save: Callable[[], dict[str, Any]],
        trigger_stop: Callable[[], None],
        trigger_screenshot: Callable[[], dict[str, Any]] | None = None,
        trigger_mute: Callable[[], dict[str, Any]] | None = None,
        trigger_open_folder: Callable[[], dict[str, Any]] | None = None,
        trigger_choose_folder: Callable[[], dict[str, Any]] | None = None,
        trigger_record_toggle: Callable[[], dict[str, Any]] | None = None,
        get_settings: Callable[[], dict[str, Any]] | None = None,
        update_settings: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        trigger_ignore_active: Callable[[], dict[str, Any]] | None = None,
        trigger_update_check: Callable[[], dict[str, Any]] | None = None,
        trigger_update_install: Callable[[], dict[str, Any]] | None = None,
        host: str = DEFAULT_IPC_HOST,
        port: int = DEFAULT_IPC_PORT,
    ) -> None:
        self._get_status = get_status
        self._trigger_save = trigger_save
        self._trigger_stop = trigger_stop
        self._trigger_screenshot = trigger_screenshot
        self._trigger_mute = trigger_mute
        self._trigger_open_folder = trigger_open_folder
        self._trigger_choose_folder = trigger_choose_folder
        self._trigger_record_toggle = trigger_record_toggle
        self._get_settings = get_settings
        self._update_settings = update_settings
        self._trigger_ignore_active = trigger_ignore_active
        self._trigger_update_check = trigger_update_check
        self._trigger_update_install = trigger_update_install
        self.host = host
        self.port = port
        self._server: _LocalThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                # Silence per-request console/stderr logging (2s polling should stay quiet)
                pass

            def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> bytes:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length > 0:
                    return self.rfile.read(min(length, 65536))
                return b""

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                if path == "/status":
                    try:
                        data = parent._get_status()
                        self._send_json(200, data)
                    except Exception as exc:
                        log.exception("GET /status failed: %s", exc)
                        self._send_json(500, {"running": False, "error": str(exc)})
                elif path == "/settings" and parent._get_settings is not None:
                    try:
                        self._send_json(200, parent._get_settings())
                    except Exception as exc:
                        log.exception("GET /settings failed: %s", exc)
                        self._send_json(500, {"error": str(exc)})
                else:
                    self._send_json(404, {"error": "Not found"})

            def do_POST(self) -> None:
                raw_body = self._read_body()
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                if path == "/settings" and parent._update_settings is not None:
                    try:
                        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                        result = parent._update_settings(payload if isinstance(payload, dict) else {})
                        self._send_json(200, result)
                    except Exception as exc:
                        log.exception("POST /settings failed: %s", exc)
                        self._send_json(400, {"ok": False, "error": str(exc)})
                elif path == "/save":
                    try:
                        log.info("HTTP POST /save triggered")
                        result = parent._trigger_save()
                        code = 200 if result.get("ok") else 409
                        self._send_json(code, result)
                    except Exception as exc:
                        log.exception("POST /save failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/screenshot" and parent._trigger_screenshot is not None:
                    try:
                        log.info("HTTP POST /screenshot triggered")
                        result = parent._trigger_screenshot()
                        code = 200 if result.get("ok") else 409
                        self._send_json(code, result)
                    except Exception as exc:
                        log.exception("POST /screenshot failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/mute" and parent._trigger_mute is not None:
                    try:
                        result = parent._trigger_mute()
                        self._send_json(200, result)
                    except Exception as exc:
                        log.exception("POST /mute failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/open-folder" and parent._trigger_open_folder is not None:
                    try:
                        result = parent._trigger_open_folder()
                        self._send_json(200 if result.get("ok") else 500, result)
                    except Exception as exc:
                        log.exception("POST /open-folder failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/choose-folder" and parent._trigger_choose_folder is not None:
                    try:
                        result = parent._trigger_choose_folder()
                        self._send_json(200, result)
                    except Exception as exc:
                        log.exception("POST /choose-folder failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/record/toggle" and parent._trigger_record_toggle is not None:
                    try:
                        log.info("HTTP POST /record/toggle triggered")
                        result = parent._trigger_record_toggle()
                        self._send_json(200 if result.get("ok") else 409, result)
                    except Exception as exc:
                        log.exception("POST /record/toggle failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/ignore-active" and parent._trigger_ignore_active is not None:
                    try:
                        result = parent._trigger_ignore_active()
                        self._send_json(200 if result.get("ok") else 409, result)
                    except Exception as exc:
                        log.exception("POST /ignore-active failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/update/check" and parent._trigger_update_check is not None:
                    try:
                        self._send_json(200, parent._trigger_update_check())
                    except Exception as exc:
                        log.exception("POST /update/check failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/update/install" and parent._trigger_update_install is not None:
                    try:
                        result = parent._trigger_update_install()
                        self._send_json(200 if result.get("ok") else 409, result)
                    except Exception as exc:
                        log.exception("POST /update/install failed: %s", exc)
                        self._send_json(500, {"ok": False, "error": str(exc)})
                elif path == "/stop":
                    log.info("HTTP POST /stop triggered")
                    self._send_json(200, {"ok": True, "stopping": True})
                    # Signal stop slightly after response flushes
                    threading.Timer(0.05, parent._trigger_stop).start()
                else:
                    self._send_json(404, {"error": "Not found"})

        self._server = _LocalThreadingHTTPServer((self.host, self.port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="wonder-ipc-http",
            daemon=True,
        )
        self._thread.start()
        log.info("Local IPC HTTP server listening on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
            self._thread = None
