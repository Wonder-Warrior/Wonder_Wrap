# Wonder Wrap

A drop-in, ultra-low-RAM (**< 150 MB active**, **~18 MB idle**) **replacement for the built-in Windows 11 Xbox Game Bar Capture widget** — delivering Instant Replay, Manual Recording, Screenshots, and live Desktop + Mic audio with zero DLL injection and zero DirectX/Vulkan hooking (anti-cheat safe by design).

## Why Replace Xbox Game Bar Capture?

The default Windows 11 **Capture** widget (`GameDVR` / `Broadcast DVR Server`) is notorious for high memory overhead, rigid settings, broken background recording when alt-tabbing, and limited replay controls. **Wonder Wrap** keeps the exact familiar 4-tile Capture layout inside `Win + G`, while outperforming it everywhere that matters:

| Capability | Default Xbox Game Bar Capture | **Wonder Wrap** |
| :--- | :--- | :--- |
| **Memory Usage (Active)** | Often 400–800+ MB (`bcastdvr.exe` + shell) | **< 150 MB total** (~130 MB active, **~18 MB idle**) |
| **Idle Overhead (Not Gaming)** | Background DVR services linger in memory | **0% GPU, ~18 MB RAM** until a game launches |
| **Alt-Tab / Tabbed-Out Recording** | Pauses, drops frames, or stops when unfocused | **Keeps recording only the game window** via WGC (`gfxcapture`) even when tabbed out |
| **Replay Buffer Length** | Fixed presets buried in Windows Settings | **Custom input (`5s–7200s`) or `0` = full game session** right inside the widget |
| **Video & Audio Quality** | Limited "Standard / High" toggles | **Custom FPS (`15–240` or `0` = match game FPS)**, **2–100 Mbps** NVENC/AMF/QSV, **64–320 kbps** AAC |
| **Save Speed** | Re-muxing/flushing delays & stutters | **Instant lossless `-c copy` MP4** via NTFS hard links (zero recording hitch) |
| **Capture Location** | Hardcoded to `Videos\Captures` unless Registry/Folder moved | **1-click Browse** folder picker inside Settings |
| **False-Positive Apps** | No quick override inside Capture | **1-click "Not a game"** ignore button + smart Win32 UI-framework filtering |
| **Anti-Cheat Safety** | System-level capture | **100% external** (zero DLL injection or graphics API hooking) |

## Features

- **Native Xbox Game Bar Captures Look** (`Win + G`): Appears as **Captures** with the exact compact `320px`-wide monochrome Fluent layout — **Wonder Wrap** branding and Wonder Lime (`#A8FF00`) accents appear only when you open the **Settings** drawer.
- **Game-Only Automatic Instant Replay**: Stays completely idle (~18 MB RAM, 0% GPU) until a game window is detected. Automatically captures **only the game window** via Windows Graphics Capture (`gfxcapture`) at your native screen resolution — even when tabbed out.
- **Ultra-Low Memory Footprint**: Hardware-accelerated D3D11 NVENC / AMF / QSV / MediaFoundation encoding directly into a rolling 2-second MPEG-TS disk buffer (no raw frame RAM buffering).
- **Live Desktop + Microphone Audio**: Pure Win32 WASAPI desktop loopback + auto-converted WASAPI microphone mixing, with a 1-click **Mic** toggle tile.
- **Instant Save (Custom Hotkey)**: Default `Alt + F10`, or click the hotkey box in Settings and press any key combo (`Ctrl`/`Alt`/`Shift` + key, or `F1–F12`). Lossless `-c copy` MP4 remux in milliseconds without interrupting recording.
- **Customizable Numeric Settings Drawer**:
  - **Replay Length**: `5–7200s`, or **`0`** to record everything since the game started
  - **Frame Rate**: `15–240 FPS`, or **`0`** to match the game's native FPS (VFR)
  - **Video & Audio Bitrate**: `2–100 Mbps` video, `64–320 kbps` AAC audio
  - **Capture Folder Picker**, **Game-only** & **Show cursor** toggles, and a 1-click **"Not a game"** ignore button
- **Single-File Standalone Installer**: Bundles the daemon, FFmpeg, `.NET Native` Xbox Game Bar widget, and Windows Installed Apps uninstaller in one `.exe`.

## Download & Install

1. Download **`WonderWrap_Installer.exe`** from the [Releases](../../releases/latest) page.
2. Run `WonderWrap_Installer.exe` (accept the UAC prompt to enable sideloading & register the Xbox Game Bar widget).
3. Press **`Win + G`** → **Widget Menu** → **Captures** *(Wonder Wrap)* → pin it in place of the default Capture widget.
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
