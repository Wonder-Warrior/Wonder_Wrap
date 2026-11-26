"""Background GitHub Releases update checker and self-updater."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import tempfile
import threading
import time
import urllib.request

from .version import GITHUB_REPO, INSTALLER_ASSET_NAME, __version__

log = logging.getLogger("wonder_wrap.updater")

RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_INTERVAL_SEC = 6 * 3600


def _parse_ver(v: str) -> tuple[int, ...]:
    cleaned = v.strip().lstrip("vV")
    parts: list[int] = []
    for p in cleaned.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


class AutoUpdater:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_version: str = __version__
        self._download_url: str = ""
        self._update_available: bool = False
        self._downloading: bool = False
        self._last_error: str = ""

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="wonder-updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def get_state(self) -> dict:
        with self._lock:
            return {
                "version": __version__,
                "latest_version": self._latest_version,
                "update_available": self._update_available,
                "downloading": self._downloading,
            }

    def _loop(self) -> None:
        # Brief initial delay so startup stays instant
        if self._stop_event.wait(15.0):
            return
        while not self._stop_event.is_set():
            self.check_now()
            if self._stop_event.wait(CHECK_INTERVAL_SEC):
                break

    def check_now(self) -> dict:
        try:
            req = urllib.request.Request(
                RELEASES_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"WonderWrap/{__version__}",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            tag = str(data.get("tag_name") or "").strip().lstrip("vV")
            asset_url = ""
            for asset in data.get("assets", []):
                if str(asset.get("name", "")).lower() == INSTALLER_ASSET_NAME.lower():
                    asset_url = str(asset.get("browser_download_url") or "")
                    break

            is_newer = bool(tag and asset_url and _parse_ver(tag) > _parse_ver(__version__))
            with self._lock:
                self._latest_version = tag or __version__
                self._download_url = asset_url
                self._update_available = is_newer
                self._last_error = ""
            if is_newer:
                log.info("Update available: v%s -> v%s; auto-installing", __version__, tag)
                self.install_update()
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            log.debug("Update check skipped: %s", exc)
        return self.get_state()

    def install_update(self) -> dict:
        with self._lock:
            if self._downloading:
                return {"ok": False, "error": "Update already downloading."}
            url = self._download_url
            latest = self._latest_version
            avail = self._update_available
            if not avail or not url:
                return {"ok": False, "error": "No update available."}
            self._downloading = True

        threading.Thread(
            target=self._download_and_run,
            args=(url, latest),
            name="wonder-update-dl",
            daemon=True,
        ).start()
        return {"ok": True, "version": latest}

    def _download_and_run(self, url: str, version: str) -> None:
        dst = os.path.join(tempfile.gettempdir(), f"WonderWrap_Installer_{version}.exe")
        try:
            log.info("Downloading Wonder Wrap v%s...", version)
            req = urllib.request.Request(url, headers={"User-Agent": f"WonderWrap/{__version__}"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(dst, "wb") as out:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            log.info("Launching silent installer for v%s...", version)
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.ShellExecuteW(None, "runas", dst, "--silent", None, 1)
        except Exception as exc:
            log.warning("Auto-update failed: %s", exc)
        finally:
            with self._lock:
                self._downloading = False
