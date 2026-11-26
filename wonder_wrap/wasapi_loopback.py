"""
Zero-dependency Windows WASAPI desktop audio loopback streamer (pure ctypes).

Pipes continuous, hardware-clocked PCM audio from the default Windows render
endpoint into FFmpeg's stdin pipe. Uses a silent keepalive render client so
WASAPI loopback continues delivering packets even during complete desktop
silence (preventing FFmpeg stalls or A/V drift), and recovers automatically
if the default output device is switched (e.g., Bluetooth headset connect/disconnect).
"""

from __future__ import annotations

from array import array
import ctypes
from ctypes import wintypes
import dataclasses
import logging
import threading
import time
from typing import BinaryIO

from .win32_dxgi import GUID, vtable_call

log = logging.getLogger("wonder_wrap.wasapi")

COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 23
ERENDER = 0
ECAPTURE = 1
EMULTIMEDIA = 1
ECOMMUNICATIONS = 2
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_E_DEVICE_INVALIDATED = 0x88890004
AUDCLNT_E_SERVICE_NOT_RUNNING = 0x88890010

CLSID_MMDeviceEnumerator = GUID.from_str("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID.from_str("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioClient = GUID.from_str("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID.from_str("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
IID_IAudioRenderClient = GUID.from_str("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", wintypes.WORD),
        ("dwChannelMask", wintypes.DWORD),
        ("SubFormat", GUID),
    ]


@dataclasses.dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    bits_per_sample: int
    block_align: int
    ffmpeg_fmt: str  # e.g. 'f32le' or 's16le'
    device_id: str


def _parse_mix_format(mix_fmt_ptr: ctypes.c_void_p, device_id: str) -> AudioFormat:
    wfx = ctypes.cast(mix_fmt_ptr, ctypes.POINTER(WAVEFORMATEX)).contents
    is_float = False
    if wfx.wFormatTag == 3:  # WAVE_FORMAT_IEEE_FLOAT
        is_float = True
    elif wfx.wFormatTag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE
        wfxe = ctypes.cast(mix_fmt_ptr, ctypes.POINTER(WAVEFORMATEXTENSIBLE)).contents
        is_float = wfxe.SubFormat.Data1 == 3

    if is_float and wfx.wBitsPerSample == 32:
        ff_fmt = "f32le"
    elif (not is_float) and wfx.wBitsPerSample == 16:
        ff_fmt = "s16le"
    elif (not is_float) and wfx.wBitsPerSample == 32:
        ff_fmt = "s32le"
    elif (not is_float) and wfx.wBitsPerSample == 24:
        ff_fmt = "s24le"
    else:
        ff_fmt = "f32le"

    return AudioFormat(
        sample_rate=int(wfx.nSamplesPerSec),
        channels=int(wfx.nChannels),
        bits_per_sample=int(wfx.wBitsPerSample),
        block_align=int(wfx.nBlockAlign),
        ffmpeg_fmt=ff_fmt,
        device_id=device_id,
    )


def probe_default_loopback_format() -> AudioFormat:
    """Query the default Windows render endpoint's native WASAPI mix format."""
    ole32 = ctypes.WinDLL("ole32.dll")
    hr_init = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    should_uninit = hr_init in (0, 1)
    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    client = ctypes.c_void_p()
    mix_fmt_ptr = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_MMDeviceEnumerator),
            None,
            CLSCTX_ALL,
            ctypes.byref(IID_IMMDeviceEnumerator),
            ctypes.byref(enumerator),
        )
        if hr != 0:
            raise OSError(f"CoCreateInstance(MMDeviceEnumerator) failed: 0x{hr & 0xFFFFFFFF:08x}")

        hr = vtable_call(
            enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        )(enumerator, ERENDER, EMULTIMEDIA, ctypes.byref(device))
        if hr != 0:
            raise OSError(f"GetDefaultAudioEndpoint failed: 0x{hr & 0xFFFFFFFF:08x}")

        dev_id_ptr = wintypes.LPWSTR()
        vtable_call(device, 5, ctypes.c_long, ctypes.POINTER(wintypes.LPWSTR))(
            device, ctypes.byref(dev_id_ptr)
        )
        dev_id = dev_id_ptr.value or "default"
        if dev_id_ptr:
            ole32.CoTaskMemFree(dev_id_ptr)

        hr = vtable_call(
            device,
            3,
            ctypes.c_long,
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(device, ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(client))
        if hr != 0:
            raise OSError(f"Activate(IAudioClient) failed: 0x{hr & 0xFFFFFFFF:08x}")

        hr = vtable_call(client, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            client, ctypes.byref(mix_fmt_ptr)
        )
        if hr != 0:
            raise OSError(f"GetMixFormat failed: 0x{hr & 0xFFFFFFFF:08x}")

        return _parse_mix_format(mix_fmt_ptr, dev_id)
    finally:
        if mix_fmt_ptr:
            ole32.CoTaskMemFree(mix_fmt_ptr)
        for ptr in (client, device, enumerator):
            if ptr:
                vtable_call(ptr, 2, wintypes.ULONG)(ptr)
        if should_uninit:
            ole32.CoUninitialize()


def _mix_pcm_bytes(desktop: bytes, mic: bytes, ffmpeg_fmt: str) -> bytes:
    """Mix two equal-length PCM buffers (desktop + mic) with soft clipping."""
    if not mic:
        return desktop
    n = min(len(desktop), len(mic))
    if n == 0:
        return desktop

    if ffmpeg_fmt == "f32le":
        a = array("f")
        a.frombytes(desktop[:n])
        b = array("f")
        b.frombytes(mic[:n])
        for i in range(len(a)):
            s = a[i] + b[i]
            if s > 1.0:
                s = 1.0
            elif s < -1.0:
                s = -1.0
            a[i] = s
        mixed = a.tobytes()
    elif ffmpeg_fmt == "s16le":
        a = array("h")
        a.frombytes(desktop[:n])
        b = array("h")
        b.frombytes(mic[:n])
        for i in range(len(a)):
            s = a[i] + b[i]
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            a[i] = s
        mixed = a.tobytes()
    else:
        return desktop

    return mixed + desktop[n:] if n < len(desktop) else mixed


class _WasapiMicBuffer:
    """
    Lightweight WASAPI microphone capture thread using AUTOCONVERTPCM so the
    OS resamples & upmixes the default microphone directly into the desktop
    stream's exact WAVEFORMATEX. Opens the mic only while unmuted.
    """

    def __init__(self, target_fmt: AudioFormat, target_wfx_bytes: bytes) -> None:
        self._fmt = target_fmt
        self._wfx_bytes = target_wfx_bytes
        self._lock = threading.Lock()
        self._buf = bytearray()
        # Keep at most ~120ms buffered so mic never drifts behind desktop audio
        self._max_bytes = max(
            target_fmt.block_align * 64,
            (target_fmt.sample_rate * target_fmt.block_align * 12) // 100,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._muted = False

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._muted = bool(value)
        if self._muted:
            with self._lock:
                self._buf.clear()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="wasapi-mic", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.5) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        with self._lock:
            self._buf.clear()

    def pop_bytes(self, nbytes: int) -> bytes:
        if self._muted or nbytes <= 0:
            return b""
        with self._lock:
            avail = len(self._buf)
            if avail == 0:
                return b""
            take = min(avail, nbytes)
            # Align to whole audio frames
            take -= take % self._fmt.block_align
            if take <= 0:
                return b""
            out = bytes(self._buf[:take])
            del self._buf[:take]
            return out

    def _run(self) -> None:
        ole32 = ctypes.WinDLL("ole32.dll")
        hr_init = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        should_uninit = hr_init in (0, 1)
        try:
            while not self._stop_event.is_set():
                if self._muted:
                    # Release microphone completely while muted
                    time.sleep(0.05)
                    continue
                self._capture_mic_session(ole32)
                if not self._stop_event.is_set() and not self._muted:
                    time.sleep(0.5)
        except Exception as exc:
            log.debug("WASAPI mic thread error: %s", exc)
        finally:
            if should_uninit:
                ole32.CoUninitialize()

    def _capture_mic_session(self, ole32) -> None:
        enumerator = ctypes.c_void_p()
        device = ctypes.c_void_p()
        cap_client = ctypes.c_void_p()
        cap_svc = ctypes.c_void_p()
        cap_started = False
        try:
            hr = ole32.CoCreateInstance(
                ctypes.byref(CLSID_MMDeviceEnumerator),
                None,
                CLSCTX_ALL,
                ctypes.byref(IID_IMMDeviceEnumerator),
                ctypes.byref(enumerator),
            )
            if hr != 0:
                return

            hr = vtable_call(
                enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
            )(enumerator, ECAPTURE, EMULTIMEDIA, ctypes.byref(device))
            if hr != 0:
                hr = vtable_call(
                    enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
                )(enumerator, ECAPTURE, ECOMMUNICATIONS, ctypes.byref(device))
            if hr != 0:
                return

            dev_id_ptr = wintypes.LPWSTR()
            vtable_call(device, 5, ctypes.c_long, ctypes.POINTER(wintypes.LPWSTR))(
                device, ctypes.byref(dev_id_ptr)
            )
            active_mic_id = dev_id_ptr.value or ""
            if dev_id_ptr:
                ole32.CoTaskMemFree(dev_id_ptr)

            hr = vtable_call(
                device,
                3,
                ctypes.c_long,
                ctypes.POINTER(GUID),
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(device, ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(cap_client))
            if hr != 0:
                return

            wfx_buf = ctypes.create_string_buffer(self._wfx_bytes)
            hns_duration = 1_000_000  # 100 ms
            stream_flags = AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
            hr = vtable_call(
                cap_client,
                3,
                ctypes.c_long,
                ctypes.c_int,
                wintypes.DWORD,
                ctypes.c_longlong,
                ctypes.c_longlong,
                ctypes.c_void_p,
                ctypes.c_void_p,
            )(
                cap_client,
                AUDCLNT_SHAREMODE_SHARED,
                stream_flags,
                hns_duration,
                0,
                ctypes.byref(wfx_buf),
                None,
            )
            if hr != 0:
                log.debug("WASAPI mic Initialize(AUTOCONVERTPCM) failed: 0x%08x", hr & 0xFFFFFFFF)
                return

            hr = vtable_call(
                cap_client, 14, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
            )(cap_client, ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(cap_svc))
            if hr != 0:
                return

            hr = vtable_call(cap_client, 10, ctypes.c_long)(cap_client)
            if hr != 0:
                return
            cap_started = True
            log.info("WASAPI microphone capture active (auto-converted to %dHz %dch %s).",
                     self._fmt.sample_rate, self._fmt.channels, self._fmt.ffmpeg_fmt)

            get_next_pkt = vtable_call(cap_svc, 5, ctypes.c_long, ctypes.POINTER(wintypes.UINT))
            get_buf = vtable_call(
                cap_svc,
                3,
                ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
                ctypes.c_void_p,
            )
            rel_buf = vtable_call(cap_svc, 4, ctypes.c_long, wintypes.UINT)
            block_align = self._fmt.block_align
            last_dev_check = time.monotonic()

            while not self._stop_event.is_set() and not self._muted:
                pkt_len = wintypes.UINT(0)
                hr_pkt = get_next_pkt(cap_svc, ctypes.byref(pkt_len))
                if (hr_pkt & 0xFFFFFFFF) in (AUDCLNT_E_DEVICE_INVALIDATED, AUDCLNT_E_SERVICE_NOT_RUNNING):
                    return
                if hr_pkt != 0:
                    return

                while pkt_len.value > 0 and not self._stop_event.is_set() and not self._muted:
                    data_ptr = ctypes.c_void_p()
                    num_frames = wintypes.UINT(0)
                    flags = wintypes.DWORD(0)
                    hr_buf = get_buf(
                        cap_svc,
                        ctypes.byref(data_ptr),
                        ctypes.byref(num_frames),
                        ctypes.byref(flags),
                        None,
                        None,
                    )
                    if (hr_buf & 0xFFFFFFFF) in (AUDCLNT_E_DEVICE_INVALIDATED, AUDCLNT_E_SERVICE_NOT_RUNNING):
                        return
                    if hr_buf != 0:
                        break

                    n = num_frames.value
                    if n > 0:
                        byte_count = n * block_align
                        if (flags.value & AUDCLNT_BUFFERFLAGS_SILENT) or not data_ptr.value:
                            mic_chunk = b"\x00" * byte_count
                        else:
                            mic_chunk = ctypes.string_at(data_ptr.value, byte_count)
                        rel_buf(cap_svc, n)
                        with self._lock:
                            self._buf.extend(mic_chunk)
                            if len(self._buf) > self._max_bytes:
                                excess = len(self._buf) - self._max_bytes
                                excess -= excess % block_align
                                if excess > 0:
                                    del self._buf[:excess]
                    else:
                        rel_buf(cap_svc, 0)

                    if get_next_pkt(cap_svc, ctypes.byref(pkt_len)) != 0:
                        break

                now = time.monotonic()
                if now - last_dev_check >= 2.5:
                    last_dev_check = now
                    new_dev = ctypes.c_void_p()
                    if (
                        vtable_call(
                            enumerator,
                            4,
                            ctypes.c_long,
                            ctypes.c_int,
                            ctypes.c_int,
                            ctypes.POINTER(ctypes.c_void_p),
                        )(enumerator, ECAPTURE, EMULTIMEDIA, ctypes.byref(new_dev))
                        == 0
                    ):
                        new_id_ptr = wintypes.LPWSTR()
                        vtable_call(new_dev, 5, ctypes.c_long, ctypes.POINTER(wintypes.LPWSTR))(
                            new_dev, ctypes.byref(new_id_ptr)
                        )
                        new_id = new_id_ptr.value or ""
                        if new_id_ptr:
                            ole32.CoTaskMemFree(new_id_ptr)
                        vtable_call(new_dev, 2, wintypes.ULONG)(new_dev)
                        if new_id and new_id != active_mic_id:
                            log.info("Default microphone changed, re-attaching WASAPI mic.")
                            return

                time.sleep(0.005)
        finally:
            if cap_started and cap_client:
                try:
                    vtable_call(cap_client, 11, ctypes.c_long)(cap_client)
                except Exception:
                    pass
            for ptr in (cap_svc, cap_client, device, enumerator):
                if ptr:
                    try:
                        vtable_call(ptr, 2, wintypes.ULONG)(ptr)
                    except Exception:
                        pass


class WasapiLoopbackStreamer:
    """
    Background thread that captures desktop audio via WASAPI loopback + optional
    default microphone audio, mixes them live, and writes raw PCM to FFmpeg stdin.
    """

    def __init__(self, target_pipe: BinaryIO, expected_format: AudioFormat) -> None:
        self._pipe = target_pipe
        self._expected_fmt = expected_format
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._mic: _WasapiMicBuffer | None = None
        self._mic_muted = False
        self.frames_written = 0
        self.last_error: str | None = None

    @property
    def muted(self) -> bool:
        """Whether the microphone is muted (desktop/game audio always records)."""
        return self._mic_muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._mic_muted = bool(value)
        mic = self._mic
        if mic is not None:
            mic.muted = self._mic_muted

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="wasapi-loopback", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _write_pipe(self, data: bytes) -> bool:
        try:
            self._pipe.write(data)
            self._pipe.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _run(self) -> None:
        ole32 = ctypes.WinDLL("ole32.dll")
        hr_init = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        should_uninit = hr_init in (0, 1)
        try:
            while not self._stop_event.is_set():
                ok = self._capture_session(ole32)
                if not ok:
                    break
                # Brief silence pad if re-attaching after audio device switch
                if not self._stop_event.is_set():
                    pad_frames = max(1, self._expected_fmt.sample_rate // 20)  # 50ms
                    if not self._write_pipe(b"\x00" * (pad_frames * self._expected_fmt.block_align)):
                        break
                    time.sleep(0.05)
        except Exception as exc:
            self.last_error = repr(exc)
            log.exception("WASAPI loopback thread crashed: %s", exc)
        finally:
            if should_uninit:
                ole32.CoUninitialize()

    def _capture_session(self, ole32) -> bool:
        """
        Run one WASAPI loopback capture session until stopped, pipe closed, or
        device invalidated. Returns True to retry on device switch, False to exit.
        """
        enumerator = ctypes.c_void_p()
        device = ctypes.c_void_p()
        cap_client = ctypes.c_void_p()
        cap_svc = ctypes.c_void_p()
        ren_client = ctypes.c_void_p()
        ren_svc = ctypes.c_void_p()
        mix_fmt_ptr = ctypes.c_void_p()
        cap_started = False
        ren_started = False

        try:
            hr = ole32.CoCreateInstance(
                ctypes.byref(CLSID_MMDeviceEnumerator),
                None,
                CLSCTX_ALL,
                ctypes.byref(IID_IMMDeviceEnumerator),
                ctypes.byref(enumerator),
            )
            if hr != 0:
                self.last_error = f"CoCreateInstance failed: 0x{hr & 0xFFFFFFFF:08x}"
                return False

            hr = vtable_call(
                enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
            )(enumerator, ERENDER, EMULTIMEDIA, ctypes.byref(device))
            if hr != 0:
                log.warning("No default render audio device (0x%08x), retrying...", hr & 0xFFFFFFFF)
                return True

            dev_id_ptr = wintypes.LPWSTR()
            vtable_call(device, 5, ctypes.c_long, ctypes.POINTER(wintypes.LPWSTR))(
                device, ctypes.byref(dev_id_ptr)
            )
            active_dev_id = dev_id_ptr.value or ""
            if dev_id_ptr:
                ole32.CoTaskMemFree(dev_id_ptr)

            # Activate loopback capture IAudioClient
            hr = vtable_call(
                device,
                3,
                ctypes.c_long,
                ctypes.POINTER(GUID),
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(device, ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(cap_client))
            if hr != 0:
                return True

            hr = vtable_call(cap_client, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                cap_client, ctypes.byref(mix_fmt_ptr)
            )
            if hr != 0:
                return True

            cur_fmt = _parse_mix_format(mix_fmt_ptr, active_dev_id)
            if (
                cur_fmt.sample_rate != self._expected_fmt.sample_rate
                or cur_fmt.channels != self._expected_fmt.channels
                or cur_fmt.ffmpeg_fmt != self._expected_fmt.ffmpeg_fmt
            ):
                log.warning(
                    "Default audio device format changed (%s -> %s); padding silence until format matches.",
                    self._expected_fmt,
                    cur_fmt,
                )
                return self._silence_pump_until_device_change()

            hns_duration = 1_000_000  # 100 ms buffer

            # Start silent render keepalive client on same device so loopback never stalls
            ren_buf_frames = wintypes.UINT(0)
            hr_ren = vtable_call(
                device,
                3,
                ctypes.c_long,
                ctypes.POINTER(GUID),
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(device, ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(ren_client))
            if hr_ren == 0:
                hr_ren = vtable_call(
                    ren_client,
                    3,
                    ctypes.c_long,
                    ctypes.c_int,
                    wintypes.DWORD,
                    ctypes.c_longlong,
                    ctypes.c_longlong,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                )(ren_client, AUDCLNT_SHAREMODE_SHARED, 0, hns_duration, 0, mix_fmt_ptr, None)
                if hr_ren == 0:
                    vtable_call(ren_client, 4, ctypes.c_long, ctypes.POINTER(wintypes.UINT))(
                        ren_client, ctypes.byref(ren_buf_frames)
                    )
                    hr_ren = vtable_call(
                        ren_client, 14, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
                    )(ren_client, ctypes.byref(IID_IAudioRenderClient), ctypes.byref(ren_svc))
                if hr_ren == 0 and ren_svc and ren_buf_frames.value > 0:
                    p_data = ctypes.c_void_p()
                    if (
                        vtable_call(ren_svc, 3, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                            ren_svc, ren_buf_frames.value, ctypes.byref(p_data)
                        )
                        == 0
                    ):
                        vtable_call(ren_svc, 4, ctypes.c_long, wintypes.UINT, wintypes.DWORD)(
                            ren_svc, ren_buf_frames.value, AUDCLNT_BUFFERFLAGS_SILENT
                        )
                    if vtable_call(ren_client, 10, ctypes.c_long)(ren_client) == 0:
                        ren_started = True

            # Initialize & start loopback capture client
            hr = vtable_call(
                cap_client,
                3,
                ctypes.c_long,
                ctypes.c_int,
                wintypes.DWORD,
                ctypes.c_longlong,
                ctypes.c_longlong,
                ctypes.c_void_p,
                ctypes.c_void_p,
            )(
                cap_client,
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK,
                hns_duration,
                0,
                mix_fmt_ptr,
                None,
            )
            if hr != 0:
                log.warning("IAudioClient::Initialize(LOOPBACK) failed: 0x%08x", hr & 0xFFFFFFFF)
                return True

            hr = vtable_call(
                cap_client, 14, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
            )(cap_client, ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(cap_svc))
            if hr != 0:
                return True

            hr = vtable_call(cap_client, 10, ctypes.c_long)(cap_client)
            if hr != 0:
                return True
            cap_started = True

            get_next_pkt = vtable_call(cap_svc, 5, ctypes.c_long, ctypes.POINTER(wintypes.UINT))
            get_buf = vtable_call(
                cap_svc,
                3,
                ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
                ctypes.c_void_p,
            )
            rel_buf = vtable_call(cap_svc, 4, ctypes.c_long, wintypes.UINT)

            ren_get_pad = (
                vtable_call(ren_client, 6, ctypes.c_long, ctypes.POINTER(wintypes.UINT))
                if ren_started
                else None
            )
            ren_get_buf = (
                vtable_call(ren_svc, 3, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
                if ren_started
                else None
            )
            ren_rel_buf = (
                vtable_call(ren_svc, 4, ctypes.c_long, wintypes.UINT, wintypes.DWORD)
                if ren_started
                else None
            )

            block_align = cur_fmt.block_align
            last_dev_check = time.monotonic()

            # Start companion WASAPI microphone capture in the exact same WAVEFORMATEX
            wfx_hdr = ctypes.cast(mix_fmt_ptr, ctypes.POINTER(WAVEFORMATEX)).contents
            wfx_total_size = ctypes.sizeof(WAVEFORMATEX) + int(wfx_hdr.cbSize)
            wfx_raw_bytes = ctypes.string_at(mix_fmt_ptr, wfx_total_size)
            mic_buf = _WasapiMicBuffer(cur_fmt, wfx_raw_bytes)
            mic_buf.muted = self._mic_muted
            mic_buf.start()
            self._mic = mic_buf

            while not self._stop_event.is_set():
                # Top up silent render keepalive buffer so it never underruns
                if ren_started and ren_get_pad and ren_get_buf and ren_rel_buf:
                    pad = wintypes.UINT(0)
                    if ren_get_pad(ren_client, ctypes.byref(pad)) == 0:
                        avail = ren_buf_frames.value - pad.value
                        if avail > 0:
                            rp = ctypes.c_void_p()
                            if ren_get_buf(ren_svc, avail, ctypes.byref(rp)) == 0:
                                ren_rel_buf(ren_svc, avail, AUDCLNT_BUFFERFLAGS_SILENT)

                pkt_len = wintypes.UINT(0)
                hr_pkt = get_next_pkt(cap_svc, ctypes.byref(pkt_len))
                if (hr_pkt & 0xFFFFFFFF) in (AUDCLNT_E_DEVICE_INVALIDATED, AUDCLNT_E_SERVICE_NOT_RUNNING):
                    log.info("WASAPI device invalidated (0x%08x), re-attaching...", hr_pkt & 0xFFFFFFFF)
                    return True
                if hr_pkt != 0:
                    return True

                while pkt_len.value > 0 and not self._stop_event.is_set():
                    data_ptr = ctypes.c_void_p()
                    num_frames = wintypes.UINT(0)
                    flags = wintypes.DWORD(0)
                    hr_buf = get_buf(
                        cap_svc,
                        ctypes.byref(data_ptr),
                        ctypes.byref(num_frames),
                        ctypes.byref(flags),
                        None,
                        None,
                    )
                    if (hr_buf & 0xFFFFFFFF) in (
                        AUDCLNT_E_DEVICE_INVALIDATED,
                        AUDCLNT_E_SERVICE_NOT_RUNNING,
                    ):
                        log.info("WASAPI device invalidated in GetBuffer, re-attaching...")
                        return True
                    if hr_buf != 0:
                        break

                    n = num_frames.value
                    if n > 0:
                        byte_count = n * block_align
                        if (flags.value & AUDCLNT_BUFFERFLAGS_SILENT) or not data_ptr.value:
                            chunk = b"\x00" * byte_count
                        else:
                            chunk = ctypes.string_at(data_ptr.value, byte_count)
                        rel_buf(cap_svc, n)
                        if not self._mic_muted:
                            mic_chunk = mic_buf.pop_bytes(byte_count)
                            if mic_chunk:
                                chunk = _mix_pcm_bytes(chunk, mic_chunk, cur_fmt.ffmpeg_fmt)
                        if not self._write_pipe(chunk):
                            return False
                        self.frames_written += n
                    else:
                        rel_buf(cap_svc, 0)

                    if get_next_pkt(cap_svc, ctypes.byref(pkt_len)) != 0:
                        break

                # Every 2 seconds, verify default endpoint hasn't switched (e.g. user plugged in headset)
                now = time.monotonic()
                if now - last_dev_check >= 2.0:
                    last_dev_check = now
                    new_dev = ctypes.c_void_p()
                    if (
                        vtable_call(
                            enumerator,
                            4,
                            ctypes.c_long,
                            ctypes.c_int,
                            ctypes.c_int,
                            ctypes.POINTER(ctypes.c_void_p),
                        )(enumerator, ERENDER, EMULTIMEDIA, ctypes.byref(new_dev))
                        == 0
                    ):
                        new_id_ptr = wintypes.LPWSTR()
                        vtable_call(new_dev, 5, ctypes.c_long, ctypes.POINTER(wintypes.LPWSTR))(
                            new_dev, ctypes.byref(new_id_ptr)
                        )
                        new_id = new_id_ptr.value or ""
                        if new_id_ptr:
                            ole32.CoTaskMemFree(new_id_ptr)
                        vtable_call(new_dev, 2, wintypes.ULONG)(new_dev)
                        if new_id and new_id != active_dev_id:
                            log.info("Default audio output changed, re-attaching WASAPI loopback.")
                            return True

                time.sleep(0.005)

            return False
        finally:
            if self._mic is not None:
                try:
                    self._mic.stop(timeout=1.5)
                except Exception:
                    pass
                self._mic = None
            if cap_started and cap_client:
                try:
                    vtable_call(cap_client, 11, ctypes.c_long)(cap_client)
                except Exception:
                    pass
            if ren_started and ren_client:
                try:
                    vtable_call(ren_client, 11, ctypes.c_long)(ren_client)
                except Exception:
                    pass
            if mix_fmt_ptr:
                ole32.CoTaskMemFree(mix_fmt_ptr)
            for ptr in (cap_svc, cap_client, ren_svc, ren_client, device, enumerator):
                if ptr:
                    try:
                        vtable_call(ptr, 2, wintypes.ULONG)(ptr)
                    except Exception:
                        pass

    def _silence_pump_until_device_change(self) -> bool:
        """Pump 20ms silence blocks for 1 second before retrying default device format."""
        chunk_frames = max(1, self._expected_fmt.sample_rate // 50)
        silence = b"\x00" * (chunk_frames * self._expected_fmt.block_align)
        for _ in range(50):
            if self._stop_event.is_set():
                return False
            if not self._write_pipe(silence):
                return False
            time.sleep(0.02)
        return True
