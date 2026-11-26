#Requires -Version 5.1
<#
.SYNOPSIS
    Builds the single-file standalone Windows installer:
      dist\WonderWrap_Installer.exe

    Embeds:
      - Standalone WonderWrapDaemon.exe (PyInstaller --noconsole --onedir)
      - Bundled ffmpeg.exe
      - Release x64 .NET Native Wonder Wrap Xbox Game Bar Widget (AppXLayout + Dependencies\x64)
      - WonderWrapUninstall.exe
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$StageDir     = Join-Path $RootDir "build\stage"
$PayloadZip   = Join-Path $RootDir "build\WonderWrapPayload.zip"
$PyDist       = Join-Path $RootDir "build\pydist"
$PyWork       = Join-Path $RootDir "build\pyinstaller"
$DistDir      = Join-Path $RootDir "dist"
$InstallerExe = Join-Path $DistDir "WonderWrap_Installer.exe"

$py = Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"
if (-not (Test-Path $py)) {
    $py = (Get-Command python.exe -ErrorAction Stop).Source
}

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    throw "csc.exe not found at $csc"
}

Write-Host "=== [1/5] Generating Wonder Lime icons & preparing staging directory ===" -ForegroundColor Cyan
& $py (Join-Path $RootDir "scripts\generate_icons.py")
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }
$appIcon = Join-Path $RootDir "installer\wonder_wrap.ico"

if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
if (Test-Path $PayloadZip) { Remove-Item -Force $PayloadZip }
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "daemon") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "widget") | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

Write-Host "=== [2/5] Building standalone WonderWrapDaemon.exe ===" -ForegroundColor Cyan
if (Test-Path $PyDist) { Remove-Item -Recurse -Force $PyDist }
if (Test-Path $PyWork) { Remove-Item -Recurse -Force $PyWork }

& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --noconsole `
    --onedir `
    --icon $appIcon `
    --name "WonderWrapDaemon" `
    --distpath $PyDist `
    --workpath $PyWork `
    --specpath $PyWork `
    (Join-Path $RootDir "replay_daemon.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

Copy-Item -Path (Join-Path $PyDist "WonderWrapDaemon\*") -Destination (Join-Path $StageDir "daemon") -Recurse -Force

Write-Host "=== [3/5] Bundling ffmpeg.exe ===" -ForegroundColor Cyan
$ffmpegPath = & $py -c "import sys; sys.path.insert(0, r'$RootDir'); from wonder_wrap.win32_dxgi import find_ffmpeg_binary; print(find_ffmpeg_binary('ffmpeg'))"
if (-not $ffmpegPath -or -not (Test-Path $ffmpegPath)) {
    throw "Could not locate ffmpeg.exe to bundle."
}
Copy-Item -Path $ffmpegPath -Destination (Join-Path $StageDir "daemon\ffmpeg.exe") -Force

Write-Host "=== [4/5] Building Release x64 .NET Native Wonder Wrap Widget ===" -ForegroundColor Cyan
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$msbuild = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" | Select-Object -First 1
$projPath = Join-Path $RootDir "WonderWrapWidget\WonderWrapWidget.csproj"
$pkgOutDir = Join-Path $RootDir "WonderWrapWidget\AppPackages"

& $msbuild $projPath /t:Restore /p:Configuration=Release /p:Platform=x64 /nologo /v:minimal
if ($LASTEXITCODE -ne 0) { throw "MSBuild restore failed." }

& $msbuild $projPath `
    /t:Build `
    /p:Configuration=Release `
    /p:Platform=x64 `
    /p:UseDotNetNativeToolchain=true `
    /p:AppxBundle=Never `
    /p:UapAppxPackageBuildMode=SideloadOnly `
    /p:GenerateAppxPackageOnBuild=true `
    /p:AppxPackageSigningEnabled=false `
    /nologo /v:minimal
if ($LASTEXITCODE -ne 0) { throw "MSBuild .NET Native widget build failed." }

$pkgFile = Get-ChildItem -Path $pkgOutDir -Recurse -Include *.msix,*.appx |
    Where-Object { $_.FullName -notmatch "\\Dependencies\\" -and $_.FullName -notmatch "_Debug" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $pkgFile) { throw "Release .msix package not found under $pkgOutDir" }

$widgetDst = Join-Path $StageDir "widget"
$depSrc = Join-Path $pkgFile.DirectoryName "Dependencies\x64"
if (Test-Path $depSrc) {
    $depDst = Join-Path $widgetDst "Dependencies\x64"
    New-Item -ItemType Directory -Force -Path $depDst | Out-Null
    Copy-Item -Path (Join-Path $depSrc "*") -Destination $depDst -Recurse -Force
}

$makeappx = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter "makeappx.exe" -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\x64\\" } |
    Sort-Object FullName -Descending |
    Select-Object -First 1 -ExpandProperty FullName
$layoutDst = Join-Path $widgetDst "AppXLayout"
New-Item -ItemType Directory -Force -Path $layoutDst | Out-Null
& $makeappx unpack /p $pkgFile.FullName /d $layoutDst /o /l | Out-Null
foreach ($meta in @("AppxBlockMap.xml", "AppxSignature.p7x", "[Content_Types].xml", "AppxMetadata")) {
    $mp = Join-Path $layoutDst $meta
    if (Test-Path $mp) { Remove-Item -Recurse -Force $mp }
}

Write-Host "=== [5/5] Compiling WonderWrapUninstall.exe & single-file WonderWrap_Installer.exe ===" -ForegroundColor Cyan
$uacManifest = Join-Path $RootDir "installer\uac.manifest"
$setupCs     = Join-Path $RootDir "installer\WonderWrapSetup.cs"
$uninstallCs = Join-Path $RootDir "installer\WonderWrapUninstall.cs"
$uninstallExe = Join-Path $StageDir "WonderWrapUninstall.exe"

& $csc /nologo /optimize+ /target:exe /platform:x64 /win32icon:$appIcon /win32manifest:$uacManifest /out:$uninstallExe $uninstallCs
if ($LASTEXITCODE -ne 0) { throw "Failed to compile WonderWrapUninstall.exe" }

Write-Host "      Packing embedded payload..."
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $PayloadZip -CompressionLevel Optimal

Write-Host "      Embedding payload into single-file WonderWrap_Installer.exe..."
& $csc /nologo /optimize+ /target:exe /platform:x64 `
    /win32icon:$appIcon `
    /win32manifest:$uacManifest `
    /r:Microsoft.CSharp.dll `
    /r:System.IO.Compression.dll `
    /r:System.IO.Compression.FileSystem.dll `
    /resource:"$PayloadZip,WonderWrapPayload.zip" `
    /out:$InstallerExe `
    $setupCs
if ($LASTEXITCODE -ne 0) { throw "Failed to compile WonderWrap_Installer.exe" }

# Clean old artifacts from dist\ so dist\ contains ONLY WonderWrap_Installer.exe
Get-ChildItem -Path $DistDir | Where-Object { $_.Name -ne "WonderWrap_Installer.exe" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$mb = [math]::Round((Get-Item $InstallerExe).Length / 1MB, 1)
Write-Host "`nBuilt single-file installer: $InstallerExe ($mb MB)" -ForegroundColor Green
