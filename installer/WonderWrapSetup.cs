using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WonderWrapInstaller
{
    internal sealed class LimeProgressBar : Control
    {
        private int _value;
        public int Value
        {
            get { return _value; }
            set
            {
                _value = Math.Max(0, Math.Min(100, value));
                Invalidate();
            }
        }

        public LimeProgressBar()
        {
            DoubleBuffered = true;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (var bg = new SolidBrush(Color.FromArgb(35, 38, 46)))
                e.Graphics.FillRectangle(bg, ClientRectangle);

            if (_value > 0)
            {
                int w = (int)(Width * (_value / 100.0));
                if (w > 0)
                {
                    using (var fill = new SolidBrush(Color.FromArgb(168, 255, 0)))
                        e.Graphics.FillRectangle(fill, 0, 0, w, Height);
                }
            }
            using (var pen = new Pen(Color.FromArgb(48, 53, 64)))
                e.Graphics.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
        }
    }

    internal sealed class SetupForm : Form
    {
        private readonly Label _statusLabel;
        private readonly Label _detailLabel;
        private readonly LimeProgressBar _progress;
        private readonly Button _actionButton;
        private readonly Button _cancelButton;
        private bool _finished;
        private bool _installing;

        public SetupForm()
        {
            Text = "Wonder Wrap Setup";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = true;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(460, 265);
            BackColor = Color.FromArgb(24, 26, 31);
            ForeColor = Color.FromArgb(240, 242, 245);
            Font = new Font("Segoe UI", 9.5f);
            try { Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch { }

            var accentBar = new Panel
            {
                Dock = DockStyle.Top,
                Height = 3,
                BackColor = Color.FromArgb(168, 255, 0)
            };
            Controls.Add(accentBar);

            var titleLabel = new Label
            {
                Text = "Wonder Wrap",
                Font = new Font("Segoe UI", 17f, FontStyle.Bold),
                ForeColor = Color.FromArgb(245, 247, 250),
                Location = new Point(24, 22),
                AutoSize = true
            };
            Controls.Add(titleLabel);

            var verLabel = new Label
            {
                Text = "v" + WonderWrapSetup.AppVersion,
                Font = new Font("Segoe UI", 9.5f, FontStyle.Bold),
                ForeColor = Color.FromArgb(168, 255, 0),
                Location = new Point(26, 54),
                AutoSize = true
            };
            Controls.Add(verLabel);

            var descLabel = new Label
            {
                Text = "Ultra-low-RAM Captures widget & instant replay daemon for Xbox Game Bar.\nInstalls to %LOCALAPPDATA%\\WonderWrap and runs silently on startup.",
                ForeColor = Color.FromArgb(160, 168, 180),
                Location = new Point(24, 82),
                Size = new Size(412, 40)
            };
            Controls.Add(descLabel);

            _statusLabel = new Label
            {
                Text = "Ready to install",
                Font = new Font("Segoe UI", 9.5f, FontStyle.Bold),
                ForeColor = Color.FromArgb(230, 234, 240),
                Location = new Point(24, 136),
                Size = new Size(412, 20)
            };
            Controls.Add(_statusLabel);

            _progress = new LimeProgressBar
            {
                Location = new Point(24, 162),
                Size = new Size(412, 12),
                Value = 0
            };
            Controls.Add(_progress);

            _detailLabel = new Label
            {
                Text = "Press Install to continue. Your existing settings and clips are preserved.",
                Font = new Font("Segoe UI", 8.5f),
                ForeColor = Color.FromArgb(135, 143, 156),
                Location = new Point(24, 180),
                Size = new Size(412, 22)
            };
            Controls.Add(_detailLabel);

            _actionButton = new Button
            {
                Text = "Install",
                Location = new Point(326, 214),
                Size = new Size(110, 32),
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(168, 255, 0),
                ForeColor = Color.FromArgb(18, 20, 24),
                Font = new Font("Segoe UI", 9.5f, FontStyle.Bold),
                Cursor = Cursors.Hand
            };
            _actionButton.FlatAppearance.BorderSize = 0;
            _actionButton.Click += ActionButton_Click;
            Controls.Add(_actionButton);

            _cancelButton = new Button
            {
                Text = "Cancel",
                Location = new Point(226, 214),
                Size = new Size(90, 32),
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(36, 40, 48),
                ForeColor = Color.FromArgb(215, 220, 228),
                Cursor = Cursors.Hand
            };
            _cancelButton.FlatAppearance.BorderColor = Color.FromArgb(52, 58, 70);
            _cancelButton.Click += (s, e) => Close();
            Controls.Add(_cancelButton);
        }

        private void ReportProgress(int pct, string status, string detail)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action(() => ReportProgress(pct, status, detail)));
                return;
            }
            _progress.Value = pct;
            _statusLabel.Text = status;
            _detailLabel.Text = detail;
        }

        private async void ActionButton_Click(object sender, EventArgs e)
        {
            if (_finished)
            {
                Close();
                return;
            }
            if (_installing) return;

            _installing = true;
            _actionButton.Enabled = false;
            _cancelButton.Enabled = false;

            try
            {
                await Task.Run(() => WonderWrapSetup.ExecuteInstall(ReportProgress));
                _finished = true;
                _progress.Value = 100;
                _statusLabel.ForeColor = Color.FromArgb(168, 255, 0);
                _statusLabel.Text = "Wonder Wrap v" + WonderWrapSetup.AppVersion + " installed!";
                _detailLabel.Text = "Press Win + G -> Widget Menu -> Captures -> Pin it.";
                _actionButton.Text = "Finish";
                _actionButton.Enabled = true;
                _cancelButton.Visible = false;
            }
            catch (Exception ex)
            {
                _installing = false;
                _statusLabel.ForeColor = Color.FromArgb(248, 113, 113);
                _statusLabel.Text = "Installation failed";
                _detailLabel.Text = ex.Message;
                _actionButton.Text = "Retry";
                _actionButton.Enabled = true;
                _cancelButton.Enabled = true;
            }
        }
    }

    internal static class WonderWrapSetup
    {
        internal const string AppVersion = "1.1.1";
        private const string PayloadResourceName = "WonderWrapPayload.zip";
        private const string WidgetPkgName = "Wonder.WrapWidget";
        private const string OldWidgetPkgName = "Woneder.InstantReplayWidget";
        private const string UninstallRegPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonderWrap";
        private const string OldUninstallRegPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WonederInstantReplay";

        [STAThread]
        private static int Main(string[] args)
        {
            bool silent = Array.Exists(args, a =>
                a.Equals("--silent", StringComparison.OrdinalIgnoreCase) ||
                a.Equals("/S", StringComparison.OrdinalIgnoreCase));

            if (silent)
            {
                try
                {
                    ExecuteInstall((pct, st, det) => { });
                    return 0;
                }
                catch
                {
                    return 1;
                }
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm());
            return 0;
        }

        internal static void ExecuteInstall(Action<int, string, string> progress)
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string installDir = Path.Combine(localAppData, "WonderWrap");
            string oldInstallDir = Path.Combine(localAppData, "WonederInstantReplay");
            string dstDaemon = Path.Combine(installDir, "daemon");
            string dstWidget = Path.Combine(installDir, "widget");
            string daemonExe = Path.Combine(dstDaemon, "WonderWrapDaemon.exe");
            string oldDaemonExe = Path.Combine(oldInstallDir, "daemon", "WonederReplayDaemon.exe");
            string uninstallExe = Path.Combine(installDir, "WonderWrapUninstall.exe");

            progress(12, "Stopping running processes...", "Closing existing Wonder Wrap daemon, Game Bar, and widget...");
            if (File.Exists(daemonExe)) RunHidden(daemonExe, "--stop", 4000);
            if (File.Exists(oldDaemonExe)) RunHidden(oldDaemonExe, "--stop", 4000);
            Thread.Sleep(450);

            KillProcesses("WonderWrapDaemon");
            KillProcesses("WonederReplayDaemon");
            KillProcesses("WonderWrapWidget");
            KillProcesses("WonederGameBarWidget");
            KillProcesses("GameBar");
            KillProcesses("GameBarFTServer");
            KillProcessesUnderDir("ffmpeg", installDir);
            KillProcessesUnderDir("ffmpeg", oldInstallDir);

            RunPowerShell(
                "foreach ($n in @('" + OldWidgetPkgName + "','" + WidgetPkgName + "')) {" +
                "  $old = Get-AppxPackage -Name $n -ErrorAction SilentlyContinue;" +
                "  if ($old) { try { Remove-AppxPackage -Package $old.PackageFullName -ErrorAction SilentlyContinue } catch {} }" +
                "}");
            KillProcesses("WonderWrapWidget");
            KillProcesses("WonederGameBarWidget");
            KillProcesses("GameBar");
            Thread.Sleep(700);

            string savedSettingsJson = null;
            string newSettingsPath = Path.Combine(installDir, "settings.json");
            string oldSettingsPath = Path.Combine(oldInstallDir, "settings.json");
            if (File.Exists(newSettingsPath)) savedSettingsJson = File.ReadAllText(newSettingsPath);
            else if (File.Exists(oldSettingsPath)) savedSettingsJson = File.ReadAllText(oldSettingsPath);

            progress(35, "Extracting application files...", "Installing to " + installDir);
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

            progress(58, "Configuring sideloading...", "Ensuring Windows Developer Mode is enabled...");
            using (var key = Registry.LocalMachine.CreateSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"))
            {
                if (key != null)
                {
                    key.SetValue("AllowDevelopmentWithoutDevLicense", 1, RegistryValueKind.DWord);
                }
            }

            progress(74, "Registering Captures widget...", "Installing Xbox Game Bar extension and loopback exemption...");
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

            progress(92, "Finishing installation...", "Creating startup shortcut and starting background daemon...");
            string startupDir = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
            string oldShortcut = Path.Combine(startupDir, "Woneder Instant Replay.lnk");
            if (File.Exists(oldShortcut)) { try { File.Delete(oldShortcut); } catch { } }
            CreateShortcut(Path.Combine(startupDir, "Wonder Wrap.lnk"), daemonExe, dstDaemon, "Wonder Wrap Daemon");

            try { Registry.CurrentUser.DeleteSubKeyTree(OldUninstallRegPath, false); } catch { }
            if (File.Exists(uninstallExe))
            {
                using (var ukey = Registry.CurrentUser.CreateSubKey(UninstallRegPath))
                {
                    if (ukey != null)
                    {
                        ukey.SetValue("DisplayName", "Wonder Wrap");
                        ukey.SetValue("DisplayVersion", AppVersion);
                        ukey.SetValue("Publisher", "Wonder");
                        ukey.SetValue("InstallLocation", installDir);
                        ukey.SetValue("DisplayIcon", daemonExe + ",0");
                        ukey.SetValue("UninstallString", "\"" + uninstallExe + "\"");
                        ukey.SetValue("NoModify", 1, RegistryValueKind.DWord);
                        ukey.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                    }
                }
            }

            Process.Start(new ProcessStartInfo
            {
                FileName = "explorer.exe",
                Arguments = "\"" + daemonExe + "\"",
                UseShellExecute = true
            });
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
                    throw new InvalidOperationException("Embedded payload '" + PayloadResourceName + "' not found.");
                using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    foreach (var entry in archive.Entries)
                    {
                        string destPath = Path.GetFullPath(Path.Combine(destinationDir, entry.FullName));
                        if (!destPath.StartsWith(Path.GetFullPath(destinationDir), StringComparison.OrdinalIgnoreCase))
                            continue;
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

        private static int RunHidden(string exe, string args, int timeoutMs)
        {
            try
            {
                using (var p = Process.Start(new ProcessStartInfo
                {
                    FileName = exe,
                    Arguments = args,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                }))
                {
                    if (p == null) return -1;
                    p.WaitForExit(timeoutMs);
                    return p.HasExited ? p.ExitCode : -1;
                }
            }
            catch { return -1; }
        }

        private static int RunPowerShell(string script)
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
