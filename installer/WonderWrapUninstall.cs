using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using Microsoft.Win32;

namespace WonderWrapInstaller
{
    internal static class WonderWrapUninstall
    {
        private const string WidgetPkgName = "Wonder.WrapWidget";
        private const string OldWidgetPkgName = "Woneder.InstantReplayWidget";

        private static int Main()
        {
            Console.Title = "Wonder Wrap - Uninstaller";
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine("==================================================");
            Console.WriteLine("  Wonder Wrap - Uninstaller");
            Console.WriteLine("==================================================");
            Console.ResetColor();

            try
            {
                string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string installDir = Path.Combine(localAppData, "WonderWrap");
                string oldInstallDir = Path.Combine(localAppData, "WonederInstantReplay");
                string daemonExe = Path.Combine(installDir, "daemon", "WonderWrapDaemon.exe");

                Console.WriteLine("[1/4] Stopping Wonder Wrap, Game Bar & FFmpeg...");
                if (File.Exists(daemonExe)) { RunHidden(daemonExe, "--stop", 4000); Thread.Sleep(500); }
                KillProcesses("WonderWrapDaemon");
                KillProcesses("WonederReplayDaemon");
                KillProcesses("WonderWrapWidget");
                KillProcesses("WonederGameBarWidget");
                KillProcesses("GameBar");
                KillProcesses("GameBarFTServer");
                KillProcessesUnderDir("ffmpeg", installDir);
                KillProcessesUnderDir("ffmpeg", oldInstallDir);
                Thread.Sleep(500);

                Console.WriteLine("[2/4] Removing Startup shortcuts...");
                string startupDir = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
                foreach (string lnk in new[] { "Wonder Wrap.lnk", "Woneder Instant Replay.lnk" })
                {
                    string p = Path.Combine(startupDir, lnk);
                    if (File.Exists(p)) { try { File.Delete(p); } catch { } }
                }

                Console.WriteLine("[3/4] Removing Wonder Wrap Xbox Game Bar widget...");
                RunPowerShell(
                    "foreach ($n in @('" + WidgetPkgName + "','" + OldWidgetPkgName + "')) {" +
                    "  $p = Get-AppxPackage -Name $n -ErrorAction SilentlyContinue;" +
                    "  if ($p) {" +
                    "    & CheckNetIsolation.exe LoopbackExempt -d ('-n=' + $p.PackageFamilyName) 2>$null | Out-Null;" +
                    "    Remove-AppxPackage -Package $p.PackageFullName" +
                    "  }" +
                    "}");
                KillProcesses("GameBar");

                Console.WriteLine("[4/4] Cleaning installed files & registry entries...");
                try { Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonderWrap", false); } catch { }
                try { Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonederInstantReplay", false); } catch { }

                foreach (string dir in new[] { installDir, oldInstallDir })
                {
                    if (Directory.Exists(dir))
                    {
                        try { Directory.Delete(dir, true); } catch { }
                    }
                }

                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine("\nWonder Wrap uninstalled cleanly! Your saved clips were kept.");
                Console.ResetColor();
                Console.WriteLine("\nPress any key to close...");
                Console.ReadKey(true);
                return 0;
            }
            catch (Exception ex)
            {
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine("\nUninstall error: " + ex.Message);
                Console.ResetColor();
                Console.WriteLine("\nPress any key to close...");
                Console.ReadKey(true);
                return 1;
            }
        }

        private static void KillProcesses(string name)
        {
            foreach (var proc in Process.GetProcessesByName(name))
            {
                try { proc.Kill(); proc.WaitForExit(3000); } catch { }
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

        private static void RunHidden(string exe, string args, int timeoutMs)
        {
            try
            {
                using (var p = Process.Start(new ProcessStartInfo
                {
                    FileName = exe,
                    Arguments = args,
                    UseShellExecute = false,
                    CreateNoWindow = true
                }))
                {
                    if (p != null) p.WaitForExit(timeoutMs);
                }
            }
            catch { }
        }

        private static void RunPowerShell(string script)
        {
            string encoded = Convert.ToBase64String(System.Text.Encoding.Unicode.GetBytes(script));
            using (var p = Process.Start(new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded,
                UseShellExecute = false,
                CreateNoWindow = true
            }))
            {
                if (p != null) p.WaitForExit(30000);
            }
        }
    }
}
