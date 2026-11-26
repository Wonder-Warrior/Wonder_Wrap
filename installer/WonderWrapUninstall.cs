using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WonderWrapInstaller
{
    internal sealed class UninstallForm : Form
    {
        private readonly Label _statusLabel;
        private readonly Button _uninstallBtn;
        private readonly Button _cancelBtn;
        private bool _done;

        public UninstallForm()
        {
            Text = "Uninstall Wonder Wrap";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(420, 195);
            BackColor = Color.FromArgb(24, 26, 31);
            ForeColor = Color.FromArgb(240, 242, 245);
            Font = new Font("Segoe UI", 9.5f);
            try { Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch { }

            var accent = new Panel { Dock = DockStyle.Top, Height = 3, BackColor = Color.FromArgb(239, 68, 68) };
            Controls.Add(accent);

            Controls.Add(new Label
            {
                Text = "Uninstall Wonder Wrap",
                Font = new Font("Segoe UI", 14f, FontStyle.Bold),
                Location = new Point(22, 22),
                AutoSize = true
            });

            _statusLabel = new Label
            {
                Text = "Remove the Wonder Wrap daemon and Xbox Game Bar widget?\nYour saved clips and screenshots will not be deleted.",
                ForeColor = Color.FromArgb(165, 173, 185),
                Location = new Point(22, 62),
                Size = new Size(376, 54)
            };
            Controls.Add(_statusLabel);

            _uninstallBtn = new Button
            {
                Text = "Uninstall",
                Location = new Point(288, 142),
                Size = new Size(108, 32),
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(239, 68, 68),
                ForeColor = Color.White,
                Font = new Font("Segoe UI", 9.5f, FontStyle.Bold),
                Cursor = Cursors.Hand
            };
            _uninstallBtn.FlatAppearance.BorderSize = 0;
            _uninstallBtn.Click += UninstallBtn_Click;
            Controls.Add(_uninstallBtn);

            _cancelBtn = new Button
            {
                Text = "Cancel",
                Location = new Point(190, 142),
                Size = new Size(88, 32),
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(36, 40, 48),
                ForeColor = Color.FromArgb(215, 220, 228),
                Cursor = Cursors.Hand
            };
            _cancelBtn.FlatAppearance.BorderColor = Color.FromArgb(52, 58, 70);
            _cancelBtn.Click += (s, e) => Close();
            Controls.Add(_cancelBtn);
        }

        private async void UninstallBtn_Click(object sender, EventArgs e)
        {
            if (_done) { Close(); return; }
            _uninstallBtn.Enabled = false;
            _cancelBtn.Enabled = false;
            _statusLabel.Text = "Removing Wonder Wrap daemon, widget, and startup entries...";

            try
            {
                await Task.Run(() => WonderWrapUninstall.RunUninstall());
                _done = true;
                _statusLabel.ForeColor = Color.FromArgb(168, 255, 0);
                _statusLabel.Text = "Wonder Wrap has been uninstalled.\nYour saved clips were kept.";
                _uninstallBtn.BackColor = Color.FromArgb(168, 255, 0);
                _uninstallBtn.ForeColor = Color.FromArgb(18, 20, 24);
                _uninstallBtn.Text = "Close";
                _uninstallBtn.Enabled = true;
                _cancelBtn.Visible = false;
            }
            catch (Exception ex)
            {
                _statusLabel.ForeColor = Color.FromArgb(248, 113, 113);
                _statusLabel.Text = "Uninstall error: " + ex.Message;
                _uninstallBtn.Enabled = true;
                _cancelBtn.Enabled = true;
            }
        }
    }

    internal static class WonderWrapUninstall
    {
        private const string WidgetPkgName = "Wonder.WrapWidget";
        private const string OldWidgetPkgName = "Woneder.InstantReplayWidget";

        [STAThread]
        private static int Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new UninstallForm());
            return 0;
        }

        internal static void RunUninstall()
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string installDir = Path.Combine(localAppData, "WonderWrap");
            string oldInstallDir = Path.Combine(localAppData, "WonederInstantReplay");
            string daemonExe = Path.Combine(installDir, "daemon", "WonderWrapDaemon.exe");

            if (File.Exists(daemonExe)) { RunHidden(daemonExe, "--stop", 4000); Thread.Sleep(450); }
            KillProcesses("WonderWrapDaemon");
            KillProcesses("WonederReplayDaemon");
            KillProcesses("WonderWrapWidget");
            KillProcesses("WonederGameBarWidget");
            KillProcesses("GameBar");
            KillProcesses("GameBarFTServer");
            KillProcessesUnderDir("ffmpeg", installDir);
            KillProcessesUnderDir("ffmpeg", oldInstallDir);
            Thread.Sleep(450);

            string startupDir = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
            foreach (string lnk in new[] { "Wonder Wrap.lnk", "Woneder Instant Replay.lnk" })
            {
                string p = Path.Combine(startupDir, lnk);
                if (File.Exists(p)) { try { File.Delete(p); } catch { } }
            }

            RunPowerShell(
                "foreach ($n in @('" + WidgetPkgName + "','" + OldWidgetPkgName + "')) {" +
                "  $p = Get-AppxPackage -Name $n -ErrorAction SilentlyContinue;" +
                "  if ($p) {" +
                "    & CheckNetIsolation.exe LoopbackExempt -d ('-n=' + $p.PackageFamilyName) 2>$null | Out-Null;" +
                "    Remove-AppxPackage -Package $p.PackageFullName" +
                "  }" +
                "}");
            KillProcesses("GameBar");

            try { Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonderWrap", false); } catch { }
            try { Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonederInstantReplay", false); } catch { }

            foreach (string dir in new[] { installDir, oldInstallDir })
            {
                if (Directory.Exists(dir)) { try { Directory.Delete(dir, true); } catch { } }
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
