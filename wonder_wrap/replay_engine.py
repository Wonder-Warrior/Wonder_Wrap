"""
Core Instant Replay Engine:
- Runs headless FFmpeg capturing DXGI Desktop Duplication (ddagrab + d3d11va)
  + WASAPI loopback desktop audio -> NVIDIA NVENC (h264_nvenc) -> rolling 2-second
  MPEG-TS segments in a temp directory (zero raw frame RAM buffering).
- On save trigger, atomically hard-links the last 30s of completed .ts segments
  and remuxes them into Videos/Replays/replay_YYYYMMDD_HHMMSS.mp4 with `-c copy`
  (no re-encoding, zero recording interruption).
"""

from __future__ import annotations

from collections import deque
import dataclasses
import datetime
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from .game_detector import GameDetector, GameWindowInfo
from .hotkey import parse_hotkey
from .wasapi_loopback import AudioFormat, WasapiLoopbackStreamer, probe_default_loopback_format
from .win32_dxgi import (
    DisplayTarget,
    EncoderProfile,
    detect_best_video_encoder,
    detect_nvidia_adapter_index,
    detect_primary_display_adapter,
    find_ffmpeg_binary,
    get_process_memory_mb,
    get_settings_path,
    get_videos_dir,
    pick_folder_dialog,
    save_user_settings,
)

log = logging.getLogger("woneder.engine")

CREATE_NO_WINDOW = 0x08000000


@dataclasses.dataclass
class ReplayConfig:
    buffer_seconds: int = 30
    segment_seconds: int = 2
    fps: int = 60
    video_bitrate: str = "18M"
    video_maxrate: str = "20M"
    video_bufsize: str = "18M"
    nvenc_preset: str = "p4"
    nvenc_cq: int = 19
    audio_bitrate: str = "192k"
    output_dir: str = ""
    adapter_index: int | None = None
    output_index: int | None = None
    game_only: bool = True
    capture_cursor: bool = True
    hotkey: str = "Alt+F10"
    alt_tab_grace_sec: float = 10.0
    synthetic_test: bool = False  # Only for headless verification without desktop access


@dataclasses.dataclass
class SaveResult:
    ok: bool
    output_path: str
    segments_used: int
    approx_duration_sec: float
    size_mb: float
    remux_ms: float
    daemon_ram_mb: float
    ffmpeg_ram_mb: float
    total_ram_mb: float
    error: str = ""


