# Wonder Wrap

A lightweight (< 150 MB active, ~18 MB idle) replacement for the Windows 11 Xbox Game Bar **Captures** widget. Supports Instant Replay, manual recording, screenshots, and live desktop + mic audio without DLL injection or graphics API hooking.

## Why Wonder Wrap vs. Default Captures

| | Default Xbox Game Bar Captures | Wonder Wrap |
| :--- | :--- | :--- |
| **Active RAM** | 400-800+ MB (`bcastdvr.exe`) | **< 150 MB** (~130 MB active, **~18 MB idle**) |
| **Idle Overhead** | DVR services stay loaded | **0% GPU, ~18 MB RAM** until a game runs |
| **Alt-Tab Recording** | Pauses or drops frames when unfocused | Keeps recording only the game window via WGC (`gfxcapture`) |
| **Replay Length** | Fixed presets in Windows Settings | Custom input (`5-7200s`) or **`0`** for the entire game session |
| **Frame Rate & Bitrate** | Basic Standard / High toggles | Custom FPS (`15-240` or **`0`** to match game FPS), `2-100 Mbps`, `64-320 kbps` audio |
| **Hotkey** | Fixed system shortcut | Click-and-press custom hotkey in Settings |
| **Save Speed** | Flushing/muxing hitches | Instant lossless `-c copy` MP4 via NTFS hard links |
| **Capture Folder** | Locked to `Videos\Captures` | Folder picker right inside Settings |
| **False Positives** | No quick override | 1-click **Not a game** button + window-class filtering |
| **Updates** | Windows Store | Automatic GitHub Release update check + 1-click install |

## Features

- **Native Captures Look (`Win + G`)**: Appears as **Captures** with the compact `320px` monochrome Game Bar layout. Wonder Wrap branding and lime accents only show when you open Settings.
- **Game-Only Instant Replay**: Stays idle until a game window opens, then records only the game window at native screen resolution even when tabbed out.
- **Hardware Encoding**: Auto-detects `h264_nvenc`, `h264_amf`, `h264_qsv`, `h264_mf`, or `libx264` fallback.
- **Desktop + Mic Audio**: Native WASAPI desktop loopback mixed with your default microphone; the mic tile toggles only your mic on/off.
- **Auto-Updates**: Checks GitHub Releases in the background and lets you update in one click from Settings.
- **GUI Installer**: Single standalone `.exe` bundling the daemon, FFmpeg, `.NET Native` Game Bar widget, and uninstaller.

## Install

1. Download **`WonderWrap_Installer.exe`** from [Releases](../../releases/latest) and run it.
2. Press **`Win + G`** -> **Widget Menu** -> **Captures** -> pin it.
3. Uninstall anytime from **Windows Settings -> Apps -> Installed Apps -> Wonder Wrap**.

## Build from Source

Requirements: Windows 11 (x64), Python 3.12+ with `pyinstaller`, Visual Studio 2022 Build Tools (UWP + SDK `10.0.22621.0`), FFmpeg 9.1+ with `gfxcapture`.

```powershell
# Builds using VERSION file (or pass -Version 1.2.0 to bump automatically)
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

Output: `dist\WonderWrap_Installer.exe`

## License

[MIT](LICENSE)
