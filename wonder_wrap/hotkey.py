"""
Win32 Global Hotkey listener (Alt+F10) + Named Event IPC for clean daemon stop.
100% external (uses Win32 RegisterHotKey, with optional WH_KEYBOARD_LL fallback
if Alt+F10 is already claimed by another app like NVIDIA Overlay).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import time
from typing import Callable

log = logging.getLogger("woneder.hotkey")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_F10 = 0x79
VK_F12 = 0x7B

_KEY_VK_MAP: dict[str, int] = {
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    "INSERT": 0x2D,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "PRINTSCREEN": 0x2C,
    "PAUSE": 0x13,
}

_MOD_FREE_KEYS = {f"F{i}" for i in range(1, 13)} | {"INSERT", "PRINTSCREEN", "PAUSE"}


def parse_hotkey(spec: str) -> tuple[str, int, int] | None:
    """
    Parse & validate a user-entered hotkey string (e.g. 'Alt+F10', 'Ctrl+Shift+S', 'F9').
    Constraint: requires at least one modifier (Ctrl/Alt/Shift) OR a function/special key.
    Returns (canonical_name, win32_mods_with_norepeat, vk_code) or None if invalid.
    """
    if not spec or not isinstance(spec, str):
        return None
    parts = [p.strip() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts or len(parts) > 4:
        return None

    mods = 0
    mod_names: list[str] = []
    key_token = ""
    for p in parts:
        u = p.upper()
        if u in ("CTRL", "CONTROL"):
            if not (mods & MOD_CONTROL):
                mods |= MOD_CONTROL
                mod_names.append("Ctrl")
        elif u in ("ALT", "MENU"):
            if not (mods & MOD_ALT):
                mods |= MOD_ALT
                mod_names.append("Alt")
        elif u == "SHIFT":
            if not (mods & MOD_SHIFT):
                mods |= MOD_SHIFT
                mod_names.append("Shift")
        else:
            if key_token:
                return None  # more than one non-modifier key
            key_token = u

    if not key_token or key_token not in _KEY_VK_MAP:
        return None
    if mods == 0 and key_token not in _MOD_FREE_KEYS:
        return None

    # Canonical modifier order: Ctrl -> Alt -> Shift
    ordered_mods: list[str] = []
    if mods & MOD_CONTROL:
        ordered_mods.append("Ctrl")
    if mods & MOD_ALT:
        ordered_mods.append("Alt")
    if mods & MOD_SHIFT:
        ordered_mods.append("Shift")

    pretty_key = {
        "INSERT": "Insert",
        "HOME": "Home",
        "END": "End",
        "PAGEUP": "PageUp",
        "PAGEDOWN": "PageDown",
        "PRINTSCREEN": "PrintScreen",
        "PAUSE": "Pause",
    }.get(key_token, key_token)

    canonical = "+".join(ordered_mods + [pretty_key])
    return canonical, (mods | MOD_NOREPEAT), _KEY_VK_MAP[key_token]


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

QS_ALLINPUT = 0x04FF
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
PM_REMOVE = 0x0001

STOP_EVENT_NAME = "Local\\WonederInstantReplayStopEvent"
SAVE_EVENT_NAME = "Local\\WonederInstantReplaySaveEvent"
MUTEX_NAME = "Local\\WonederInstantReplaySingleInstance"

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class GlobalHotkeyLoop:
    """
    Runs a low-overhead Win32 message loop that reacts instantaneously to:
    - Global hotkey Alt+F10 (save replay)
    - Global hotkey Ctrl+Alt+F12 (optional quick quit)
    - Named Win32 Events (for `--stop` and `--save` CLI commands)
    """

    def __init__(
        self,
        on_save: Callable[[], None],
        on_periodic_tick: Callable[[], None] | None = None,
        tick_interval_sec: float = 60.0,
        hotkey_name: str = "Alt+F10",
    ) -> None:
        self._on_save = on_save
        self._on_tick = on_periodic_tick
        self._tick_interval = tick_interval_sec
        parsed = parse_hotkey(hotkey_name)
        self._hotkey_name = parsed[0] if parsed else "Alt+F10"
        self._pending_hotkey: str | None = None

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._user32.MsgWaitForMultipleObjects.argtypes = [
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
        self._user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self._user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self._user32.SetWindowsHookExW.restype = wintypes.HHOOK

        self._stop_event = self._kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME)
        self._save_event = self._kernel32.CreateEventW(None, False, False, SAVE_EVENT_NAME)
        self._hook_handle = None
        self._hook_cb = None
        self._last_hook_fire = 0.0
        self.hotkey_mode = "uninitialized"

    def set_hotkey(self, hotkey_name: str) -> None:
        parsed = parse_hotkey(hotkey_name)
        if parsed and parsed[0] != self._hotkey_name:
            self._pending_hotkey = parsed[0]

    def _register_save_hotkey(self, hotkey_save_id: int) -> bool:
        parsed = parse_hotkey(self._hotkey_name)
        if parsed is None:
            canonical, mods, vk = "Alt+F10", (MOD_ALT | MOD_NOREPEAT), VK_F10
        else:
            canonical, mods, vk = parsed
        self._hotkey_name = canonical
        reg_ok = bool(self._user32.RegisterHotKey(None, hotkey_save_id, mods, vk))
        if reg_ok:
            self.hotkey_mode = f"RegisterHotKey({self._hotkey_name})"
            log.info("Registered global hotkey: %s (Win32 RegisterHotKey)", self._hotkey_name)
        else:
            err = ctypes.get_last_error()
            if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                log.warning(
                    "%s is already registered by another app (e.g., NVIDIA Overlay). "
                    "Falling back to external WH_KEYBOARD_LL listener.",
                    self._hotkey_name,
                )
            else:
                log.warning("RegisterHotKey(%s) returned error %d; falling back to WH_KEYBOARD_LL.", self._hotkey_name, err)
            self._install_ll_keyboard_fallback(mods, vk)
        return reg_ok

    def run(self) -> None:
        hotkey_save_id = 1
        hotkey_quit_id = 2

        reg_ok = self._register_save_hotkey(hotkey_save_id)

        # Optional secondary exit hotkey: Ctrl+Alt+F12
        quit_ok = bool(
            self._user32.RegisterHotKey(
                None, hotkey_quit_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F12
            )
        )
        if quit_ok:
            log.info("Registered optional shutdown hotkey: Ctrl+Alt+F12")

        handles = (wintypes.HANDLE * 2)(self._stop_event, self._save_event)
        msg = wintypes.MSG()
        last_tick = time.monotonic()

        try:
            while True:
                # Apply live hotkey change if requested from settings
                if self._pending_hotkey is not None:
                    if reg_ok:
                        self._user32.UnregisterHotKey(None, hotkey_save_id)
                        reg_ok = False
                    if self._hook_handle:
                        self._user32.UnhookWindowsHookEx(self._hook_handle)
                        self._hook_handle = None
                    self._hotkey_name = self._pending_hotkey
                    self._pending_hotkey = None
                    reg_ok = self._register_save_hotkey(hotkey_save_id)

                # Sleep efficiently with ~0% CPU until a Windows message or IPC event arrives
                wait_ms = 500
                rc = self._user32.MsgWaitForMultipleObjects(
                    2, handles, False, wait_ms, QS_ALLINPUT
                )
                if rc == WAIT_OBJECT_0:
                    log.info("Received stop signal via IPC event.")
                    break
                if rc == WAIT_OBJECT_0 + 1:
                    log.info("Received manual save trigger via IPC event.")
                    self._on_save()

                while self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_QUIT:
                        return
                    if msg.message == WM_HOTKEY:
                        if msg.wParam == hotkey_save_id:
                            log.info("Hotkey %s triggered!", self._hotkey_name)
                            self._on_save()
                        elif msg.wParam == hotkey_quit_id:
                            log.info("Hotkey Ctrl+Alt+F12 triggered — shutting down.")
                            return
                    self._user32.TranslateMessage(ctypes.byref(msg))
                    self._user32.DispatchMessageW(ctypes.byref(msg))

                now = time.monotonic()
                if self._on_tick and (now - last_tick >= self._tick_interval):
                    last_tick = now
                    self._on_tick()
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received — shutting down.")
        finally:
            if reg_ok:
                self._user32.UnregisterHotKey(None, hotkey_save_id)
            if quit_ok:
                self._user32.UnregisterHotKey(None, hotkey_quit_id)
            if self._hook_handle:
                self._user32.UnhookWindowsHookEx(self._hook_handle)
                self._hook_handle = None
            for h in (self._stop_event, self._save_event):
                if h:
                    self._kernel32.CloseHandle(h)

    def _install_ll_keyboard_fallback(self, req_mods: int = MOD_ALT, target_vk: int = VK_F10) -> None:
        need_ctrl = bool(req_mods & MOD_CONTROL)
        need_alt = bool(req_mods & MOD_ALT)
        need_shift = bool(req_mods & MOD_SHIFT)

        def _cb(nCode: int, wParam: int, lParam: int) -> int:
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == target_vk:
                    ctrl_ok = (not need_ctrl) or bool(self._user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                    alt_ok = (not need_alt) or bool(self._user32.GetAsyncKeyState(VK_MENU) & 0x8000)
                    shift_ok = (not need_shift) or bool(self._user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                    now = time.monotonic()
                    if ctrl_ok and alt_ok and shift_ok and (now - self._last_hook_fire) > 0.6:
                        self._last_hook_fire = now
                        log.info("Hotkey %s triggered (WH_KEYBOARD_LL fallback)!", self._hotkey_name)
                        self._on_save()
            return self._user32.CallNextHookEx(self._hook_handle, nCode, wParam, lParam)

        self._hook_cb = HOOKPROC(_cb)
        self._hook_handle = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_cb, None, 0)
        if self._hook_handle:
            self.hotkey_mode = f"WH_KEYBOARD_LL({self._hotkey_name})"
        else:
            self.hotkey_mode = "ipc-only"
            log.error("Failed to install WH_KEYBOARD_LL fallback: err=%d", ctypes.get_last_error())


def acquire_single_instance_mutex() -> wintypes.HANDLE | None:
    """Ensure only one replay daemon runs at a time."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(handle)
        return None
    return handle


def signal_named_event(event_name: str) -> bool:
    """Signal a running daemon's Win32 Named Event (stop or save)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.SetEvent.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    EVENT_MODIFY_STATE = 0x0002
    h = k32.OpenEventW(EVENT_MODIFY_STATE, False, event_name)
    if not h:
        return False
    try:
        return bool(k32.SetEvent(h))
    finally:
        k32.CloseHandle(h)
