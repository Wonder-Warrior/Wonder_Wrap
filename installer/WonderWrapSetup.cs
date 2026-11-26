using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using Microsoft.Win32;

namespace WonderWrapInstaller
{
    internal static class WonderWrapSetup
    {
        private const string PayloadResourceName = "WonderWrapPayload.zip";
        private const string WidgetPkgName = "Wonder.WrapWidget";
        private const string OldWidgetPkgName = "Woneder.InstantReplayWidget";
        private const string UninstallRegPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonderWrap";
        private const string OldUninstallRegPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonederInstantReplay";

        private static int Main(string[] args)
        {
            bool silent = Array.Exists(args, a =>
                a.Equals("--silent", StringComparison.OrdinalIgnoreCase) ||
                a.Equals("/S", StringComparison.OrdinalIgnoreCase));

            Console.Title = "Wonder Wrap - Installer";
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine("==================================================");
            Console.WriteLine("  Wonder Wrap - Standalone Installer");
            Console.WriteLine("==================================================");
            Console.ResetColor();

            try
            {
                string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string installDir = Path.Combine(localAppData, "WonderWrap");
                string oldInstallDir = Path.Combine(localAppData, "WonederInstantReplay");
                string dstDaemon = Path.Combine(installDir, "daemon");
                string dstWidget = Path.Combine(installDir, "widget");
                string daemonExe = Path.Combine(dstDaemon, "WonderWrapDaemon.exe");
                string oldDaemonExe = Path.Combine(oldInstallDir, "daemon", "WonederReplayDaemon.exe");
                string uninstallExe = Path.Combine(installDir, "WonderWrapUninstall.exe");

                // 1. Stop any running Wonder Wrap daemon, FFmpeg, Game Bar, and widget processes holding DLL locks
                Console.WriteLine("[1/5] Stopping any running Wonder Wrap / Game Bar / FFmpeg processes...");
                if (File.Exists(daemonExe)) RunHidden(daemonExe, "--stop", 4000);
                if (File.Exists(oldDaemonExe)) RunHidden(oldDaemonExe, "--stop", 4000);
                Thread.Sleep(500);

                KillProcesses("WonderWrapDaemon");
                KillProcesses("WonederReplayDaemon");
                KillProcesses("WonderWrapWidget");
                KillProcesses("WonederGameBarWidget");
                KillProcesses("GameBar");
                KillProcesses("GameBarFTServer");
                KillProcessesUnderDir("ffmpeg", installDir);
                KillProcessesUnderDir("ffmpeg", oldInstallDir);

                // Unregister existing widget packages before deleting AppXLayout so Windows releases DLL locks
                RunPowerShell(
                    "foreach ($n in @('" + OldWidgetPkgName + "','" + WidgetPkgName + "')) {" +
                    "  $old = Get-AppxPackage -Name $n -ErrorAction SilentlyContinue;" +
                    "  if ($old) { try { Remove-AppxPackage -Package $old.PackageFullName -ErrorAction SilentlyContinue } catch {} }" +
                    "}");
                KillProcesses("WonderWrapWidget");
                KillProcesses("WonederGameBarWidget");
                KillProcesses("GameBar");
                Thread.Sleep(800);

                // Preserve existing user settings.json when upgrading
                string savedSettingsJson = null;
                string newSettingsPath = Path.Combine(installDir, "settings.json");
                string oldSettingsPath = Path.Combine(oldInstallDir, "settings.json");
                if (File.Exists(newSettingsPath)) savedSettingsJson = File.ReadAllText(newSettingsPath);
                else if (File.Exists(oldSettingsPath)) savedSettingsJson = File.ReadAllText(oldSettingsPath);

                // 2. Clean old install directories & extract embedded standalone payload
                Console.WriteLine("[2/5] Extracting standalone files to " + installDir + "...");
                SafeDeleteDir(dstDaemon);
                SafeDeleteDir(dstWidget);
                SafeDeleteDir(oldInstallDir);
                Directory.CreateDirectory(installDir);

                ExtractEmbeddedPayload(installDir);

                if (savedSettingsJson != null && !File.Exists(newSettingsPath))
                {
                    File.WriteAllText(newSettingsPath, savedSettingsJson);
                }

                if (!File.Exists(daemonExe))
                {
                    throw new FileNotFoundException("Extracted payload is missing WonderWrapDaemon.exe.", daemonExe);
                }

                // 3. Ensure Windows Developer Mode is enabled for widget sideloading
                Console.WriteLine("[3/5] Ensuring Windows Developer Mode is enabled...");
                using (var key = Registry.LocalMachine.CreateSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"))
                {
                    if (key != null)
                    {
                        key.SetValue("AllowDevelopmentWithoutDevLicense", 1, RegistryValueKind.DWord);
                    }
                }

                // 4. Register Release .NET Native Xbox Game Bar widget + dependencies + loopback exemption
                Console.WriteLine("[4/5] Installing Wonder Wrap Xbox Game Bar widget & loopback exemption...");
                string layoutDir = Path.Combine(dstWidget, "AppXLayout");
                string manifestPath = Path.Combine(layoutDir, "AppxManifest.xml");
                string depDir = Path.Combine(dstWidget, "Dependencies\\x64");

                RunHidden("icacls.exe", string.Format("\"{0}\" /grant \"*S-1-15-2-1:(OI)(CI)(RX)\" /T /Q", layoutDir), 30000);

                string psCmd =
                    "$ErrorActionPreference='Stop';" +
                    "foreach ($n in @('" + OldWidgetPkgName + "','" + WidgetPkgName + "')) {" +
                    "  $old = Get-AppxPackage -Name $n -ErrorAction SilentlyContinue;" +
                    "  if ($old) { Remove-AppxPackage -Package $old.PackageFullName }" +
                    "};" +
                    "if (Test-Path '" + depDir.Replace("'", "''") + "') {" +
                    "  Get-ChildItem -Path '" + depDir.Replace("'", "''") + "' -Recurse -Include *.appx,*.msix | ForEach-Object {" +
                    "    try { Add-AppxPackage -Path $_.FullName -ErrorAction SilentlyContinue } catch {}" +
                    "  }" +
                    "};" +
                    "Add-AppxPackage -Register '" + manifestPath.Replace("'", "''") + "' -ForceApplicationShutdown;" +
                    "$pkg = Get-AppxPackage -Name '" + WidgetPkgName + "' | Select-Object -First 1;" +
                    "if (-not $pkg) { throw 'Widget registration failed.' };" +
                    "& CheckNetIsolation.exe LoopbackExempt -a ('-n=' + $pkg.PackageFamilyName) | Out-Null;";

                int psRc = RunPowerShell(psCmd);
                if (psRc != 0)
                {
                    throw new Exception("Widget registration failed (exit code " + psRc + ").");
                }

                KillProcesses("GameBar");

                // 5. Add Startup shortcut, clean legacy entries, register in Installed Apps, and launch daemon
                Console.WriteLine("[5/5] Registering Startup shortcut & launching WonderWrapDaemon...");
                string startupDir = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
                string oldShortcut = Path.Combine(startupDir, "Woneder Instant Replay.lnk");
                if (File.Exists(oldShortcut))
                {
                    try { File.Delete(oldShortcut); } catch { }
                }
                string shortcutPath = Path.Combine(startupDir, "Wonder Wrap.lnk");
                CreateShortcut(shortcutPath, daemonExe, dstDaemon, "Wonder Wrap Instant Replay Daemon");

                try { Registry.CurrentUser.DeleteSubKeyTree(OldUninstallRegPath, false); } catch { }
                if (File.Exists(uninstallExe))
                {
                    using (var ukey = Registry.CurrentUser.CreateSubKey(UninstallRegPath))
                    {
                        if (ukey != null)
                        {
                            ukey.SetValue("DisplayName", "Wonder Wrap");
                            ukey.SetValue("DisplayVersion", "1.0.0");
                            ukey.SetValue("Publisher", "Wonder");
                            ukey.SetValue("InstallLocation", installDir);
                            ukey.SetValue("DisplayIcon", daemonExe + ",0");
                            ukey.SetValue("UninstallString", "\"" + uninstallExe + "\"");
                            ukey.SetValue("NoModify", 1, RegistryValueKind.DWord);
                            ukey.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                        }
                    }
                }

                // Launch WonderWrapDaemon.exe non-elevated via explorer.exe so it runs at normal user level
                Process.Start(new ProcessStartInfo
                {
                    FileName = "explorer.exe",
                    Arguments = "\"" + daemonExe + "\"",
                    UseShellExecute = true
                });

                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine("\nWonder Wrap installed cleanly!");
                Console.ResetColor();
                Console.WriteLine("  - Running silently in game-only idle mode (~18 MB RAM, 0% GPU).");
                Console.WriteLine("  - Press Win + G -> Widget Menu -> 'Wonder Wrap' -> Pin it.");
                Console.WriteLine("  - Screenshot & Manual Record work anytime; Instant Replay activates while gaming.");
                Console.WriteLine("  - Uninstall anytime from Windows Settings -> Installed Apps.");

                if (!silent)
                {
                    Console.WriteLine("\nPress any key to close...");
                    Console.ReadKey(true);
                }
                return 0;
            }
            catch (Exception ex)
            {
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine("\nInstallation failed: " + ex.Message);
                Console.ResetColor();
                if (!silent)
                {
                    Console.WriteLine("\nPress any key to close...");
                    Console.ReadKey(true);
                }
                return 1;
            }
        }

        private static void SafeDeleteDir(string path)
        {
            if (!Directory.Exists(path)) return;
            for (int attempt = 0; attempt < 8; attempt++)
            {
                try
                {
                    Directory.Delete(path, true);
                    return;
                }
                catch
                {
                    KillProcesses("WonderWrapWidget");
                    KillProcesses("WonederGameBarWidget");
                    KillProcesses("GameBar");
                    Thread.Sleep(500);
                }
            }
        }

        private static void ExtractEmbeddedPayload(string destinationDir)
        {
            var asm = Assembly.GetExecutingAssembly();
            using (Stream stream = asm.GetManifestResourceStream(PayloadResourceName))
            {
                if (stream == null)
                {
                    throw new InvalidOperationException("Embedded payload '" + PayloadResourceName + "' not found.");
                }
                using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    foreach (var entry in archive.Entries)
                    {
                        string destPath = Path.GetFullPath(Path.Combine(destinationDir, entry.FullName));
                        if (!destPath.StartsWith(Path.GetFullPath(destinationDir), StringComparison.OrdinalIgnoreCase))
                        {
                            continue;
                        }
                        if (string.IsNullOrEmpty(entry.Name))
                        {
                            Directory.CreateDirectory(destPath);
                            continue;
                        }
                        Directory.CreateDirectory(Path.GetDirectoryName(destPath));
                        entry.ExtractToFile(destPath, true);
                    }
                }
            }
        }

