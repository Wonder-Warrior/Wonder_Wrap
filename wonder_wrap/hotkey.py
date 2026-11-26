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

log = logging.getLogger("wonder_wrap.hotkey")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
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
        elif u in ("WIN", "WINDOWS", "LWIN", "RWIN", "META"):
            if not (mods & MOD_WIN):
                mods |= MOD_WIN
                mod_names.append("Win")
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

    # Canonical modifier order: Ctrl -> Win -> Alt -> Shift
    ordered_mods: list[str] = []
    if mods & MOD_CONTROL:
        ordered_mods.append("Ctrl")
    if mods & MOD_WIN:
        ordered_mods.append("Win")
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
        hotkey_name: str = "Win+Alt+G",
        on_record: Callable[[], None] | None = None,
        record_hotkey_name: str = "Win+Alt+R",
        on_screenshot: Callable[[], None] | None = None,
        screenshot_hotkey_name: str = "Win+Alt+PrintScreen",
        on_mic: Callable[[], None] | None = None,
        mic_hotkey_name: str = "Win+Alt+M",
    ) -> None:
        self._on_save = on_save
        self._on_tick = on_periodic_tick
        self._tick_interval = tick_interval_sec
        self._callbacks: dict[str, Callable[[], None] | None] = {
            "save": on_save,
            "record": on_record,
            "screenshot": on_screenshot,
            "mic": on_mic,
        }
        self._hotkeys: dict[str, str] = {
            "save": (parse_hotkey(hotkey_name) or ("Win+Alt+G", 0, 0))[0],
            "record": (parse_hotkey(record_hotkey_name) or ("Win+Alt+R", 0, 0))[0],
            "screenshot": (parse_hotkey(screenshot_hotkey_name) or ("Win+Alt+PrintScreen", 0, 0))[0],
            "mic": (parse_hotkey(mic_hotkey_name) or ("Win+Alt+M", 0, 0))[0],
        }
        self._hotkey_name = self._hotkeys["save"]
        self._pending_hotkeys: dict[str, str] | None = None
        self._reg_ids: dict[int, str] = {}
        self._ll_bindings: list[tuple[str, str, int, int]] = []

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
        self.set_hotkeys({"save": hotkey_name})

    def set_hotkeys(self, updates: dict[str, str]) -> None:
        merged = dict(self._hotkeys)
        changed = False
        for action, spec in updates.items():
            if action not in merged:
                continue
            parsed = parse_hotkey(spec)
            if parsed and parsed[0] != merged[action]:
                merged[action] = parsed[0]
                changed = True
        if changed:
            self._pending_hotkeys = merged

    def _register_all_hotkeys(self) -> None:
        self._reg_ids.clear()
        self._ll_bindings.clear()
        modes: list[str] = []
        action_order = ("save", "record", "screenshot", "mic")
        for idx, action in enumerate(action_order, start=1):
            cb = self._callbacks.get(action)
            if cb is None:
                continue
            spec = self._hotkeys[action]
            parsed = parse_hotkey(spec)
            if parsed is None:
                continue
            canonical, mods, vk = parsed
            self._hotkeys[action] = canonical
            if action == "save":
                self._hotkey_name = canonical
            reg_ok = bool(self._user32.RegisterHotKey(None, idx, mods, vk))
            if reg_ok:
                self._reg_ids[idx] = action
                modes.append(f"{action}:{canonical}")
                log.info("Registered %s hotkey: %s (Win32 RegisterHotKey)", action, canonical)
            else:
                self._ll_bindings.append((action, canonical, mods, vk))
                modes.append(f"{action}:LL({canonical})")
                log.info("Registered %s hotkey: %s (WH_KEYBOARD_LL fallback)", action, canonical)
        if self._ll_bindings:
            self._install_ll_keyboard_fallback()
        self.hotkey_mode = ", ".join(modes) if modes else "ipc-only"

    def _unregister_all_hotkeys(self) -> None:
        for hid in list(self._reg_ids.keys()):
            self._user32.UnregisterHotKey(None, hid)
        self._reg_ids.clear()
        self._ll_bindings.clear()
        if self._hook_handle:
            self._user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None

    def run(self) -> None:
        hotkey_quit_id = 99

        self._register_all_hotkeys()

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
                if self._pending_hotkeys is not None:
                    self._unregister_all_hotkeys()
                    self._hotkeys = self._pending_hotkeys
                    self._pending_hotkeys = None
                    self._register_all_hotkeys()

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
                        if msg.wParam in self._reg_ids:
                            action = self._reg_ids[msg.wParam]
                            cb = self._callbacks.get(action)
                            log.info("Hotkey %s (%s) triggered!", action, self._hotkeys.get(action))
                            if cb:
                                cb()
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
            self._unregister_all_hotkeys()
            if quit_ok:
                self._user32.UnregisterHotKey(None, hotkey_quit_id)
            for h in (self._stop_event, self._save_event):
                if h:
                    self._kernel32.CloseHandle(h)

    def _install_ll_keyboard_fallback(self) -> None:
        bindings = list(self._ll_bindings)

        def _cb(nCode: int, wParam: int, lParam: int) -> int:
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                for action, canonical, req_mods, target_vk in bindings:
                    if kb.vkCode != target_vk:
                        continue
                    need_ctrl = bool(req_mods & MOD_CONTROL)
                    need_alt = bool(req_mods & MOD_ALT)
                    need_shift = bool(req_mods & MOD_SHIFT)
                    need_win = bool(req_mods & MOD_WIN)
                    ctrl_ok = (not need_ctrl) or bool(self._user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                    alt_ok = (not need_alt) or bool(self._user32.GetAsyncKeyState(VK_MENU) & 0x8000)
                    shift_ok = (not need_shift) or bool(self._user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                    win_ok = (not need_win) or bool(
                        (self._user32.GetAsyncKeyState(VK_LWIN) & 0x8000)
                        or (self._user32.GetAsyncKeyState(VK_RWIN) & 0x8000)
                    )
                    now = time.monotonic()
                    if ctrl_ok and alt_ok and shift_ok and win_ok and (now - self._last_hook_fire) > 0.6:
                        self._last_hook_fire = now
                        log.info("Hotkey %s (%s) triggered (WH_KEYBOARD_LL fallback)!", action, canonical)
                        cb = self._callbacks.get(action)
                        if cb:
                            cb()
                        break
            return self._user32.CallNextHookEx(self._hook_handle, nCode, wParam, lParam)

        self._hook_cb = HOOKPROC(_cb)
        self._hook_handle = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_cb, None, 0)
        if not self._hook_handle:
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
