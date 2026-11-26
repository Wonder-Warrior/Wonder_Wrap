"""Win32 helpers: DXGI adapter/display detection, Videos folder, RAM stats, FFmpeg lookup."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import winreg


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> "GUID":
        s = s.strip("{}").replace("-", "")
        d1 = int(s[0:8], 16)
        d2 = int(s[8:12], 16)
        d3 = int(s[12:16], 16)
        d4 = (ctypes.c_ubyte * 8)(*[int(s[16 + i * 2 : 18 + i * 2], 16) for i in range(8)])
        return cls(d1, d2, d3, d4)


def vtable_call(ptr: ctypes.c_void_p, index: int, restype, *argtypes):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return fn_type(vtable[index])


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", LUID),
        ("Flags", wintypes.UINT),
    ]


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", wintypes.WCHAR * 32),
        ("DesktopCoordinates", wintypes.RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", ctypes.c_int),
        ("Monitor", wintypes.HMONITOR),
    ]


@dataclasses.dataclass
class DisplayTarget:
    adapter_index: int
    adapter_name: str
    vendor_id: int
    output_index: int
    device_name: str
    width: int
    height: int


def enable_dpi_awareness() -> None:
    """Ensure DXGI / GetSystemMetrics report physical pixel dimensions."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def detect_primary_display_adapter() -> DisplayTarget:
    """
    Enumerate DXGI adapters/outputs and return the adapter index that owns
    the primary desktop monitor (typically AMD iGPU adapter 0 on Optimus laptops).
    """
    enable_dpi_awareness()
    dxgi = ctypes.WinDLL("dxgi.dll")
    iid_factory1 = GUID.from_str("{770aae78-f26f-4dba-a829-253c83d1b387}")
    factory = ctypes.c_void_p()
    hr = dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory))
    if hr != 0:
        return DisplayTarget(0, "Default Adapter", 0, 0, r"\\.\DISPLAY1", 1920, 1080)

    enum_adapters1 = vtable_call(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
    release_factory = vtable_call(factory, 2, wintypes.ULONG)

    fallback: DisplayTarget | None = None
    try:
        idx = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters1(factory, idx, ctypes.byref(adapter)) != 0:
                break
            try:
                desc = DXGI_ADAPTER_DESC1()
                vtable_call(adapter, 10, ctypes.c_long, ctypes.POINTER(DXGI_ADAPTER_DESC1))(
                    adapter, ctypes.byref(desc)
                )
                enum_outputs = vtable_call(
                    adapter, 7, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)
                )
                oidx = 0
                while True:
                    output = ctypes.c_void_p()
                    if enum_outputs(adapter, oidx, ctypes.byref(output)) != 0:
                        break
                    try:
                        odesc = DXGI_OUTPUT_DESC()
                        vtable_call(output, 7, ctypes.c_long, ctypes.POINTER(DXGI_OUTPUT_DESC))(
                            output, ctypes.byref(odesc)
                        )
                        if odesc.AttachedToDesktop:
                            r = odesc.DesktopCoordinates
                            target = DisplayTarget(
                                adapter_index=idx,
                                adapter_name=desc.Description,
                                vendor_id=desc.VendorId,
                                output_index=oidx,
                                device_name=odesc.DeviceName,
                                width=r.right - r.left,
                                height=r.bottom - r.top,
                            )
                            if r.left == 0 and r.top == 0:
                                return target
                            if fallback is None:
                                fallback = target
                    finally:
                        vtable_call(output, 2, wintypes.ULONG)(output)
                    oidx += 1
            finally:
                vtable_call(adapter, 2, wintypes.ULONG)(adapter)
            idx += 1
    finally:
        release_factory(factory)

    if fallback is not None:
        return fallback
    return DisplayTarget(0, "Default Adapter", 0, 0, r"\\.\DISPLAY1", 1920, 1080)


