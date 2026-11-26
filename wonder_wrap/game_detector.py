"""
100% external, anti-cheat safe Windows game window detector (pure ctypes).

Detects when a game is actively focused (fullscreen, borderless, or windowed),
ignores Xbox Game Bar overlays (so pressing Win+G never interrupts recording),
and returns the exact physical pixel client rect of the game window clamped to
the primary display.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import dataclasses
import json
import logging
import os
import time
import winreg

from .win32_dxgi import enable_dpi_awareness

log = logging.getLogger("woneder.gamedetect")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
QUNS_RUNNING_D3D_FULL_SCREEN = 3
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

# Universal Win32 window class prefixes/names used by non-game desktop UI frameworks
# (Chromium/Electron/WebView2, WPF, WinForms, UWP Shell, Qt, Firefox, Windows Terminal, etc.).
# Real 3D games use native Win32/DirectX/Vulkan/SDL/Unity/Unreal/GLFW classes, never these.
NON_GAME_WINDOW_CLASS_PREFIXES = (
    "chrome_widgetwin_",          # ALL Electron, CEF, WebView2, Chrome, Edge, Brave, Opera, VS Code, Discord, Spotify, etc.
    "hwndwrapper[",               # ALL .NET WPF desktop apps (Playnite, Visual Studio, launchers, utilities)
    "windowsforms10.",            # ALL .NET WinForms desktop apps
    "qt5qwindow",                 # Qt5 desktop apps
    "qt6qwindow",                 # Qt6 desktop apps
    "wxwindow",                   # wxWidgets desktop apps
    "avalonia-",                  # Avalonia UI desktop apps
    "tauri",                      # Tauri webview desktop apps
)

NON_GAME_WINDOW_CLASSES = {
    "applicationframewindow",     # Standard UWP desktop apps (Settings, Photos, Store, etc.)
    "cabinetwclass",              # File Explorer
    "explorewclass",
    "progman",
    "workerw",
    "shell_traywnd",
    "mozillawindowclass",         # Firefox / Gecko browsers
    "cascadia_hosting_window_class",  # Windows Terminal
    "consolewindowclass",         # cmd / PowerShell
    "notepad",
    "opusapp",                    # Microsoft Word
    "xlmain",                     # Microsoft Excel
    "pptframeclass",              # Microsoft PowerPoint
}

# Known native 3D game engine window classes (exact or prefix, lowercased)
GAME_ENGINE_WINDOW_CLASSES = (
    "unrealwindow",               # Unreal Engine 4/5
    "launchunrealuwindowsclient", # Unreal Engine 3
    "unitywndclass",              # Unity Engine
    "sdl_app",                    # SDL2/SDL3 (CS2, Dota 2, Valve Source 2, many PC games)
    "glfw30",                     # GLFW (Minecraft Java, OpenGL/Vulkan games)
    "lwjgl",                      # LWJGL (Minecraft Java)
    "windowsclient",              # Roblox
    "grcwindow",                  # Rockstar RAGE (GTA V, RDR2)
    "riotwindowclass",            # Riot Games
    "tankwindowclass",            # Overwatch
    "respawn001",                 # Apex Legends / Titanfall
    "cryengine",                  # CryEngine
    "frostbite",                  # Frostbite Engine
    "godot_engine",               # Godot Engine
)
# System directory prefixes that never contain user games
SYSTEM_PATH_PREFIXES = (
    "c:\\windows\\system32\\",
    "c:\\windows\\syswow64\\",
    "c:\\windows\\systemapps\\",
    "c:\\windows\\immersivecontrolpanel\\",
    "c:\\windows\\explorer.exe",
)

# Overlays that appear on top of a game — keep the current game locked when these are focused
OVERLAY_EXES = {
    "gamebar.exe",
    "gamebarftserver.exe",
    "wondergamebarwidget.exe",
    "wonederGameBarWidget.exe".lower(),
    "nvidia overlay.exe",
    "nvcontainer.exe",
    "rtss.exe",
}

# Desktop, shell, browser, IDE, chat, and utility apps that are never games
NON_GAME_EXES = {
    "explorer.exe",
    "dwm.exe",
    "applicationframehost.exe",
    "textinputhost.exe",
    "widgets.exe",
    "widgetservice.exe",
    "phoneexperiencehost.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "searchapp.exe",
    "lockapp.exe",
    "logonui.exe",
    "systemsettings.exe",
    "taskmgr.exe",
    "mmc.exe",
    "regedit.exe",
    "control.exe",
    "msiexec.exe",
    "consent.exe",
    "credentialuibroker.exe",
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "opera_gx.exe",
    "vivaldi.exe",
    "arc.exe",
    "code.exe",
    "antigravity.exe",
    "cursor.exe",
    "devenv.exe",
    "rider64.exe",
    "idea64.exe",
    "pycharm64.exe",
    "windowsterminal.exe",
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "conhost.exe",
    "notepad.exe",
    "notepad++.exe",
    "discord.exe",
    "slack.exe",
    "ms-teams.exe",
    "teams.exe",
    "telegram.exe",
    "whatsapp.exe",
    "spotify.exe",
    "vlc.exe",
    "mpv.exe",
    "obs64.exe",
    "obs32.exe",
    "streamlabs obs.exe",
    "steam.exe",
    "steamwebhelper.exe",
    "epicgameslauncher.exe",
    "riotclientux.exe",
    "riotclientservices.exe",
    "battle.net.exe",
    "eadesktop.exe",
    "upc.exe",
    "galaxyclient.exe",
    "xboxpcapp.exe",
    "playnite.desktopapp.exe",
    "playnite.fullscreenapp.exe",
    "voidstrap.exe",
    "bloxstrap.exe",
    "robloxcrashhandler.exe",
    "robloxstudiobeta.exe",
    "python.exe",
    "pythonw.exe",
    "wonderwrapdaemon.exe",
    "wonederreplaydaemon.exe",
    "wonderwatch.exe",
    "githubdesktop.exe",
    "winword.exe",
    "excel.exe",
    "powerpnt.exe",
    "acrobat.exe",
    "msedgewebview2.exe",
}

# Known game installation directory substrings (lowercased)
GAME_PATH_MARKERS = (
    "\\steamapps\\common\\",
    "\\epic games\\",
    "\\riot games\\",
    "\\xboxgames\\",
    "\\ubisoft game launcher\\games\\",
    "\\ea games\\",
    "\\origin games\\",
    "\\gog galaxy\\games\\",
    "\\battle.net\\",
    "\\roblox\\versions\\",
    "\\rblxversions\\",
    "\\voidstrap\\versions\\",
    "\\bloxstrap\\versions\\",
    "\\hoyoverse\\",
    "\\genshin impact\\",
    "\\rockstar games\\",
    "\\blizzard\\",
    "\\program files\\windowsapps\\microsoft.minecraft",
)

# Common game executables (lowercased)
KNOWN_GAME_EXES = {
    "robloxplayerbeta.exe",
    "valorant-win64-shipping.exe",
    "cs2.exe",
    "csgo.exe",
    "dota2.exe",
    "league of legends.exe",
    "fortniteclient-win64-shipping.exe",
    "r5apex.exe",
    "overwatch.exe",
    "gta5.exe",
    "gtav.exe",
    "eldenring.exe",
    "cyberpunk2077.exe",
    "minecraft.windows.exe",
    "genshinimpact.exe",
    "starrail.exe",
    "zenlesszonezero.exe",
    "rocketleague.exe",
    "rainbowsix.exe",
    "rainbowsix_vulkan.exe",
    "cod.exe",
    "destiny2.exe",
    "warframe.x64.exe",
    "helldivers2.exe",
    "bg3.exe",
    "bg3_dx11.exe",
    "palworld-win64-shipping.exe",
    "marvel-win64-shipping.exe",
}


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


@dataclasses.dataclass
class GameWindowInfo:
    hwnd: int
    pid: int
    exe_name: str
    exe_path: str
    title: str
    x: int
    y: int
    width: int
    height: int
    is_fullscreen: bool

    def geometry_differs(self, other: "GameWindowInfo", tol: int = 8) -> bool:
        return (
            abs(self.x - other.x) > tol
            or abs(self.y - other.y) > tol
            or abs(self.width - other.width) > tol
            or abs(self.height - other.height) > tol
        )


class GameDetector:
    """Monitors the foreground Win32 window and detects active games safely."""

    def __init__(self, screen_width: int, screen_height: int, config_dir: str = "") -> None:
        enable_dpi_awareness()
        self.screen_w = max(2, screen_width)
        self.screen_h = max(2, screen_height)
        self._config_path = os.path.join(config_dir, "games_config.json") if config_dir else ""

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

        self._dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        self._user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.GetWindowLongW.restype = wintypes.LONG

        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowTextW.restype = wintypes.int if hasattr(wintypes, "int") else ctypes.c_int
        self._user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetClientRect.restype = wintypes.BOOL
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
        self._user32.ClientToScreen.restype = wintypes.BOOL
        self._enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._user32.EnumWindows.argtypes = [self._enum_proc_type, wintypes.LPARAM]
        self._user32.EnumWindows.restype = wintypes.BOOL

        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        self._shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
        self._shell32.SHQueryUserNotificationState.restype = ctypes.c_long

        self._gameconfig_paths: set[str] = set()
        self._gameconfig_exes: set[str] = set()
        self._custom_allow: set[str] = set()
        self._custom_deny: set[str] = set()
        self._last_refresh = 0.0
        self._refresh_databases()

    def _refresh_databases(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh < 30.0:
            return
        self._last_refresh = now

        # 1. Read Windows' own GameConfigStore (games recognized by Xbox Game Bar)
        paths: set[str] = set()
        exes: set[str] = set()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children") as root:
                i = 0
                while True:
                    try:
                        subname = winreg.EnumKey(root, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(root, subname) as child:
                            val, _ = winreg.QueryValueEx(child, "MatchedExeFullPath")
                            if isinstance(val, str) and val:
                                low = val.lower()
                                paths.add(low)
                                exes.add(os.path.basename(low))
                    except OSError:
                        pass
        except OSError:
            pass
        self._gameconfig_paths = paths
        self._gameconfig_exes = exes

        # 2. Optional user overrides: Videos/Replays/games_config.json
        if self._config_path and os.path.isfile(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._custom_allow = {str(x).lower() for x in data.get("allow", [])}
                self._custom_deny = {str(x).lower() for x in data.get("deny", [])}
            except Exception:
                pass

    def _get_exe_path(self, pid: int) -> str:
        if pid <= 0:
            return ""
        h = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if self._kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            self._kernel32.CloseHandle(h)

    def _get_window_title(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self._user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value.strip()

    def _get_window_class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(hwnd, buf, 256)
        return buf.value.strip().lower()

    def _is_non_game_window_class(self, win_class: str, title_low: str) -> bool:
        if not win_class:
            return False
        # Minecraft Bedrock runs inside ApplicationFrameWindow
        if win_class == "applicationframewindow" and "minecraft" in title_low:
            return False
        if win_class in NON_GAME_WINDOW_CLASSES:
            return True
        return any(win_class.startswith(p) for p in NON_GAME_WINDOW_CLASS_PREFIXES)

    def _is_game_engine_class(self, win_class: str) -> bool:
        if not win_class:
            return False
        return any(win_class == g or win_class.startswith(g) for g in GAME_ENGINE_WINDOW_CLASSES)

    def add_ignored_exe(self, exe_name: str) -> None:
        """Persistently ignore an executable that the user marked as 'Not a game'."""
        low = exe_name.strip().lower()
        if not low:
            return
        self._custom_deny.add(low)
        self._custom_allow.discard(low)
        self._save_custom_config()

    def _save_custom_config(self) -> None:
        if not self._config_path:
            return
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"allow": sorted(self._custom_allow), "deny": sorted(self._custom_deny)},
                    f,
                    indent=2,
                )
        except Exception:
            pass

    def _is_d3d_fullscreen(self) -> bool:
        state = ctypes.c_int(0)
        if self._shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value == QUNS_RUNNING_D3D_FULL_SCREEN
        return False

    def get_window_capture_rect(self, hwnd: int) -> tuple[int, int, int, int, bool] | None:
        """
        Return (x, y, w, h, is_fullscreen) of the window's client area clamped to
        the primary monitor [0..screen_w, 0..screen_h] with even dimensions.
        """
        if not hwnd or not self._user32.IsWindow(hwnd) or not self._user32.IsWindowVisible(hwnd):
            return None
        if self._user32.IsIconic(hwnd):
            return None

        crect = wintypes.RECT()
        if not self._user32.GetClientRect(hwnd, ctypes.byref(crect)):
            return None
        cw = crect.right - crect.left
        ch = crect.bottom - crect.top
        if cw < 320 or ch < 240:
            return None

        pt = POINT(crect.left, crect.top)
        if not self._user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None

        x1 = max(0, pt.x)
        y1 = max(0, pt.y)
        x2 = min(self.screen_w, pt.x + cw)
        y2 = min(self.screen_h, pt.y + ch)

        w = (x2 - x1) & ~1  # H.264 / NV12 require even dimensions
        h = (y2 - y1) & ~1
        if w < 320 or h < 240:
            return None

        is_fs = (w >= self.screen_w - 4) and (h >= self.screen_h - 4)
        if is_fs:
            return 0, 0, self.screen_w & ~1, self.screen_h & ~1, True
        return x1, y1, w, h, False

    def is_overlay_foreground(self) -> bool:
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe_path = self._get_exe_path(pid.value)
        if not exe_path:
            return False
        return os.path.basename(exe_path).lower() in OVERLAY_EXES

    def _is_window_cloaked(self, hwnd: int) -> bool:
        """Return True if DWM has cloaked this window (e.g. hidden UWP TextInputHost/Widgets)."""
        cloaked = wintypes.DWORD(0)
        hr = self._dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return hr == 0 and cloaked.value != 0

    def _is_tool_or_noactivate_window(self, hwnd: int) -> bool:
        exstyle = self._user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        return bool(exstyle & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE))

    def _is_system_exe(self, exe_low: str) -> bool:
        if any(exe_low.startswith(p) for p in SYSTEM_PATH_PREFIXES):
            return True
        if "\\windowsapps\\microsoft." in exe_low and "minecraft" not in exe_low and "forza" not in exe_low and "halo" not in exe_low:
            return True
        return False

    def is_window_alive(self, hwnd: int) -> bool:
        """Return True as long as the game window still exists (even if minimized on Alt+Tab)."""
        return bool(hwnd and self._user32.IsWindow(hwnd))

    def _inspect_hwnd_as_game(self, hwnd: int, is_foreground: bool, allow_preferred: bool = False) -> GameWindowInfo | None:
        if not hwnd or not self._user32.IsWindow(hwnd) or not self._user32.IsWindowVisible(hwnd):
            return None
        if self._is_window_cloaked(hwnd):
            return None
        if self._is_tool_or_noactivate_window(hwnd):
            return None
        # Allow a minimized window ONLY if it is already our locked game (keeps WGC alive on Alt+Tab)
        is_iconic = bool(self._user32.IsIconic(hwnd))
        if is_iconic and not allow_preferred:
            return None

        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value <= 0:
            return None

        exe_path = self._get_exe_path(pid.value)
        if not exe_path:
            return None

        exe_low = exe_path.lower()
        exe_name = os.path.basename(exe_path)
        exe_name_low = exe_name.lower()

        if self._is_system_exe(exe_low):
            return None
        if exe_name_low in OVERLAY_EXES or exe_name_low in NON_GAME_EXES or exe_name_low in self._custom_deny:
            return None

        if is_iconic:
            x, y, w, h, is_fs = 0, 0, self.screen_w & ~1, self.screen_h & ~1, True
        else:
            rect = self.get_window_capture_rect(hwnd)
            if rect is None:
                return None
            x, y, w, h, is_fs = rect
        title = self._get_window_title(hwnd)
        title_low = title.lower()
        win_class = self._get_window_class(hwnd)

        # Universal structural rejection: Electron/Chromium/WPF/WinForms/Qt/Shell/UWP UI apps
        # are NEVER 3D games (unless explicitly user-allowed in custom_allow).
        if exe_name_low not in self._custom_allow and self._is_non_game_window_class(win_class, title_low):
            return None

        is_game = False
        if exe_name_low in self._custom_allow:
            is_game = True
        elif self._is_game_engine_class(win_class):
            is_game = True
        elif exe_name_low in KNOWN_GAME_EXES:
            is_game = True
        elif exe_name_low == "javaw.exe" and "minecraft" in title_low:
            is_game = True
        elif exe_low in self._gameconfig_paths or exe_name_low in self._gameconfig_exes:
            is_game = True
        elif any(marker in exe_low for marker in GAME_PATH_MARKERS):
            is_game = True
        elif is_foreground and self._is_d3d_fullscreen():
            is_game = True
        elif (is_foreground or allow_preferred) and is_fs and bool(title):
            # Native Win32 borderless-fullscreen window (already passed UI-framework class filter)
            is_game = True

        if not is_game:
            return None

        return GameWindowInfo(
            hwnd=int(hwnd),
            pid=int(pid.value),
            exe_name=exe_name,
            exe_path=exe_path,
            title=title or exe_name,
            x=x,
            y=y,
            width=w,
            height=h,
            is_fullscreen=is_fs,
        )

    def detect_foreground_game(self) -> GameWindowInfo | None:
        """Return GameWindowInfo if the foreground window is a game, else None."""
        self._refresh_databases()
        fg = self._user32.GetForegroundWindow()
        return self._inspect_hwnd_as_game(fg, is_foreground=True)

    def find_running_game(self, preferred_hwnd: int = 0) -> GameWindowInfo | None:
        """
        Find an active game window regardless of which window is currently focused:
        1. If the foreground window is a game, prefer it.
        2. If `preferred_hwnd` is still a live visible game window, keep it locked.
        3. Otherwise scan visible non-cloaked top-level windows via EnumWindows for
           known/registered games (never false-matching hidden Windows system hosts).
        """
        self._refresh_databases()

        fg = self._user32.GetForegroundWindow()
        fg_game = self._inspect_hwnd_as_game(fg, is_foreground=True)
        if fg_game is not None:
            return fg_game

        if preferred_hwnd:
            kept = self._inspect_hwnd_as_game(preferred_hwnd, is_foreground=False, allow_preferred=True)
            if kept is not None:
                return kept

        found: list[GameWindowInfo] = []

        def _cb(hwnd: int, _lparam: int) -> bool:
            info = self._inspect_hwnd_as_game(hwnd, is_foreground=False, allow_preferred=False)
            if info is not None:
                found.append(info)
                return False  # Stop on top-most visible game window in Z-order
            return True

        cb_ref = self._enum_proc_type(_cb)
        self._user32.EnumWindows(cb_ref, 0)
        return found[0] if found else None
