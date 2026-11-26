"""
Woneder Instant Replay — Ultra-Low-RAM Windows Background Daemon (Milestone 1).

Usage:
  Silent background mode (no console window):
      pythonw.exe replay_daemon.py
      (or double-click start_replay_silent.vbs)

  Foreground with live terminal logs:
      python replay_daemon.py

  Control a running daemon:
      python replay_daemon.py --save     # Trigger an instant save via IPC
      python replay_daemon.py --stop     # Gracefully stop the background daemon
      python replay_daemon.py --status   # Show recent daemon log & save confirmations
"""

from __future__ import annotations

import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
import time

from wonder_wrap.hotkey import (
    SAVE_EVENT_NAME,
    STOP_EVENT_NAME,
    GlobalHotkeyLoop,
    acquire_single_instance_mutex,
    signal_named_event,
)
from wonder_wrap.ipc_server import DEFAULT_IPC_HOST, DEFAULT_IPC_PORT, LocalIpcServer
from wonder_wrap.replay_engine import ReplayConfig, ReplayEngine
from wonder_wrap.win32_dxgi import (
    get_thread_desktop_name,
    get_videos_dir,
    load_user_settings,
)


def setup_logging(log_path: str, verbose: bool = False) -> logging.Logger:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Attach console handler only when stdout exists (skipped cleanly under pythonw.exe)
    if sys.stdout is not None:
        try:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(fmt)
            root.addHandler(stream_handler)
        except Exception:
            pass

    return logging.getLogger("woneder.daemon")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Woneder Instant Replay Background Daemon")
    p.add_argument("--seconds", type=int, default=30, help="Rolling replay buffer length in seconds (default: 30)")
    p.add_argument("--segment", type=int, default=2, help="MPEG-TS segment duration in seconds (default: 2)")
    p.add_argument("--fps", type=int, default=60, help="Target framerate (default: 60)")
    p.add_argument("--bitrate", type=str, default="18M", help="Target NVENC video bitrate (default: 18M)")
    p.add_argument("--output-dir", type=str, default="", help="Output folder (default: Videos/Replays)")
    p.add_argument("--adapter", type=int, default=None, help="Override DXGI adapter index for ddagrab")
    p.add_argument("--output-idx", type=int, default=None, help="Override DXGI monitor output index")
    p.add_argument("--ipc-port", type=int, default=DEFAULT_IPC_PORT, help=f"Localhost HTTP IPC port (default: {DEFAULT_IPC_PORT})")
    p.add_argument("--always-record", action="store_true", help="Record desktop even when no game is detected")
    p.add_argument("--stop", action="store_true", help="Stop the currently running background daemon")
    p.add_argument("--save", action="store_true", help="Trigger an immediate replay save on the running daemon")
    p.add_argument("--status", action="store_true", help="Print the tail of the daemon log")
    p.add_argument("--verbose", action="store_true", help="Enable debug logging (includes FFmpeg stderr)")
    p.add_argument(
        "--synthetic-test",
        action="store_true",
        help="Use testsrc2 video instead of ddagrab (for headless/automated verification only)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    saved_settings = load_user_settings()
    replays_dir = (
        args.output_dir
        or saved_settings.get("output_dir")
        or os.path.join(get_videos_dir(), "Replays")
    )
    log_path = os.path.join(replays_dir, "wonder_wrap.log")

    if args.stop:
        if signal_named_event(STOP_EVENT_NAME):
            print("Stop signal sent to Woneder Instant Replay daemon.")
            return 0
        print("No running Woneder Instant Replay daemon found.")
        return 1

    if args.save:
        if signal_named_event(SAVE_EVENT_NAME):
            print(f"Save signal sent! Check {replays_dir} (log: {log_path}).")
            return 0
        print("No running Woneder Instant Replay daemon found.")
        return 1

    if args.status:
        if not os.path.isfile(log_path):
            print(f"No log file found at {log_path}")
            return 1
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-25:]
        print(f"=== Last {len(lines)} lines of {log_path} ===")
        print("".join(lines), end="")
        return 0

    log = setup_logging(log_path, verbose=args.verbose)

    mutex = acquire_single_instance_mutex()
    if mutex is None:
        log.warning("Another instance of Woneder Instant Replay is already running. Exiting.")
        return 1

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    engine: ReplayEngine | None = None
    ipc_server: LocalIpcServer | None = None
    try:
        desktop_name = get_thread_desktop_name()
        log.info("=== Wonder Wrap Daemon starting (PID=%d, desktop=%s) ===", os.getpid(), desktop_name)
        if not args.synthetic_test and desktop_name.lower() != "default":
            log.warning(
                "Process is running on non-interactive desktop '%s' instead of 'Default'. "
                "DXGI ddagrab requires launching from your normal Windows user desktop.",
                desktop_name,
            )

        saved_buf = int(saved_settings.get("buffer_sec", args.seconds))
        saved_fps = int(saved_settings.get("fps", args.fps))
        saved_vbr = str(saved_settings.get("video_bitrate", args.bitrate))
        saved_abr = str(saved_settings.get("audio_bitrate", "192k"))
        saved_game_only = bool(saved_settings.get("game_only", True))
        saved_cursor = bool(saved_settings.get("capture_cursor", True))
        saved_hotkey = str(saved_settings.get("hotkey", "Alt+F10"))

        vbr_num = int(saved_vbr[:-1]) if saved_vbr.upper().endswith("M") and saved_vbr[:-1].isdigit() else 18
        cfg = ReplayConfig(
            buffer_seconds=saved_buf if args.seconds == 30 else args.seconds,
            segment_seconds=args.segment,
            fps=saved_fps if args.fps == 60 else args.fps,
            video_bitrate=f"{vbr_num}M",
            video_maxrate=f"{vbr_num + 4}M",
            video_bufsize=f"{vbr_num}M",
            audio_bitrate=saved_abr,
            output_dir=replays_dir,
            adapter_index=args.adapter,
            output_index=args.output_idx,
            game_only=(False if (args.always_record or args.synthetic_test) else saved_game_only),
            capture_cursor=saved_cursor,
            hotkey=saved_hotkey,
            synthetic_test=args.synthetic_test,
        )
        engine = ReplayEngine(cfg)
        engine.start()

        state_lock = threading.Lock()
        last_saved_state = {"filename": "", "path": ""}

        # Log initial RAM footprint after FFmpeg warms up
        def _log_ram(prefix: str = "RAM heartbeat") -> None:
            assert engine is not None
            d_mb, f_mb, tot_mb = engine.get_ram_stats()
            budget_tag = "OK (<150 MB)" if tot_mb < 150.0 else "OVER 150 MB!"
            log.info(
                "%s | Daemon: %.1f MB | FFmpeg: %.1f MB | Total: %.1f MB [%s]",
                prefix,
                d_mb,
                f_mb,
                tot_mb,
                budget_tag,
            )

        def _warmup_ram_log() -> None:
            time.sleep(3.0)
            if engine is not None:
                _log_ram("Startup RAM (3s warmup)")

        threading.Thread(target=_warmup_ram_log, daemon=True).start()

        def _perform_save() -> dict:
            assert engine is not None
            res = engine.save_replay()
            if res.ok:
                filename = os.path.basename(res.output_path)
                with state_lock:
                    last_saved_state["filename"] = filename
                    last_saved_state["path"] = res.output_path
                log.info(
                    "SAVED REPLAY -> %s | ~%.1fs (%d segs) | %.2f MB | remux=%.0f ms | "
                    "RAM: daemon=%.1f MB + ffmpeg=%.1f MB = %.1f MB total",
                    res.output_path,
                    res.approx_duration_sec,
                    res.segments_used,
                    res.size_mb,
                    res.remux_ms,
                    res.daemon_ram_mb,
                    res.ffmpeg_ram_mb,
                    res.total_ram_mb,
                )
                return {
                    "ok": True,
                    "filename": filename,
                    "path": res.output_path,
                    "duration_sec": round(res.approx_duration_sec, 1),
                    "size_mb": round(res.size_mb, 2),
                    "remux_ms": round(res.remux_ms, 0),
                    "ram_mb": round(res.total_ram_mb, 1),
                }
            log.warning("Save replay skipped/failed: %s", res.error)
            return {
                "ok": False,
                "filename": "",
                "error": res.error,
                "ram_mb": round(res.total_ram_mb, 1),
            }

        def _on_save_trigger() -> None:
            threading.Thread(target=_perform_save, name="replay-save", daemon=True).start()

        def _get_ipc_status() -> dict:
            assert engine is not None
            d_mb, f_mb, tot_mb = engine.get_ram_stats()
            with state_lock:
                last_saved = last_saved_state["filename"]
            return {
                "running": True,
                "mode": engine.mode,
                "game": engine.active_game_name,
                "encoder": engine.encoder.codec,
                "output_dir": engine.cfg.output_dir,
                "recording_sec": round(engine.recording_elapsed_sec, 1),
                "ram_mb": round(tot_mb, 1),
                "daemon_ram_mb": round(d_mb, 1),
                "ffmpeg_ram_mb": round(f_mb, 1),
                "buffer_sec": cfg.buffer_seconds,
                "muted": engine.muted,
                "last_saved": last_saved,
            }

        def _trigger_screenshot() -> dict:
            assert engine is not None
            return engine.save_screenshot()

        def _trigger_mute() -> dict:
            assert engine is not None
            return {"ok": True, "muted": engine.toggle_mute()}

        def _trigger_open_folder() -> dict:
            assert engine is not None
            return {"ok": engine.open_captures_folder(), "path": engine.cfg.output_dir}

        def _trigger_choose_folder() -> dict:
            assert engine is not None
            return engine.choose_output_dir()

        def _trigger_record_toggle() -> dict:
            assert engine is not None
            res = engine.toggle_recording()
            if res.get("ok") and res.get("filename"):
                with state_lock:
                    last_saved_state["filename"] = res["filename"]
                    last_saved_state["path"] = res.get("path", "")
            return res

        def _trigger_stop_from_ipc() -> None:
            signal_named_event(STOP_EVENT_NAME)

        loop = GlobalHotkeyLoop(
            on_save=_on_save_trigger,
            on_periodic_tick=lambda: _log_ram("RAM heartbeat"),
            tick_interval_sec=60.0,
            hotkey_name=cfg.hotkey,
        )

        def _get_settings() -> dict:
            assert engine is not None
            return engine.get_settings_dict()

        def _update_settings(updates: dict) -> dict:
            assert engine is not None
            res = engine.apply_settings(updates)
            if res.get("ok") and "hotkey" in res.get("settings", {}):
                loop.set_hotkey(res["settings"]["hotkey"])
            return res

        def _trigger_ignore_active() -> dict:
            assert engine is not None
            return engine.ignore_active_game()

        ipc_server = LocalIpcServer(
            get_status=_get_ipc_status,
            trigger_save=_perform_save,
            trigger_stop=_trigger_stop_from_ipc,
            trigger_screenshot=_trigger_screenshot,
            trigger_mute=_trigger_mute,
            trigger_open_folder=_trigger_open_folder,
            trigger_choose_folder=_trigger_choose_folder,
            trigger_record_toggle=_trigger_record_toggle,
            get_settings=_get_settings,
            update_settings=_update_settings,
            trigger_ignore_active=_trigger_ignore_active,
            host=DEFAULT_IPC_HOST,
            port=args.ipc_port,
        )
        try:
            ipc_server.start()
        except OSError as exc:
            log.warning("Could not start local IPC HTTP server on %s:%d: %s", DEFAULT_IPC_HOST, args.ipc_port, exc)
            ipc_server = None

        log.info(
            "Ready! Buffering last %ds -> press %s or POST http://%s:%d/save to save to %s (Ctrl+Alt+F12 or --stop to exit).",
            cfg.buffer_seconds,
            cfg.hotkey,
            DEFAULT_IPC_HOST,
            args.ipc_port,
            cfg.output_dir,
        )
        loop.run()
        return 0
    finally:
        if ipc_server is not None:
            ipc_server.stop()
        if engine is not None:
            engine.stop()
        if mutex:
            k32.CloseHandle(mutex)
        log.info("=== Woneder Instant Replay Daemon exited cleanly ===")


if __name__ == "__main__":
    raise SystemExit(main())