def detect_nvidia_adapter_index() -> tuple[int | None, str]:
    """
    Find the DXGI adapter index of the NVIDIA discrete GPU (VendorId == 0x10DE)
    so h264_nvenc can run via D3D11 (avoiding ~100 MB of CUDA context RAM bloat).
    """
    dxgi = ctypes.WinDLL("dxgi.dll")
    iid_factory1 = GUID.from_str("{770aae78-f26f-4dba-a829-253c83d1b387}")
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory)) != 0:
        return None, ""

    enum_adapters1 = vtable_call(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
    release_factory = vtable_call(factory, 2, wintypes.ULONG)
    try:
        idx = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters1(factory, idx, ctypes.byref(adapter)) != 0:
                break
            try:
                desc = DXGI_ADAPTER_DESC1()
                vtable_call(adapter, 10, ctypes.c_long, ctypes.POINTER(DXGI_ADAPTER_DESC1))(
                    adapter, ctypes.byref(desc)
                )
                if desc.VendorId == 0x10DE:
                    return idx, desc.Description
            finally:
                vtable_call(adapter, 2, wintypes.ULONG)(adapter)
            idx += 1
    finally:
        release_factory(factory)
    return None, ""


@dataclasses.dataclass(frozen=True)
class EncoderProfile:
    codec: str               # h264_nvenc | h264_amf | h264_qsv | h264_mf | libx264
    hw_adapter_index: int | None
    gpu_name: str
    use_d3d11_upload: bool


def detect_best_video_encoder() -> EncoderProfile:
    """
    Auto-detect the best available hardware H.264 encoder across ANY Windows PC:
      1. NVIDIA GPU (0x10DE) -> h264_nvenc via D3D11 (ultra-low RAM, zero CUDA bloat)
      2. AMD GPU    (0x1002) -> h264_amf
      3. Intel GPU  (0x8086) -> h264_qsv
      4. Fallback            -> h264_mf (Windows Media Foundation) / libx264
    """
    dxgi = ctypes.WinDLL("dxgi.dll")
    iid_factory1 = GUID.from_str("{770aae78-f26f-4dba-a829-253c83d1b387}")
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory)) != 0:
        return EncoderProfile("libx264", None, "Software CPU", False)

    enum_adapters1 = vtable_call(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
    release_factory = vtable_call(factory, 2, wintypes.ULONG)
    adapters: list[tuple[int, int, str]] = []
    try:
        idx = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters1(factory, idx, ctypes.byref(adapter)) != 0:
                break
            try:
                desc = DXGI_ADAPTER_DESC1()
                vtable_call(adapter, 10, ctypes.c_long, ctypes.POINTER(DXGI_ADAPTER_DESC1))(
                    adapter, ctypes.byref(desc)
                )
                adapters.append((idx, int(desc.VendorId), desc.Description))
            finally:
                vtable_call(adapter, 2, wintypes.ULONG)(adapter)
            idx += 1
    finally:
        release_factory(factory)

    for idx, vid, name in adapters:
        if vid == 0x10DE:
            return EncoderProfile("h264_nvenc", idx, name, True)
    for idx, vid, name in adapters:
        if vid == 0x1002:
            return EncoderProfile("h264_amf", idx, name, False)
    for idx, vid, name in adapters:
        if vid == 0x8086:
            return EncoderProfile("h264_qsv", idx, name, False)
    return EncoderProfile("h264_mf", None, "Windows Media Foundation", False)


def get_settings_path() -> str:
    local_app = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    base = os.path.join(local_app, "WonderWrap")
    os.makedirs(base, exist_ok=True)
    new_path = os.path.join(base, "settings.json")
    if not os.path.isfile(new_path):
        # Seamlessly migrate settings from previous install path if present
        old_path = os.path.join(local_app, "WonederInstantReplay", "settings.json")
        if os.path.isfile(old_path):
            try:
                shutil.copy2(old_path, new_path)
            except OSError:
                pass
    return new_path


def load_user_settings() -> dict:
    path = get_settings_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_user_settings(updates: dict) -> dict:
    data = load_user_settings()
    data.update(updates)
    path = get_settings_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    return data


def pick_folder_dialog(initial_dir: str = "") -> str | None:
    """Open a native Windows Folder Browser dialog and return the selected path (or None if cancelled)."""
    safe_init = initial_dir.replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f = New-Object System.Windows.Forms.Form;"
        "$f.TopMost = $true;"
        "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$dlg.Description = 'Choose Wonder Wrap capture folder';"
        "$dlg.ShowNewFolderButton = $true;"
        + (f"if (Test-Path '{safe_init}') {{ $dlg.SelectedPath = '{safe_init}' }};" if safe_init else "")
        + "if ($dlg.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dlg.SelectedPath }"
    )
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True,
            text=True,
            creationflags=0x08000000,
            timeout=120,
        )
        chosen = res.stdout.strip()
        if chosen and os.path.isdir(chosen):
            return chosen
    except Exception:
        pass
    return None


def get_thread_desktop_name() -> str:
    """Return the current Win32 desktop name (normal interactive user sessions use 'Default')."""
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hd = u32.GetThreadDesktop(k32.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(256)
        # UOI_NAME = 2
        if u32.GetUserObjectInformationW(hd, 2, buf, 512, None):
            return buf.value
    except Exception:
        pass
    return "unknown"


def get_videos_dir() -> str:
    """Resolve the user's real Windows Videos folder (respecting folder redirection)."""
    try:
        shell32 = ctypes.WinDLL("shell32.dll")
        ole32 = ctypes.WinDLL("ole32.dll")
        # FOLDERID_Videos = {18989B1D-99B5-455B-841C-AB7C74E4DDFC}
        folder_id = GUID.from_str("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}")
        path_ptr = wintypes.LPWSTR()
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr))
        if hr == 0 and path_ptr.value:
            res = path_ptr.value
            ole32.CoTaskMemFree(path_ptr)
            return res
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Videos")