        private static void KillProcesses(string name)
        {
            foreach (var proc in Process.GetProcessesByName(name))
            {
                try
                {
                    proc.Kill();
                    proc.WaitForExit(3000);
                }
                catch { }
            }
        }

        private static void KillProcessesUnderDir(string name, string dirPrefix)
        {
            foreach (var proc in Process.GetProcessesByName(name))
            {
                try
                {
                    string exePath = proc.MainModule != null ? proc.MainModule.FileName : string.Empty;
                    if (!string.IsNullOrEmpty(exePath) &&
                        exePath.StartsWith(dirPrefix, StringComparison.OrdinalIgnoreCase))
                    {
                        proc.Kill();
                        proc.WaitForExit(3000);
                    }
                }
                catch { }
            }
        }

        private static int RunHidden(string exe, string args, int timeoutMs)
        {
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = exe,
                    Arguments = args,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (var p = Process.Start(psi))
                {
                    if (p == null) return -1;
                    p.WaitForExit(timeoutMs);
                    return p.HasExited ? p.ExitCode : -1;
                }
            }
            catch
            {
                return -1;
            }
        }

        private static int RunPowerShell(string script)
        {
            string encoded = Convert.ToBase64String(System.Text.Encoding.Unicode.GetBytes(script));
            var psi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using (var p = Process.Start(psi))
            {
                if (p == null) return -1;
                p.WaitForExit(60000);
                return p.ExitCode;
            }
        }

        private static void CreateShortcut(string shortcutPath, string targetPath, string workingDir, string description)
        {
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            dynamic shell = Activator.CreateInstance(shellType);
            dynamic shortcut = shell.CreateShortcut(shortcutPath);
            shortcut.TargetPath = targetPath;
            shortcut.WorkingDirectory = workingDir;
            shortcut.Description = description;
            shortcut.Save();
        }
    }
}