class ReplayEngine:
    def __init__(self, config: ReplayConfig) -> None:
        self.cfg = config
        self.ffmpeg_path = find_ffmpeg_binary("ffmpeg")
        self.display: DisplayTarget = detect_primary_display_adapter()
        if self.cfg.adapter_index is not None:
            self.display.adapter_index = self.cfg.adapter_index
        if self.cfg.output_index is not None:
            self.display.output_index = self.cfg.output_index
        self.nv_adapter_index, self.nv_adapter_name = detect_nvidia_adapter_index()
        self.encoder: EncoderProfile = detect_best_video_encoder()

        if not self.cfg.output_dir:
            self.cfg.output_dir = os.path.join(get_videos_dir(), "Replays")
        os.makedirs(self.cfg.output_dir, exist_ok=True)

        self.audio_fmt: AudioFormat = probe_default_loopback_format()
        self.buffer_dir = tempfile.mkdtemp(prefix=f"woneder_replay_buf_{os.getpid()}_")
        self.playlist_path = os.path.join(self.buffer_dir, "buffer.m3u8")

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._wasapi: WasapiLoopbackStreamer | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._watchdog_thread: threading.Thread | None = None
        self._game_monitor_thread: threading.Thread | None = None
        self._save_in_progress = threading.Lock()
        self._mode_lock = threading.Lock()
        self._muted = False
        self._mode: str = "idle" if (self.cfg.game_only and not self.cfg.synthetic_test) else "replay"
        self._active_game: GameWindowInfo | None = None
        self._game_lost_since: float = 0.0
        self._detector = GameDetector(
            self.display.width,
            self.display.height,
            os.path.dirname(get_settings_path()),
        )
        self._manual_rec_ts: str = ""
        self._manual_rec_started_at: float = 0.0

    def _build_video_source_and_filter(self, use_d3d11_nvenc: bool) -> tuple[str, str]:
        """
        Build video capture source + filter matching native screen resolution:
        - When a game window is active: uses FFmpeg 9.1 `gfxcapture` (Windows Graphics Capture)
          locked to `hwnd=<game.hwnd>` so ONLY the game window is recorded even when unfocused
          or covered by other windows, scaled cleanly to native screen resolution on GPU.
        - When idle / desktop manual capture: uses full-screen `ddagrab`.
        """
        sw = max(2, self.display.width & ~1)
        sh = max(2, self.display.height & ~1)
        eff_fps = self.cfg.fps if self.cfg.fps > 0 else 60

        if self.cfg.synthetic_test:
            vsrc = f"testsrc2=size={sw}x{sh}:rate={eff_fps},format=bgra"
            vf = "format=nv12,hwupload" if use_d3d11_nvenc else "format=nv12"
            return vsrc, vf

        game = self._active_game
        cursor_flag = 1 if self.cfg.capture_cursor else 0
        if game is not None and game.hwnd:
            # Windows Graphics Capture (WGC) locked to ONLY the game window HWND
            # regardless of which window is currently in focus or covering it.
            # When fps == 0, set high ceiling (240) + VFR so WGC captures every frame at the game's native FPS.
            fps_opt = f":max_framerate={self.cfg.fps}" if self.cfg.fps > 0 else ":max_framerate=240"
            vsrc = (
                f"gfxcapture=hwnd={game.hwnd}"
                f"{fps_opt}"
                f":capture_cursor={cursor_flag}"
                f":capture_border=0"
                f":display_border=0"
                f":width={sw}"
                f":height={sh}"
                f":resize_mode=scale_aspect"
                f":scale_mode=bilinear"
                f":output_fmt=bgra"
                f",hwdownload,format=bgra"
            )
            vf = "format=nv12,hwupload" if use_d3d11_nvenc else "format=nv12"
            return vsrc, vf

        vsrc = (
            f"ddagrab=output_idx={self.display.output_index}"
            f":framerate={eff_fps}"
            f":draw_mouse={cursor_flag}"
            f":output_fmt=bgra"
            f":allow_fallback=1"
            f",hwdownload,format=bgra"
        )
        vf = "format=nv12,hwupload" if use_d3d11_nvenc else "format=nv12"
        return vsrc, vf

    def _build_video_encoder_args(self, gop: int, force_keyframes: bool) -> list[str]:
        """Build optimal H.264 encoder flags for NVIDIA, AMD, Intel, or CPU fallback."""
        codec = self.encoder.codec
        if codec == "h264_nvenc":
            args = [
                "-c:v", "h264_nvenc",
                "-preset", self.cfg.nvenc_preset,
                "-tune", "ll",
                "-rc", "vbr",
                "-cq", str(self.cfg.nvenc_cq),
                "-b:v", self.cfg.video_bitrate,
                "-maxrate", self.cfg.video_maxrate,
                "-bufsize", self.cfg.video_bufsize,
                "-bf", "0",
                "-surfaces", "4",
                "-rc-lookahead", "0",
            ]
        elif codec == "h264_amf":
            args = [
                "-c:v", "h264_amf",
                "-quality", "speed",
                "-rc", "vbr_latency",
                "-b:v", self.cfg.video_bitrate,
                "-maxrate", self.cfg.video_maxrate,
                "-bf", "0",
            ]
        elif codec == "h264_qsv":
            args = [
                "-c:v", "h264_qsv",
                "-preset", "veryfast",
                "-b:v", self.cfg.video_bitrate,
                "-maxrate", self.cfg.video_maxrate,
                "-bf", "0",
            ]
        elif codec == "h264_mf":
            args = [
                "-c:v", "h264_mf",
                "-rate_control", "cbr",
                "-b:v", self.cfg.video_bitrate,
            ]
        else:
            args = [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-b:v", self.cfg.video_bitrate,
                "-bf", "0",
            ]

        args += ["-g", str(gop)]
        if force_keyframes:
            args += ["-force_key_frames", f"expr:gte(t,n_forced*{self.cfg.segment_seconds})"]
        if self.cfg.fps > 0:
            args += ["-fps_mode", "cfr", "-r", str(self.cfg.fps)]
        else:
            # fps == 0 -> match the game's native frame pacing (VFR)
            args += ["-fps_mode", "vfr"]
        args += [
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-color_range", "tv",
        ]
        return args

    def build_recorder_cmd(self) -> list[str]:
        eff_fps = self.cfg.fps if self.cfg.fps > 0 else 60
        gop = eff_fps * self.cfg.segment_seconds
        # buffer_seconds == 0 -> record entire game session (keep all segments until game exits)
        full_session = self.cfg.buffer_seconds == 0
        list_size = 0 if full_session else (self.cfg.buffer_seconds // self.cfg.segment_seconds) + 2
        seg_pattern = os.path.join(self.buffer_dir, "seg_%06d.ts")

        use_d3d11_nvenc = self.nv_adapter_index is not None

        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            # Keep FFmpeg filter/frame thread queues minimal for ultra-low RAM (<150 MB total)
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
        ]

        if use_d3d11_nvenc:
            # Bind NVIDIA dGPU D3D11 device so h264_nvenc runs over D3D11 instead of CUDA
            # (cuts FFmpeg Working Set by ~100 MB by avoiding nvcuda.dll context overhead)
            cmd += [
                "-init_hw_device",
                f"d3d11va=nv:{self.nv_adapter_index}",
                "-filter_hw_device",
                "nv",
            ]

        # Input 0: Live Windows WASAPI desktop audio loopback over stdin pipe
        cmd += [
            "-probesize",
            "32768",
            "-analyzeduration",
            "0",
            "-f",
            self.audio_fmt.ffmpeg_fmt,
            "-ar",
            str(self.audio_fmt.sample_rate),
            "-ac",
            str(self.audio_fmt.channels),
            "-i",
            "pipe:0",
        ]

        video_src, vf = self._build_video_source_and_filter(use_d3d11_nvenc)
        cmd += [
            "-f",
            "lavfi",
            "-i",
            video_src,
        ]

        cmd += [
            "-map",
            "1:v",
            "-map",
            "0:a",
            "-vf",
            vf,
        ]
        cmd += self._build_video_encoder_args(gop, force_keyframes=True)
        cmd += [
            # Desktop audio -> AAC 192 kbps stereo
            "-c:a",
            "aac",
            "-b:a",
            self.cfg.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
            # Rolling 2-second MPEG-TS segments (or full-session when buffer_seconds == 0)
            "-f",
            "hls",
            "-hls_time",
            str(self.cfg.segment_seconds),
            "-hls_list_size",
            str(list_size),
            *(["-hls_delete_threshold", "3"] if not full_session else []),
            "-hls_flags",
            ("temp_file+independent_segments" if full_session else "delete_segments+temp_file+independent_segments"),
            "-hls_segment_filename",
            seg_pattern,
            self.playlist_path,
        ]
        return cmd

    def build_manual_recording_cmd(self, ts_output_path: str) -> list[str]:
        """Build a low-RAM D3D11 NVENC + WASAPI continuous recording command to MPEG-TS."""
        eff_fps = self.cfg.fps if self.cfg.fps > 0 else 60
        gop = eff_fps * self.cfg.segment_seconds
        use_d3d11_nvenc = self.nv_adapter_index is not None

        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
        ]

        if use_d3d11_nvenc:
            cmd += [
                "-init_hw_device",
                f"d3d11va=nv:{self.nv_adapter_index}",
                "-filter_hw_device",
                "nv",
            ]

        cmd += [
            "-probesize",
            "32768",
            "-analyzeduration",
            "0",
            "-f",
            self.audio_fmt.ffmpeg_fmt,
            "-ar",
            str(self.audio_fmt.sample_rate),
            "-ac",
            str(self.audio_fmt.channels),
            "-i",
            "pipe:0",
        ]

        video_src, vf = self._build_video_source_and_filter(use_d3d11_nvenc)
        cmd += ["-f", "lavfi", "-i", video_src]

        cmd += [
            "-map",
            "1:v",
            "-map",
            "0:a",
            "-vf",
            vf,
            "-shortest",
        ]
        cmd += self._build_video_encoder_args(gop, force_keyframes=False)
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            self.cfg.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "mpegts",
            ts_output_path,
        ]
        return cmd

    def _spawn_ffmpeg_with_cmd(self, cmd: list[str], label: str) -> None:
        with self._lock:
            self._stderr_tail.clear()
            log.info(
                "Starting headless FFmpeg (%s) | adapter=%d (%s) %dx%d | audio=%s %dHz %dch",
                label,
                self.display.adapter_index,
                self.display.adapter_name,
                self.display.width,
                self.display.height,
                self.audio_fmt.ffmpeg_fmt,
                self.audio_fmt.sample_rate,
                self.audio_fmt.channels,
            )

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                bufsize=0,
            )
            self._ffmpeg_proc = proc

            threading.Thread(
                target=self._drain_stderr, args=(proc,), name="ffmpeg-stderr", daemon=True
            ).start()

            assert proc.stdin is not None
            wasapi = WasapiLoopbackStreamer(proc.stdin, self.audio_fmt)
            wasapi.muted = self._muted
            wasapi.start()
            self._wasapi = wasapi

    def start(self) -> None:
        self._stop_event.clear()
        if self.cfg.game_only and not self.cfg.synthetic_test:
            self._mode = "idle"
            log.info(
                "Game-only mode active: staying idle (~17 MB RAM, 0%% GPU) until a game window is detected."
            )
        else:
            self._mode = "replay"
            self._spawn_recorder()
        if not self.cfg.synthetic_test:
            self._game_monitor_thread = threading.Thread(
                target=self._game_monitor_loop, name="game-monitor", daemon=True
            )
            self._game_monitor_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="ffmpeg-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def get_settings_dict(self) -> dict:
        return {
            "buffer_sec": self.cfg.buffer_seconds,
            "fps": self.cfg.fps,
            "video_bitrate": self.cfg.video_bitrate,
            "audio_bitrate": self.cfg.audio_bitrate,
            "game_only": self.cfg.game_only,
            "capture_cursor": self.cfg.capture_cursor,
            "hotkey": self.cfg.hotkey,
            "output_dir": self.cfg.output_dir,
            "encoder": self.encoder.codec,
        }

    def apply_settings(self, updates: dict) -> dict:
        """Apply live user settings, persist to settings.json, and restart buffer if needed."""
        restart_needed = False
        with self._mode_lock:
            if "buffer_sec" in updates:
                raw_buf = int(updates["buffer_sec"])
                val = 0 if raw_buf <= 0 else max(5, min(7200, raw_buf))
                if val != self.cfg.buffer_seconds:
                    self.cfg.buffer_seconds = val
                    restart_needed = True
            if "fps" in updates:
                raw_fps = int(updates["fps"])
                val = 0 if raw_fps <= 0 else max(15, min(240, raw_fps))
                if val != self.cfg.fps:
                    self.cfg.fps = val
                    restart_needed = True
            if "video_bitrate" in updates:
                vb = str(updates["video_bitrate"]).strip().upper().rstrip("M")
                if vb.isdigit():
                    mbps = max(2, min(100, int(vb)))
                    new_vb = f"{mbps}M"
                    if new_vb != self.cfg.video_bitrate:
                        self.cfg.video_bitrate = new_vb
                        self.cfg.video_maxrate = f"{mbps + 4}M"
                        self.cfg.video_bufsize = f"{mbps}M"
                        restart_needed = True
            if "audio_bitrate" in updates:
                ab = str(updates["audio_bitrate"]).strip().lower().rstrip("k")
                if ab.isdigit():
                    kbps = max(64, min(320, int(ab)))
                    new_ab = f"{kbps}k"
                    if new_ab != self.cfg.audio_bitrate:
                        self.cfg.audio_bitrate = new_ab
                        restart_needed = True
            if "capture_cursor" in updates:
                cc = bool(updates["capture_cursor"])
                if cc != self.cfg.capture_cursor:
                    self.cfg.capture_cursor = cc
                    restart_needed = True
            if "hotkey" in updates:
                parsed_hk = parse_hotkey(str(updates["hotkey"]).strip())
                if parsed_hk is not None:
                    self.cfg.hotkey = parsed_hk[0]
            if "game_only" in updates:
                go = bool(updates["game_only"])
                if go != self.cfg.game_only:
                    self.cfg.game_only = go
                    if not go and self._mode == "idle":
                        self._mode = "replay"
                        self._spawn_recorder()
                        restart_needed = False
                    elif go and self._mode == "replay" and self._active_game is None:
                        self._enter_idle_locked("Switched to Game-Only mode")
                        restart_needed = False

            if restart_needed and self._mode == "replay":
                log.info("Settings changed -> restarting replay buffer with updated parameters.")
                self._stop_current_recorder(wait_for_eof=False)
                self._spawn_recorder()

        persisted = self.get_settings_dict()
        if not self.cfg.synthetic_test:
            save_user_settings(persisted)
        log.info("Updated settings: %s", persisted)
        return {"ok": True, "settings": persisted}

    def _game_monitor_loop(self) -> None:
        """
        Poll foreground window every 1s (0% CPU, zero injection):
        - Starts 30s replay buffer automatically when a game becomes focused.
        - Crops capture to ONLY the game window and scales to native screen resolution.
        - Keeps active game locked while Xbox Game Bar overlay is open.
        - Returns to idle (stops FFmpeg) when the game exits or after alt-tab grace expires.
        """
        while not self._stop_event.is_set():
            try:
                self._game_monitor_tick()
            except Exception as exc:
                log.debug("Game monitor tick error: %s", exc)
            self._stop_event.wait(1.0)

    def _game_monitor_tick(self) -> None:
        if not self.cfg.game_only:
            return
        with self._mode_lock:
            pref_hwnd = self._active_game.hwnd if self._active_game is not None else 0

        # Track any running visible game window regardless of what window is in focus!
        # Because capture uses Windows Graphics Capture (gfxcapture=hwnd=...),
        # ONLY that game window is captured even while unfocused or covered by other apps.
        running_game = self._detector.find_running_game(preferred_hwnd=pref_hwnd)

        with self._mode_lock:
            if self._mode == "idle":
                if running_game is not None:
                    self._active_game = running_game
                    self._game_lost_since = 0.0
                    self._mode = "replay"
                    log.info(
                        "Game detected: %s (hwnd=0x%x, %dx%d) -> starting WGC %ds replay buffer (%s)",
                        running_game.exe_name,
                        running_game.hwnd,
                        running_game.width,
                        running_game.height,
                        self.cfg.buffer_seconds,
                        self.encoder.codec,
                    )
                    self._spawn_recorder()
                return

            if self._mode == "replay":
                if running_game is not None:
                    self._game_lost_since = 0.0
                    prev = self._active_game
                    if prev is None or running_game.hwnd != prev.hwnd:
                        log.info(
                            "Switched game target -> %s (hwnd=0x%x)",
                            running_game.exe_name,
                            running_game.hwnd,
                        )
                        self._active_game = running_game
                        self._stop_current_recorder(wait_for_eof=False)
                        self._spawn_recorder()
                else:
                    # Game closed or minimized — stop FFmpeg and return to idle
                    self._enter_idle_locked("Game closed or minimized")
                return

            if self._mode == "recording":
                # If a manually recorded game exits completely, finalize & save cleanly
                if self._active_game is not None and not self._detector.is_window_alive(self._active_game.hwnd):
                    log.info("Game window closed during manual recording -> finalizing MP4.")
                    self._stop_manual_recording_locked()

    def _enter_idle_locked(self, reason: str) -> None:
        log.info("Returning to idle (%s) -> stopping FFmpeg.", reason)
        self._stop_current_recorder(wait_for_eof=False)
        self._clear_buffer_files()
        self._active_game = None
        self._game_lost_since = 0.0
        self._mode = "idle"

    def _clear_buffer_files(self) -> None:
        for name in os.listdir(self.buffer_dir):
            if name.endswith((".ts", ".ts.tmp", ".m3u8", ".m3u8.tmp")):
                try:
                    os.remove(os.path.join(self.buffer_dir, name))
                except OSError:
                    pass

    def _spawn_recorder(self) -> None:
        self._clear_buffer_files()
        cmd = self.build_recorder_cmd()
        self._spawn_ffmpeg_with_cmd(cmd, f"replay-buffer:{self.buffer_dir}")

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        try:
            for raw_line in iter(proc.stderr.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_tail.append(line)
                    log.debug("[ffmpeg] %s", line)
        except Exception:
            pass

    def _watchdog_loop(self) -> None:
        """Restart FFmpeg gracefully if ddagrab exits (e.g., Win+L lock screen or UAC)."""
        backoff = 1.5
        while not self._stop_event.is_set():
            # Skip watchdog intervention while a manual mode switch is in progress
            if not self._mode_lock.acquire(blocking=False):
                self._stop_event.wait(0.25)
                continue
            try:
                proc = self._ffmpeg_proc
                mode = self._mode
            finally:
                self._mode_lock.release()

            if mode == "replay" and proc is not None and proc.poll() is not None:
                rc = proc.returncode
                tail = " | ".join(list(self._stderr_tail)[-5:])
                log.warning(
                    "FFmpeg recorder exited unexpectedly (rc=%s). Tail: %s",
                    rc,
                    tail or "(no stderr)",
                )
                self._stop_current_recorder()
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 1.5, 10.0)
                try:
                    with self._mode_lock:
                        if self._mode != "replay":
                            continue
                        self.display = detect_primary_display_adapter()
                        if self.cfg.adapter_index is not None:
                            self.display.adapter_index = self.cfg.adapter_index
                        self.audio_fmt = probe_default_loopback_format()
                        self._spawn_recorder()
                    backoff = 1.5
                except Exception as exc:
                    log.error("Failed to respawn FFmpeg recorder: %s", exc)
            else:
                self._stop_event.wait(1.0)

    def _stop_current_recorder(self, wait_for_eof: bool = False) -> None:
        with self._lock:
            wasapi = self._wasapi
            proc = self._ffmpeg_proc
            self._wasapi = None
            self._ffmpeg_proc = None

        if wasapi is not None:
            wasapi.stop(timeout=1.5)

        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                if wait_for_eof:
                    try:
                        proc.wait(timeout=4.0)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    proc.terminate()
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)

    def stop(self) -> None:
        self._stop_event.set()
        self._stop_current_recorder()
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2.0)
        try:
            shutil.rmtree(self.buffer_dir, ignore_errors=True)
        except Exception:
            pass
        log.info("ReplayEngine stopped and temp buffer cleaned up.")

    def get_ram_stats(self) -> tuple[float, float, float]:
        """Return (daemon_private_ws_mb, ffmpeg_private_ws_mb, total_private_ws_mb) matching Task Manager."""
        daemon_mem = get_process_memory_mb(os.getpid())
        proc = self._ffmpeg_proc
        if proc is not None and proc.poll() is None:
            ff_handle = getattr(proc, "_handle", None)
            ff_mem = get_process_memory_mb(proc.pid, proc_handle=int(ff_handle) if ff_handle else None)
        else:
            ff_mem = get_process_memory_mb(0)
        total = daemon_mem.private_ws_mb + ff_mem.private_ws_mb
        return daemon_mem.private_ws_mb, ff_mem.private_ws_mb, total

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def recording_elapsed_sec(self) -> float:
        if self._mode != "recording" or self._manual_rec_started_at <= 0:
            return 0.0
        return max(0.0, time.monotonic() - self._manual_rec_started_at)

    @property
    def active_game_name(self) -> str:
        g = self._active_game
        return g.exe_name if g is not None else ""

    def ignore_active_game(self) -> dict:
        """Mark the currently detected target as 'not a game', persist, and return to idle."""
        with self._mode_lock:
            g = self._active_game
            if g is None:
                return {"ok": False, "ignored": ""}
            exe_name = g.exe_name
            self._detector.add_ignored_exe(exe_name)
            self._enter_idle_locked(f"User marked {exe_name} as not a game")
            return {"ok": True, "ignored": exe_name}

    def toggle_recording(self) -> dict:
        """
        Capture-style Record button toggle:
        - In 'idle' or 'replay' mode: stops any rolling replay buffer and starts a
          continuous manual recording (single FFmpeg process -> stays <150 MB RAM).
        - In 'recording' mode: cleanly finalizes the recording to
          Videos/Replays/recording_YYYYMMDD_HHMMSS.mp4 (-c copy) and resumes the 30s
          Instant Replay buffer if a game is active (otherwise returns to idle).
        """
        with self._mode_lock:
            if self._mode in ("idle", "replay"):
                return self._start_manual_recording_locked()
            return self._stop_manual_recording_locked()

    def _start_manual_recording_locked(self) -> dict:
        log.info("Switching from Instant Replay buffer -> continuous manual recording...")
        self._stop_current_recorder(wait_for_eof=False)
        self._clear_buffer_files()

        ts_path = os.path.join(self.buffer_dir, f"manual_rec_{int(time.time())}.ts")
        self._manual_rec_ts = ts_path
        self._mode = "recording"
        self._manual_rec_started_at = time.monotonic()

        cmd = self.build_manual_recording_cmd(ts_path)
        self._spawn_ffmpeg_with_cmd(cmd, f"manual-recording:{ts_path}")
        return {"ok": True, "mode": "recording", "filename": ""}

    def _stop_manual_recording_locked(self) -> dict:
        t0 = time.perf_counter()
        elapsed = self.recording_elapsed_sec
        ts_path = self._manual_rec_ts
        self._manual_rec_ts = ""
        self._manual_rec_started_at = 0.0

        log.info("Stopping manual recording (%.1fs) and finalizing MP4...", elapsed)
        # Closing stdin triggers clean EOF via -shortest so FFmpeg flushes final frames cleanly
        self._stop_current_recorder(wait_for_eof=True)

        out_path = ""
        fname = ""
        size_mb = 0.0
        error_msg = ""

        if ts_path and os.path.isfile(ts_path) and os.path.getsize(ts_path) > 0:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(self.cfg.output_dir, f"recording_{stamp}.mp4")
            if os.path.exists(out_path):
                ms_tag = int((time.time() % 1.0) * 1000)
                out_path = os.path.join(self.cfg.output_dir, f"recording_{stamp}_{ms_tag:03d}.mp4")

            remux_cmd = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                ts_path,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                out_path,
            ]
            res = subprocess.run(
                remux_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                timeout=60,
            )
            if res.returncode == 0 and os.path.isfile(out_path):
                fname = os.path.basename(out_path)
                size_mb = os.path.getsize(out_path) / (1024.0 * 1024.0)
                remux_ms = (time.perf_counter() - t0) * 1000.0
                log.info(
                    "SAVED RECORDING -> %s | %.1fs | %.2f MB | finalize=%.0f ms",
                    out_path,
                    elapsed,
                    size_mb,
                    remux_ms,
                )
            else:
                err = res.stderr.decode("utf-8", errors="replace").strip()
                error_msg = f"Failed to finalize recording MP4: {err}"
                log.warning(error_msg)
            try:
                os.remove(ts_path)
            except OSError:
                pass
        else:
            error_msg = "Recording too short (no video frames captured)."
            log.warning(error_msg)

        # Automatically resume the 30-second Instant Replay buffer (or idle if game exited)
        if self.cfg.game_only and not self.cfg.synthetic_test:
            still_alive = self._active_game is not None and self._detector.is_window_alive(
                self._active_game.hwnd
            )
            if still_alive and not self._stop_event.is_set():
                self._mode = "replay"
                self._spawn_recorder()
            else:
                self._active_game = None
                self._mode = "idle"
        else:
            self._mode = "replay"
            if not self._stop_event.is_set():
                self._spawn_recorder()

        if error_msg:
            return {"ok": False, "mode": self._mode, "filename": "", "error": error_msg}
        return {
            "ok": True,
            "mode": self._mode,
            "filename": fname,
            "path": out_path,
            "duration_sec": round(elapsed, 1),
            "size_mb": round(size_mb, 2),
        }

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        with self._lock:
            if self._wasapi is not None:
                self._wasapi.muted = self._muted
        log.info("Microphone %s", "MUTED" if self._muted else "UNMUTED")
        return self._muted

    def save_screenshot(self) -> dict:
        """
        Save a crisp screen-resolution PNG screenshot to Videos/Replays/:
        - In 'replay' mode with completed segments: extracts from latest .ts segment (~190 ms).
        - In 'idle' or 'recording' mode: grabs a single frame directly via ddagrab (~250 ms).
        """
        t0 = time.perf_counter()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(self.cfg.output_dir, f"screenshot_{stamp}.png")
        if os.path.exists(out_path):
            ms_tag = int((time.time() % 1.0) * 1000)
            out_path = os.path.join(self.cfg.output_dir, f"screenshot_{stamp}_{ms_tag:03d}.png")

        segments = self._read_completed_segments() if self._mode == "replay" else []
        if segments:
            latest_seg, _ = segments[-1]
            snap_dir = tempfile.mkdtemp(prefix="shot_", dir=self.buffer_dir)
            try:
                snap_ts = os.path.join(snap_dir, "latest.ts")
                try:
                    os.link(latest_seg, snap_ts)
                except OSError:
                    shutil.copy2(latest_seg, snap_ts)

                cmd = [
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    snap_ts,
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    out_path,
                ]
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=10,
                )
            finally:
                shutil.rmtree(snap_dir, ignore_errors=True)
        else:
            # Direct single-frame capture (works while idle or during manual recording)
            sw = max(2, self.display.width & ~1)
            sh = max(2, self.display.height & ~1)
            if self.cfg.synthetic_test:
                vsrc = f"testsrc2=size={sw}x{sh}:rate=30,format=bgra"
                vf = "format=rgb24"
            else:
                game = self._active_game
                if game is not None and game.hwnd:
                    vsrc = (
                        f"gfxcapture=hwnd={game.hwnd}:max_framerate=30:capture_cursor=1"
                        f":capture_border=0:display_border=0:width={sw}:height={sh}"
                        f":resize_mode=scale_aspect:scale_mode=bilinear:output_fmt=bgra,hwdownload,format=bgra"
                    )
                    vf = "format=bgra"
                else:
                    vsrc = (
                        f"ddagrab=output_idx={self.display.output_index}"
                        f":framerate=30:draw_mouse=1:output_fmt=bgra:allow_fallback=1,hwdownload,format=bgra"
                    )
                    vf = "format=bgra"

            cmd = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                "1",
                "-f",
                "lavfi",
                "-i",
                vsrc,
                "-vf",
                vf,
                "-frames:v",
                "1",
                "-update",
                "1",
                out_path,
            ]
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )

        if res.returncode != 0 or not os.path.isfile(out_path):
            err = res.stderr.decode("utf-8", errors="replace").strip()
            return {"ok": False, "filename": "", "error": f"Screenshot failed: {err}"}

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        fname = os.path.basename(out_path)
        log.info("SAVED SCREENSHOT -> %s (%.0f ms)", out_path, elapsed_ms)
        return {"ok": True, "filename": fname, "path": out_path, "ms": round(elapsed_ms, 0)}

    def open_captures_folder(self) -> bool:
        """Open current capture directory in Windows File Explorer."""
        try:
            os.makedirs(self.cfg.output_dir, exist_ok=True)
            os.startfile(self.cfg.output_dir)
            log.info("Opened captures folder: %s", self.cfg.output_dir)
            return True
        except Exception as exc:
            log.warning("Failed to open captures folder: %s", exc)
            return False

    def choose_output_dir(self) -> dict:
        """Open native Windows folder picker, persist choice, and switch output_dir live."""
        chosen = pick_folder_dialog(self.cfg.output_dir)
        if not chosen:
            return {"ok": False, "cancelled": True, "path": self.cfg.output_dir}
        os.makedirs(chosen, exist_ok=True)
        self.cfg.output_dir = chosen
        save_user_settings({"output_dir": chosen})
        log.info("Capture folder changed -> %s", chosen)
        return {"ok": True, "cancelled": False, "path": chosen}

    def _read_completed_segments(self) -> list[tuple[str, float]]:
        """Parse HLS playlist for completed .ts segments and their exact durations."""
        if not os.path.isfile(self.playlist_path):
            return []
        try:
            with open(self.playlist_path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return []

        segments: list[tuple[str, float]] = []
        pending_dur = float(self.cfg.segment_seconds)
        for ln in lines:
            if ln.startswith("#EXTINF:"):
                try:
                    pending_dur = float(ln.split(":", 1)[1].split(",", 1)[0])
                except ValueError:
                    pending_dur = float(self.cfg.segment_seconds)
            elif not ln.startswith("#"):
                seg_path = ln if os.path.isabs(ln) else os.path.join(self.buffer_dir, ln)
                if os.path.isfile(seg_path):
                    segments.append((seg_path, pending_dur))
                pending_dur = float(self.cfg.segment_seconds)

        # buffer_seconds == 0 -> keep every segment recorded since the game started
        if self.cfg.buffer_seconds <= 0:
            return segments

        # Pick trailing segments summing up to >= buffer_seconds
        selected: list[tuple[str, float]] = []
        total_dur = 0.0
        for seg_path, dur in reversed(segments):
            selected.append((seg_path, dur))
            total_dur += dur
            if total_dur >= self.cfg.buffer_seconds:
                break
        selected.reverse()
        return selected

    def save_replay(self) -> SaveResult:
        """
        Snapshot the last 30s of completed MPEG-TS segments via NTFS hard links
        and remux to MP4 with `-c copy` (zero re-encoding, non-blocking to recorder).
        """
        t0 = time.perf_counter()
        daemon_mb, ff_mb, total_mb = self.get_ram_stats()

        if self._mode != "replay":
            return SaveResult(
                ok=False,
                output_path="",
                segments_used=0,
                approx_duration_sec=0.0,
                size_mb=0.0,
                remux_ms=0.0,
                daemon_ram_mb=daemon_mb,
                ffmpeg_ram_mb=ff_mb,
                total_ram_mb=total_mb,
                error="Manual recording is active; stop recording first.",
            )

        if not self._save_in_progress.acquire(blocking=False):
            return SaveResult(
                ok=False,
                output_path="",
                segments_used=0,
                approx_duration_sec=0.0,
                size_mb=0.0,
                remux_ms=0.0,
                daemon_ram_mb=daemon_mb,
                ffmpeg_ram_mb=ff_mb,
                total_ram_mb=total_mb,
                error="A replay save is already in progress.",
            )

        snap_dir = ""
        try:
            segments = self._read_completed_segments()
            if not segments:
                return SaveResult(
                    ok=False,
                    output_path="",
                    segments_used=0,
                    approx_duration_sec=0.0,
                    size_mb=0.0,
                    remux_ms=(time.perf_counter() - t0) * 1000.0,
                    daemon_ram_mb=daemon_mb,
                    ffmpeg_ram_mb=ff_mb,
                    total_ram_mb=total_mb,
                    error="No completed buffer segments yet (wait ~2 seconds after start).",
                )

            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(self.cfg.output_dir, f"replay_{stamp}.mp4")
            # Avoid filename collision if triggered twice within the same second
            if os.path.exists(out_path):
                ms_tag = int((time.time() % 1.0) * 1000)
                out_path = os.path.join(self.cfg.output_dir, f"replay_{stamp}_{ms_tag:03d}.mp4")

            snap_dir = tempfile.mkdtemp(prefix="snap_", dir=self.buffer_dir)
            concat_path = os.path.join(snap_dir, "concat.txt")
            linked_paths: list[str] = []
            approx_dur = 0.0

            for i, (src_path, dur) in enumerate(segments):
                dst_path = os.path.join(snap_dir, f"part_{i:04d}.ts")
                try:
                    # Atomic NTFS hard link (< 0.1ms, 0 bytes copied, immune to HLS delete_segments)
                    os.link(src_path, dst_path)
                except OSError:
                    if not os.path.isfile(src_path):
                        continue
                    shutil.copy2(src_path, dst_path)
                linked_paths.append(dst_path)
                approx_dur += dur

            if not linked_paths:
                return SaveResult(
                    ok=False,
                    output_path="",
                    segments_used=0,
                    approx_duration_sec=0.0,
                    size_mb=0.0,
                    remux_ms=(time.perf_counter() - t0) * 1000.0,
                    daemon_ram_mb=daemon_mb,
                    ffmpeg_ram_mb=ff_mb,
                    total_ram_mb=total_mb,
                    error="Buffered segments rolled over before snapshot could link them.",
                )

            with open(concat_path, "w", encoding="utf-8") as cf:
                for lp in linked_paths:
                    escaped = lp.replace("'", r"'\''")
                    cf.write(f"file '{escaped}'\n")

            remux_cmd = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                out_path,
            ]
            res = subprocess.run(
                remux_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                timeout=120,
            )
            remux_ms = (time.perf_counter() - t0) * 1000.0
            daemon_mb, ff_mb, total_mb = self.get_ram_stats()

            if res.returncode != 0 or not os.path.isfile(out_path):
                err_txt = res.stderr.decode("utf-8", errors="replace").strip()
                return SaveResult(
                    ok=False,
                    output_path=out_path,
                    segments_used=len(linked_paths),
                    approx_duration_sec=approx_dur,
                    size_mb=0.0,
                    remux_ms=remux_ms,
                    daemon_ram_mb=daemon_mb,
                    ffmpeg_ram_mb=ff_mb,
                    total_ram_mb=total_mb,
                    error=f"FFmpeg remux failed (rc={res.returncode}): {err_txt}",
                )

            size_mb = os.path.getsize(out_path) / (1024.0 * 1024.0)
            return SaveResult(
                ok=True,
                output_path=out_path,
                segments_used=len(linked_paths),
                approx_duration_sec=approx_dur,
                size_mb=size_mb,
                remux_ms=remux_ms,
                daemon_ram_mb=daemon_mb,
                ffmpeg_ram_mb=ff_mb,
                total_ram_mb=total_mb,
            )
        finally:
            if snap_dir:
                shutil.rmtree(snap_dir, ignore_errors=True)
            self._save_in_progress.release()
