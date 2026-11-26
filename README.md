# Wonder Wrap

Ultra-low-RAM (**< 150 MB active**, **~18 MB idle**), 100% external Windows 11 **Instant Replay**, **Manual Recording**, and **Screenshot** tool integrated directly into **Xbox Game Bar**.

Zero DLL injection, zero DirectX/Vulkan hooking — anti-cheat safe by design.

## Features

- **Xbox Game Bar Integration** (`Win + G`): Compact `320px`-wide Fluent dark widget with **Wonder Lime (`#A8FF00`)** accents, matching the native Capture layout.
- **Game-Only Automatic Instant Replay**: Stays completely idle (~18 MB RAM, 0% GPU) until a game window is detected. Automatically captures **only the game window** via Windows Graphics Capture (`gfxcapture`) at your native screen resolution — even when tabbed out.
- **Ultra-Low Memory Footprint**: Hardware-accelerated D3D11 NVENC / AMF / QSV / MediaFoundation encoding directly into a rolling 2-second MPEG-TS disk buffer (no raw frame RAM buffering).
- **Live Desktop + Microphone Audio**: Pure Win32 WASAPI desktop loopback + auto-converted WASAPI microphone mixing, with a 1-click **Mic** toggle tile.
- **Instant Save (`Alt + F10`)**: Lossless `-c copy` MP4 remux in milliseconds without interrupting recording.
- **Customizable Settings Drawer**: Replay length (`5s` – `30 min`), FPS (`30`/`60`), video bitrate (`10–35 Mbps`), audio bitrate (`128–256 kbps`), save hotkey, capture folder picker, game-only & cursor toggles, and a 1-click **"Not a game"** ignore button.
- **Single-File Standalone Installer**: Bundles the daemon, FFmpeg, `.NET Native` Xbox Game Bar widget, and Windows Installed Apps uninstaller in one `.exe`.

## Download & Install

1. Download **`WonderWrap_Installer.exe`** from the [Releases](../../releases/latest) page.
2. Run `WonderWrap_Installer.exe` (accept the UAC prompt to enable sideloading & register the Xbox Game Bar widget).
3. Press **`Win + G`** → **Widget Menu** → **Wonder Wrap** → pin it wherever you like.
4. Uninstall anytime from **Windows Settings → Apps → Installed Apps → Wonder Wrap**.

## Building from Source

### Prerequisites
- Windows 11 (x64)
- Python 3.12+ with `pyinstaller`
- Visual Studio 2022 Build Tools with UWP / Windows 10/11 SDK (`10.0.22621.0`)
- FFmpeg 9.1+ (full build with `gfxcapture`, `ddagrab`, `h264_nvenc`/`amf`/`qsv`)

### Build Single-File Installer
```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```
Output: `dist\WonderWrap_Installer.exe`

## Architecture

- `replay_daemon.py` & `wonder_wrap/`: Standalone Python background daemon (`WonderWrapDaemon.exe`) handling game detection (`game_detector.py`), rolling HLS buffer + manual recording + screenshots (`replay_engine.py`), WASAPI loopback + mic mixing (`wasapi_loopback.py`), global hotkeys (`hotkey.py`), and localhost HTTP IPC (`ipc_server.py`).
- `WonderWrapWidget/`: UWP C# Xbox Game Bar widget compiled with `.NET Native`.
- `installer/`: Self-extracting C# installer (`WonderWrapSetup.cs`) and uninstaller (`WonderWrapUninstall.cs`).

## License

[MIT](LICENSE)