def find_ffmpeg_binary(name: str = "ffmpeg") -> str:
    """
    Find ffmpeg/ffprobe:
    1. Bundled right next to the standalone executable / project folder (for standalone distribution)
    2. Current PATH
    3. Freshly read Windows Machine+User PATH / WinGet packages
    """
    exe_name = f"{name}.exe"
    candidate_dirs: list[str] = []
    if getattr(sys, "frozen", False):
        candidate_dirs.append(os.path.dirname(sys.executable))
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidate_dirs.append(meipass)
    else:
        candidate_dirs.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for d in candidate_dirs:
        local_bin = os.path.join(d, exe_name)
        if os.path.isfile(local_bin):
            return local_bin
        nested_bin = os.path.join(d, "bin", exe_name)
        if os.path.isfile(nested_bin):
            return nested_bin

    found = shutil.which(name)
    if found:
        return found

    # Refresh PATH from registry (handles recently winget-installed FFmpeg without shell restart)
    paths: list[str] = []
    for root, subkey in [
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ]:
        try:
            with winreg.OpenKey(root, subkey) as key:
                val, _ = winreg.QueryValueEx(key, "Path")
                for entry in val.split(";"):
                    entry = os.path.expandvars(entry.strip())
                    if entry:
                        paths.append(entry)
        except OSError:
            pass

    winget_links = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links")
    if os.path.isdir(winget_links):
        paths.append(winget_links)

    search_path = ";".join(paths)
    found = shutil.which(name, path=search_path)
    if found:
        return found

    # Direct fallback scan inside WinGet Gyan.FFmpeg package folder
    winget_pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_pkgs):
        exe_name = f"{name}.exe"
        for dirpath, _, filenames in os.walk(winget_pkgs):
            if exe_name in filenames:
                return os.path.join(dirpath, exe_name)

    raise FileNotFoundError(
        f"Could not find '{name}.exe' in PATH. Install FFmpeg (e.g. `winget install Gyan.FFmpeg`)."
    )


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESS_MEMORY_COUNTERS_EX2(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
        ("PrivateWorkingSetSize", ctypes.c_size_t),
        ("SharedCommitUsage", ctypes.c_uint64),
    ]


@dataclasses.dataclass
class ProcessMemoryMB:
    private_ws_mb: float      # Exact Task Manager "Memory" (Private Working Set)
    working_set_mb: float     # Total Working Set (includes shared OS/GPU DLLs)
    private_mb: float         # Private Commit
    peak_working_set_mb: float


def get_process_memory_mb(pid: int | None = None, proc_handle: int | None = None) -> ProcessMemoryMB:
    """Query exact Private Working Set (Task Manager RAM) and Total Working Set (in MB)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    close_after = False
    if proc_handle:
        handle = wintypes.HANDLE(int(proc_handle))
    elif pid is None or pid == os.getpid():
        handle = k32.GetCurrentProcess()
    elif pid <= 0:
        return ProcessMemoryMB(0.0, 0.0, 0.0, 0.0)
    else:
        # PROCESS_QUERY_INFORMATION (0x0400) | PROCESS_VM_READ (0x0010)
        handle = k32.OpenProcess(0x0410, False, int(pid))
        if not handle:
            handle = k32.OpenProcess(0x1010, False, int(pid))
        if not handle:
            handle = k32.OpenProcess(0x1000, False, int(pid))
        close_after = True

    if not handle:
        return ProcessMemoryMB(0.0, 0.0, 0.0, 0.0)

    try:
        mb = 1024.0 * 1024.0
        c2 = PROCESS_MEMORY_COUNTERS_EX2()
        c2.cb = ctypes.sizeof(c2)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(c2), c2.cb):
            pws = c2.PrivateWorkingSetSize if c2.PrivateWorkingSetSize > 0 else c2.WorkingSetSize
            return ProcessMemoryMB(
                private_ws_mb=pws / mb,
                working_set_mb=c2.WorkingSetSize / mb,
                private_mb=c2.PrivateUsage / mb,
                peak_working_set_mb=c2.PeakWorkingSetSize / mb,
            )

        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return ProcessMemoryMB(0.0, 0.0, 0.0, 0.0)
        return ProcessMemoryMB(
            private_ws_mb=counters.WorkingSetSize / mb,
            working_set_mb=counters.WorkingSetSize / mb,
            private_mb=counters.PrivateUsage / mb,
            peak_working_set_mb=counters.PeakWorkingSetSize / mb,
        )
    finally:
        if close_after:
            k32.CloseHandle(handle)
