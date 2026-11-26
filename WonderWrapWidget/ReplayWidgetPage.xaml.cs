using System;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Gaming.XboxGameBar;
using Windows.Data.Json;
using Windows.Foundation;
using Windows.System;
using Windows.UI;
using Windows.UI.Core;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Input;
using Windows.UI.Xaml.Media;
using Windows.UI.Xaml.Navigation;
using Windows.Web.Http;
using Windows.Web.Http.Filters;

namespace WonderWrapWidget
{
    public sealed partial class ReplayWidgetPage : Page
    {
        private const string BaseUrl = "http://127.0.0.1:47821";

        // Wonder Lime (#A8FF00) + dark Fluent palette
        private static readonly SolidColorBrush WonderLimeBrush =
            new SolidColorBrush(Color.FromArgb(255, 168, 255, 0));   // #A8FF00
        private static readonly SolidColorBrush WonderLimeBorderBrush =
            new SolidColorBrush(Color.FromArgb(145, 168, 255, 0));   // #91A8FF00
        private static readonly SolidColorBrush DefaultTileBorderBrush =
            new SolidColorBrush(Color.FromArgb(255, 48, 53, 64));    // #303540
        private static readonly SolidColorBrush RecordingRedBrush =
            new SolidColorBrush(Color.FromArgb(255, 239, 68, 68));   // #EF4444
        private static readonly SolidColorBrush OfflineBrush =
            new SolidColorBrush(Color.FromArgb(255, 122, 126, 135)); // #7A7E87
        private static readonly SolidColorBrush SubtleGrayBrush =
            new SolidColorBrush(Color.FromArgb(255, 156, 163, 175)); // #9CA3AF
        private static readonly SolidColorBrush ErrorBrush =
            new SolidColorBrush(Color.FromArgb(255, 248, 113, 113)); // #F87171
        private static readonly SolidColorBrush IconActiveBrush =
            new SolidColorBrush(Color.FromArgb(255, 232, 234, 237)); // #E8EAED

        private const string GlyphMicOn = "\uE720";
        private const string GlyphMicOff = "\uF781";

        private XboxGameBarWidget _widget;
        private readonly HttpClient _http;
        private readonly DispatcherTimer _pollTimer;
        private readonly DispatcherTimer _feedbackTimer;
        private bool _pollInFlight;
        private bool _actionInFlight;
        private bool _showingFeedback;
        private bool _isOnline;
        private bool _settingsOpen;
        private bool _loadingSettings;
        private string _mode = "idle";
        private string _activeGame = string.Empty;
        private string _outputDir = string.Empty;
        private string _hotkey = "Win+Alt+G";
        private string _recordHotkey = "Win+Alt+R";
        private string _screenshotHotkey = "Win+Alt+PrintScreen";
        private string _micHotkey = "Win+Alt+M";
        private double _recordingSec;
        private double _lastRamMb;
        private int _bufferSec = 30;

        public ReplayWidgetPage()
        {
            InitializeComponent();

            var filter = new HttpBaseProtocolFilter();
            filter.CacheControl.ReadBehavior = HttpCacheReadBehavior.MostRecent;
            filter.CacheControl.WriteBehavior = HttpCacheWriteBehavior.NoCache;
            _http = new HttpClient(filter);

            _pollTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _pollTimer.Tick += PollTimer_Tick;

            _feedbackTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            _feedbackTimer.Tick += FeedbackTimer_Tick;

            Loaded += ReplayWidgetPage_Loaded;
            Unloaded += ReplayWidgetPage_Unloaded;
        }

        protected override void OnNavigatedTo(NavigationEventArgs e)
        {
            base.OnNavigatedTo(e);
            _widget = e.Parameter as XboxGameBarWidget;
        }

        private async void ReplayWidgetPage_Loaded(object sender, RoutedEventArgs e)
        {
            if (_widget != null)
            {
                try
                {
                    await _widget.TryResizeWindowAsync(new Size(320, 156));
                }
                catch { }
            }
            await RefreshStatusAsync();
            _pollTimer.Start();
        }

        private void ReplayWidgetPage_Unloaded(object sender, RoutedEventArgs e)
        {
            _pollTimer.Stop();
            _feedbackTimer.Stop();
            _http.Dispose();
        }

        private async void PollTimer_Tick(object sender, object e)
        {
            await RefreshStatusAsync();
        }

        private void FeedbackTimer_Tick(object sender, object e)
        {
            _feedbackTimer.Stop();
            _showingFeedback = false;
            UpdateMetaLabel();
        }

        private void ShowFeedback(string message, bool isError = false)
        {
            _feedbackTimer.Stop();
            _showingFeedback = true;
            var okBrush = _settingsOpen ? WonderLimeBrush : IconActiveBrush;
            StatusDot.Fill = isError ? ErrorBrush : okBrush;
            StatusMetaText.Foreground = isError ? ErrorBrush : okBrush;
            StatusMetaText.Text = message;
            _feedbackTimer.Start();
        }

        private void UpdateMetaLabel()
        {
            if (_showingFeedback)
            {
                return;
            }

            if (!_isOnline)
            {
                StatusDot.Fill = OfflineBrush;
                StatusMetaText.Foreground = SubtleGrayBrush;
                StatusMetaText.Text = "Offline";
                return;
            }

            if (_mode == "recording")
            {
                StatusDot.Fill = RecordingRedBrush;
                StatusMetaText.Foreground = IconActiveBrush;
                var ts = TimeSpan.FromSeconds(Math.Max(0, (int)_recordingSec));
                StatusMetaText.Text = string.Format("{0:mm\\:ss}", ts);
            }
            else if (_mode == "replay")
            {
                StatusDot.Fill = _settingsOpen ? WonderLimeBrush : IconActiveBrush;
                StatusMetaText.Foreground = _settingsOpen ? WonderLimeBrush : IconActiveBrush;
                StatusMetaText.Text = string.Format("{0:F0} MB", _lastRamMb);
            }
            else
            {
                StatusDot.Fill = OfflineBrush;
                StatusMetaText.Foreground = SubtleGrayBrush;
                StatusMetaText.Text = string.Format("Idle · {0:F0} MB", _lastRamMb);
            }
        }

        private async Task RefreshStatusAsync()
        {
            if (_pollInFlight) return;

            _pollInFlight = true;
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(1500)))
                {
                    var response = await _http.GetAsync(new Uri(BaseUrl + "/status")).AsTask(cts.Token);
                    if (!response.IsSuccessStatusCode)
                    {
                        SetOfflineUi();
                        return;
                    }

                    string body = await response.Content.ReadAsStringAsync().AsTask(cts.Token);
                    if (JsonObject.TryParse(body, out JsonObject obj) &&
                        obj.ContainsKey("running") &&
                        obj["running"].GetBoolean())
                    {
                        double ramMb = obj.ContainsKey("ram_mb") ? obj["ram_mb"].GetNumber() : 0.0;
                        int bufSec = obj.ContainsKey("buffer_sec") ? (int)obj["buffer_sec"].GetNumber() : 30;
                        bool muted = obj.ContainsKey("muted") && obj["muted"].GetBoolean();
                        string mode = obj.ContainsKey("mode") ? obj["mode"].GetString() : "idle";
                        string game = obj.ContainsKey("game") ? obj["game"].GetString() : string.Empty;
                        string outDir = obj.ContainsKey("output_dir") ? obj["output_dir"].GetString() : string.Empty;
                        double recSec = obj.ContainsKey("recording_sec") ? obj["recording_sec"].GetNumber() : 0.0;
                        string version = obj.ContainsKey("version") ? obj["version"].GetString() : string.Empty;
                        bool updateAvail = obj.ContainsKey("update_available") && obj["update_available"].GetBoolean();
                        string latestVer = obj.ContainsKey("latest_version") ? obj["latest_version"].GetString() : string.Empty;
                        bool updateDl = obj.ContainsKey("update_downloading") && obj["update_downloading"].GetBoolean();
                        SetOnlineUi(ramMb, bufSec, muted, mode, game, outDir, recSec, version, updateAvail, latestVer, updateDl);
                    }
                    else
                    {
                        SetOfflineUi();
                    }
                }
            }
            catch
            {
                SetOfflineUi();
            }
            finally
            {
                _pollInFlight = false;
            }
        }

        private void SetOnlineUi(double ramMb, int bufferSec, bool muted, string mode, string game, string outDir, double recSec, string version, bool updateAvail, string latestVer, bool updateDl)
        {
            _isOnline = true;
            _lastRamMb = ramMb;
            _bufferSec = bufferSec;
            _mode = mode;
            _activeGame = game;
            _outputDir = outDir;
            _recordingSec = recSec;

            if (updateAvail && !string.IsNullOrEmpty(latestVer))
            {
                UpdateRow.Visibility = Visibility.Visible;
                UpdateText.Text = updateDl ? ("Downloading v" + latestVer + "...") : ("Update available: v" + latestVer);
                UpdateButton.IsEnabled = !updateDl;
            }
            else
            {
                UpdateRow.Visibility = Visibility.Collapsed;
            }
            if (!string.IsNullOrEmpty(version) && !EncoderBadgeText.Text.Contains("v" + version))
            {
                string enc = EncoderBadgeText.Text.Split('·')[0].Trim();
                EncoderBadgeText.Text = enc + " · v" + version;
            }

            bool isRecording = mode == "recording";
            bool isReplay = mode == "replay";

            SaveIcon.Foreground = isReplay ? (_settingsOpen ? WonderLimeBrush : IconActiveBrush) : SubtleGrayBrush;
            SaveButton.BorderBrush = (isReplay && _settingsOpen) ? WonderLimeBorderBrush : DefaultTileBorderBrush;

            RecordDot.Visibility = isRecording ? Visibility.Collapsed : Visibility.Visible;
            RecordDot.Fill = IconActiveBrush;
            StopSquare.Visibility = isRecording ? Visibility.Visible : Visibility.Collapsed;
            RecordButton.BorderBrush = isRecording ? RecordingRedBrush : DefaultTileBorderBrush;

            string gameSuffix = string.IsNullOrEmpty(game) ? string.Empty : " (" + game + ")";
            ToolTipService.SetToolTip(ScreenshotButton, "Take screenshot" + gameSuffix);
            string replayLenLabel = bufferSec <= 0 ? "full session" : string.Format("last {0}s", bufferSec);
            ToolTipService.SetToolTip(SaveButton, isReplay
                ? string.Format("Save {0}{1} ({2})", replayLenLabel, gameSuffix, _hotkey)
                : (isRecording ? "Unavailable while manually recording" : "Instant Replay activates while gaming"));
            ToolTipService.SetToolTip(RecordButton, isRecording
                ? "Stop recording" + gameSuffix
                : "Start recording" + gameSuffix);

            if (!string.IsNullOrEmpty(outDir))
            {
                ToolTipService.SetToolTip(OpenCapturesButton, "Open " + outDir);
                FolderPathText.Text = outDir;
            }

            if (!string.IsNullOrEmpty(game))
            {
                ActiveGameRow.Visibility = Visibility.Visible;
                ActiveGameText.Text = "Active: " + game;
            }
            else
            {
                ActiveGameRow.Visibility = Visibility.Collapsed;
                ActiveGameText.Text = string.Empty;
            }

            ApplyMuteVisual(muted);
            UpdateMetaLabel();

            if (!_actionInFlight)
            {
                ScreenshotButton.IsEnabled = true;
                SaveButton.IsEnabled = isReplay;
                RecordButton.IsEnabled = true;
                MuteButton.IsEnabled = true;
                OpenCapturesButton.IsEnabled = true;
                SettingsButton.IsEnabled = true;
            }
        }

        private void SetOfflineUi()
        {
            _isOnline = false;
            _mode = "idle";
            _activeGame = string.Empty;
            SaveIcon.Foreground = SubtleGrayBrush;
            SaveButton.BorderBrush = DefaultTileBorderBrush;
            RecordDot.Visibility = Visibility.Visible;
            RecordDot.Fill = OfflineBrush;
            StopSquare.Visibility = Visibility.Collapsed;
            RecordButton.BorderBrush = DefaultTileBorderBrush;
            MuteButton.BorderBrush = DefaultTileBorderBrush;
            ToolTipService.SetToolTip(RecordButton, "Daemon offline — start WonderWrapDaemon");
            SetControlsEnabled(false);
            UpdateMetaLabel();
        }

        private void ApplyMuteVisual(bool muted)
        {
            MuteIcon.Glyph = muted ? GlyphMicOff : GlyphMicOn;
            MuteIcon.Foreground = muted ? ErrorBrush : IconActiveBrush;
            MuteButton.BorderBrush = muted ? ErrorBrush : DefaultTileBorderBrush;
            ToolTipService.SetToolTip(MuteButton, muted ? "Mic muted (click to turn mic on)" : "Mic on (click to mute mic)");
        }

        private void SetControlsEnabled(bool enabled)
        {
            ScreenshotButton.IsEnabled = enabled;
            SaveButton.IsEnabled = enabled;
            RecordButton.IsEnabled = enabled;
            MuteButton.IsEnabled = enabled;
            OpenCapturesButton.IsEnabled = enabled;
            SettingsButton.IsEnabled = enabled;
        }

        private static void SelectComboByTag(ComboBox combo, string tag)
        {
            for (int i = 0; i < combo.Items.Count; i++)
            {
                if (combo.Items[i] is ComboBoxItem item &&
                    string.Equals(item.Tag as string, tag, StringComparison.OrdinalIgnoreCase))
                {
                    combo.SelectedIndex = i;
                    return;
                }
            }
            if (combo.Items.Count > 0 && combo.SelectedIndex < 0)
            {
                combo.SelectedIndex = 0;
            }
        }

        private static int ParseDigits(string text, int fallback)
        {
            if (string.IsNullOrWhiteSpace(text)) return fallback;
            string digits = string.Empty;
            foreach (char c in text)
            {
                if (char.IsDigit(c)) digits += c;
            }
            return int.TryParse(digits, out int val) ? val : fallback;
        }

        private async Task LoadSettingsAsync()
        {
            _loadingSettings = true;
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
                {
                    var resp = await _http.GetAsync(new Uri(BaseUrl + "/settings")).AsTask(cts.Token);
                    if (!resp.IsSuccessStatusCode) return;
                    string body = await resp.Content.ReadAsStringAsync().AsTask(cts.Token);
                    if (!JsonObject.TryParse(body, out JsonObject s)) return;

                    if (s.ContainsKey("buffer_sec")) BufferInput.Text = ((int)s["buffer_sec"].GetNumber()).ToString();
                    if (s.ContainsKey("fps")) FpsInput.Text = ((int)s["fps"].GetNumber()).ToString();
                    if (s.ContainsKey("video_bitrate")) VideoBitrateInput.Text = ParseDigits(s["video_bitrate"].GetString(), 18).ToString();
                    if (s.ContainsKey("audio_bitrate")) AudioBitrateInput.Text = ParseDigits(s["audio_bitrate"].GetString(), 192).ToString();
                    if (s.ContainsKey("hotkey"))
                    {
                        _hotkey = s["hotkey"].GetString();
                        HotkeyInput.Text = _hotkey;
                    }
                    if (s.ContainsKey("record_hotkey"))
                    {
                        _recordHotkey = s["record_hotkey"].GetString();
                        RecordHotkeyInput.Text = _recordHotkey;
                    }
                    if (s.ContainsKey("screenshot_hotkey"))
                    {
                        _screenshotHotkey = s["screenshot_hotkey"].GetString();
                        ScreenshotHotkeyInput.Text = _screenshotHotkey;
                    }
                    if (s.ContainsKey("mic_hotkey"))
                    {
                        _micHotkey = s["mic_hotkey"].GetString();
                        MicHotkeyInput.Text = _micHotkey;
                    }
                    if (s.ContainsKey("game_only")) GameOnlyCheck.IsChecked = s["game_only"].GetBoolean();
                    if (s.ContainsKey("capture_cursor")) CursorCheck.IsChecked = s["capture_cursor"].GetBoolean();
                    if (s.ContainsKey("output_dir")) FolderPathText.Text = s["output_dir"].GetString();
                    if (s.ContainsKey("encoder")) EncoderBadgeText.Text = s["encoder"].GetString().ToUpperInvariant();
                }
            }
            catch { }
            finally
            {
                _loadingSettings = false;
            }
        }

        private async void SettingsButton_Click(object sender, RoutedEventArgs e)
        {
            _settingsOpen = !_settingsOpen;
            SettingsPanel.Visibility = _settingsOpen ? Visibility.Visible : Visibility.Collapsed;
            SettingsIcon.Foreground = _settingsOpen ? WonderLimeBrush : IconActiveBrush;
            SettingsButton.BorderBrush = _settingsOpen ? WonderLimeBorderBrush : DefaultTileBorderBrush;
            bool isReplay = _mode == "replay";
            SaveIcon.Foreground = isReplay ? (_settingsOpen ? WonderLimeBrush : IconActiveBrush) : SubtleGrayBrush;
            SaveButton.BorderBrush = (isReplay && _settingsOpen) ? WonderLimeBorderBrush : DefaultTileBorderBrush;
            UpdateMetaLabel();

            if (_settingsOpen)
            {
                await LoadSettingsAsync();
            }

            if (_widget != null)
            {
                try
                {
                    await _widget.TryResizeWindowAsync(new Size(320, _settingsOpen ? 426 : 156));
                }
                catch { }
            }
        }

        private static string MapVirtualKeyToToken(VirtualKey key)
        {
            if (key >= VirtualKey.F1 && key <= VirtualKey.F12)
                return "F" + ((int)key - (int)VirtualKey.F1 + 1);
            if (key >= VirtualKey.A && key <= VirtualKey.Z)
                return key.ToString();
            if (key >= VirtualKey.Number0 && key <= VirtualKey.Number9)
                return ((int)key - (int)VirtualKey.Number0).ToString();
            if (key == VirtualKey.Insert) return "Insert";
            if (key == VirtualKey.Home) return "Home";
            if (key == VirtualKey.End) return "End";
            if (key == VirtualKey.PageUp) return "PageUp";
            if (key == VirtualKey.PageDown) return "PageDown";
            if (key == VirtualKey.Snapshot) return "PrintScreen";
            if (key == VirtualKey.Pause) return "Pause";
            return null;
        }

        private static bool IsModFreeKey(VirtualKey key)
        {
            return (key >= VirtualKey.F1 && key <= VirtualKey.F12) ||
                   key == VirtualKey.Insert ||
                   key == VirtualKey.Snapshot ||
                   key == VirtualKey.Pause;
        }

        private async void HotkeyInput_PreviewKeyDown(object sender, KeyRoutedEventArgs e)
        {
            e.Handled = true;
            var key = e.Key;
            if (key == VirtualKey.Control || key == VirtualKey.LeftControl || key == VirtualKey.RightControl ||
                key == VirtualKey.Menu || key == VirtualKey.LeftMenu || key == VirtualKey.RightMenu ||
                key == VirtualKey.Shift || key == VirtualKey.LeftShift || key == VirtualKey.RightShift ||
                key == VirtualKey.LeftWindows || key == VirtualKey.RightWindows)
            {
                return;
            }

            string token = MapVirtualKeyToToken(key);
            if (string.IsNullOrEmpty(token))
            {
                ShowFeedback("Invalid key", isError: true);
                return;
            }

            var win = Window.Current.CoreWindow;
            bool ctrl = (win.GetKeyState(VirtualKey.Control) & CoreVirtualKeyStates.Down) == CoreVirtualKeyStates.Down;
            bool winMod = (win.GetKeyState(VirtualKey.LeftWindows) & CoreVirtualKeyStates.Down) == CoreVirtualKeyStates.Down ||
                          (win.GetKeyState(VirtualKey.RightWindows) & CoreVirtualKeyStates.Down) == CoreVirtualKeyStates.Down;
            bool alt = (win.GetKeyState(VirtualKey.Menu) & CoreVirtualKeyStates.Down) == CoreVirtualKeyStates.Down;
            bool shift = (win.GetKeyState(VirtualKey.Shift) & CoreVirtualKeyStates.Down) == CoreVirtualKeyStates.Down;

            if (!ctrl && !winMod && !alt && !shift && !IsModFreeKey(key))
            {
                ShowFeedback("Add Ctrl/Win/Alt/Shift", isError: true);
                return;
            }

            string combo = string.Empty;
            if (ctrl) combo += "Ctrl+";
            if (winMod) combo += "Win+";
            if (alt) combo += "Alt+";
            if (shift) combo += "Shift+";
            combo += token;

            var box = sender as TextBox;
            if (box == RecordHotkeyInput)
            {
                _recordHotkey = combo;
                RecordHotkeyInput.Text = combo;
            }
            else if (box == ScreenshotHotkeyInput)
            {
                _screenshotHotkey = combo;
                ScreenshotHotkeyInput.Text = combo;
            }
            else if (box == MicHotkeyInput)
            {
                _micHotkey = combo;
                MicHotkeyInput.Text = combo;
            }
            else
            {
                _hotkey = combo;
                HotkeyInput.Text = combo;
            }
            await ApplySettingsFromUiAsync();
        }

        private async void NumericInput_LostFocus(object sender, RoutedEventArgs e)
        {
            await ApplySettingsFromUiAsync();
        }

        private async void NumericInput_KeyDown(object sender, KeyRoutedEventArgs e)
        {
            if (e.Key == VirtualKey.Enter)
            {
                e.Handled = true;
                await ApplySettingsFromUiAsync();
            }
        }

        private async void SettingsControl_Changed(object sender, RoutedEventArgs e)
        {
            await ApplySettingsFromUiAsync();
        }

        private async Task ApplySettingsFromUiAsync()
        {
            if (_loadingSettings || !_isOnline) return;

            int rawBuf = ParseDigits(BufferInput.Text, _bufferSec);
            int clampedBuf = rawBuf <= 0 ? 0 : Math.Max(5, Math.Min(7200, rawBuf));

            int rawFps = ParseDigits(FpsInput.Text, 60);
            int clampedFps = rawFps <= 0 ? 0 : Math.Max(15, Math.Min(240, rawFps));

            int rawVbr = ParseDigits(VideoBitrateInput.Text, 18);
            int clampedVbr = Math.Max(2, Math.Min(100, rawVbr));

            int rawAbr = ParseDigits(AudioBitrateInput.Text, 192);
            int clampedAbr = Math.Max(64, Math.Min(320, rawAbr));

            _loadingSettings = true;
            try
            {
                BufferInput.Text = clampedBuf.ToString();
                FpsInput.Text = clampedFps.ToString();
                VideoBitrateInput.Text = clampedVbr.ToString();
                AudioBitrateInput.Text = clampedAbr.ToString();
            }
            finally
            {
                _loadingSettings = false;
            }

            var payload = new JsonObject();
            payload["buffer_sec"] = JsonValue.CreateNumberValue(clampedBuf);
            payload["fps"] = JsonValue.CreateNumberValue(clampedFps);
            payload["video_bitrate"] = JsonValue.CreateStringValue(clampedVbr + "M");
            payload["audio_bitrate"] = JsonValue.CreateStringValue(clampedAbr + "k");
            if (!string.IsNullOrWhiteSpace(_hotkey))
                payload["hotkey"] = JsonValue.CreateStringValue(_hotkey);
            if (!string.IsNullOrWhiteSpace(_recordHotkey))
                payload["record_hotkey"] = JsonValue.CreateStringValue(_recordHotkey);
            if (!string.IsNullOrWhiteSpace(_screenshotHotkey))
                payload["screenshot_hotkey"] = JsonValue.CreateStringValue(_screenshotHotkey);
            if (!string.IsNullOrWhiteSpace(_micHotkey))
                payload["mic_hotkey"] = JsonValue.CreateStringValue(_micHotkey);
            payload["game_only"] = JsonValue.CreateBooleanValue(GameOnlyCheck.IsChecked == true);
            payload["capture_cursor"] = JsonValue.CreateBooleanValue(CursorCheck.IsChecked == true);

            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
                {
                    var content = new HttpStringContent(payload.Stringify(), Windows.Storage.Streams.UnicodeEncoding.Utf8, "application/json");
                    var resp = await _http.PostAsync(new Uri(BaseUrl + "/settings"), content).AsTask(cts.Token);
                    if (resp.IsSuccessStatusCode)
                    {
                        ShowFeedback("Applied!");
                    }
                }
            }
            catch
            {
                ShowFeedback("Save error", isError: true);
            }
        }

        private async void ScreenshotButton_Click(object sender, RoutedEventArgs e)
        {
            if (_actionInFlight) return;
            _actionInFlight = true;
            SetControlsEnabled(false);
            ShowFeedback("Capturing...");
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(12)))
                {
                    var response = await _http.PostAsync(new Uri(BaseUrl + "/screenshot"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                    string body = await response.Content.ReadAsStringAsync().AsTask(cts.Token);
                    if (response.IsSuccessStatusCode &&
                        JsonObject.TryParse(body, out JsonObject obj) &&
                        obj.ContainsKey("ok") &&
                        obj["ok"].GetBoolean())
                    {
                        ShowFeedback("Shot saved!");
                    }
                    else
                    {
                        ShowFeedback("Shot failed", isError: true);
                    }
                }
            }
            catch
            {
                ShowFeedback("Offline", isError: true);
                SetOfflineUi();
            }
            finally
            {
                _actionInFlight = false;
                await RefreshStatusAsync();
            }
        }

        private async void SaveButton_Click(object sender, RoutedEventArgs e)
        {
            if (_actionInFlight) return;
            _actionInFlight = true;
            SetControlsEnabled(false);
            ShowFeedback("Saving...");
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(90)))
                {
                    var response = await _http.PostAsync(new Uri(BaseUrl + "/save"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                    string body = await response.Content.ReadAsStringAsync().AsTask(cts.Token);
                    if (response.IsSuccessStatusCode &&
                        JsonObject.TryParse(body, out JsonObject obj) &&
                        obj.ContainsKey("ok") &&
                        obj["ok"].GetBoolean())
                    {
                        ShowFeedback("Saved!");
                    }
                    else
                    {
                        ShowFeedback("Save failed", isError: true);
                    }
                }
            }
            catch
            {
                ShowFeedback("Offline", isError: true);
                SetOfflineUi();
            }
            finally
            {
                _actionInFlight = false;
                await RefreshStatusAsync();
            }
        }

        private async void RecordButton_Click(object sender, RoutedEventArgs e)
        {
            if (_actionInFlight) return;
            _actionInFlight = true;
            SetControlsEnabled(false);
            ShowFeedback(_mode == "recording" ? "Stopping..." : "Starting...");
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30)))
                {
                    var response = await _http.PostAsync(new Uri(BaseUrl + "/record/toggle"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                    string body = await response.Content.ReadAsStringAsync().AsTask(cts.Token);
                    if (response.IsSuccessStatusCode &&
                        JsonObject.TryParse(body, out JsonObject obj) &&
                        obj.ContainsKey("ok") &&
                        obj["ok"].GetBoolean())
                    {
                        string mode = obj.ContainsKey("mode") ? obj["mode"].GetString() : "idle";
                        if (mode == "recording")
                        {
                            _showingFeedback = false;
                            _mode = "recording";
                            _recordingSec = 0;
                        }
                        else
                        {
                            ShowFeedback("Saved!");
                        }
                    }
                    else
                    {
                        ShowFeedback("Record error", isError: true);
                    }
                }
            }
            catch
            {
                ShowFeedback("Offline", isError: true);
                SetOfflineUi();
            }
            finally
            {
                _actionInFlight = false;
                await RefreshStatusAsync();
            }
        }

        private async void MuteButton_Click(object sender, RoutedEventArgs e)
        {
            if (_actionInFlight) return;
            _actionInFlight = true;
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
                {
                    var response = await _http.PostAsync(new Uri(BaseUrl + "/mute"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                    string body = await response.Content.ReadAsStringAsync().AsTask(cts.Token);
                    if (response.IsSuccessStatusCode &&
                        JsonObject.TryParse(body, out JsonObject obj) &&
                        obj.ContainsKey("muted"))
                    {
                        bool muted = obj["muted"].GetBoolean();
                        ApplyMuteVisual(muted);
                        ShowFeedback(muted ? "Mic muted" : "Mic on");
                    }
                }
            }
            catch
            {
                SetOfflineUi();
            }
            finally
            {
                _actionInFlight = false;
            }
        }

        private async void OpenCapturesButton_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
                {
                    await _http.PostAsync(new Uri(BaseUrl + "/open-folder"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                }
            }
            catch
            {
                SetOfflineUi();
            }
        }

        private async void ChangeFolderButton_Click(object sender, RoutedEventArgs e)
        {
            if (_actionInFlight) return;
            _actionInFlight = true;
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(120)))
                {
                    var response = await _http.PostAsync(new Uri(BaseUrl + "/choose-folder"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                    string body = await response.Content.ReadAsStringAsync().AsTask(cts.Token);
                    if (response.IsSuccessStatusCode &&
                        JsonObject.TryParse(body, out JsonObject obj) &&
                        obj.ContainsKey("ok") &&
                        obj["ok"].GetBoolean())
                    {
                        if (obj.ContainsKey("path")) FolderPathText.Text = obj["path"].GetString();
                        ShowFeedback("Folder set!");
                    }
                }
            }
            catch
            {
                SetOfflineUi();
            }
            finally
            {
                _actionInFlight = false;
                await RefreshStatusAsync();
            }
        }

        private async void UpdateButton_Click(object sender, RoutedEventArgs e)
        {
            UpdateButton.IsEnabled = false;
            UpdateText.Text = "Downloading update...";
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10)))
                {
                    var resp = await _http.PostAsync(new Uri(BaseUrl + "/update/install"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                    if (resp.IsSuccessStatusCode)
                    {
                        ShowFeedback("Updating...");
                    }
                    else
                    {
                        ShowFeedback("Update failed", isError: true);
                        UpdateButton.IsEnabled = true;
                    }
                }
            }
            catch
            {
                ShowFeedback("Update error", isError: true);
                UpdateButton.IsEnabled = true;
            }
        }

        private async void IgnoreGameButton_Click(object sender, RoutedEventArgs e)
        {
            if (_actionInFlight) return;
            _actionInFlight = true;
            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
                {
                    var response = await _http.PostAsync(new Uri(BaseUrl + "/ignore-active"), new HttpStringContent(string.Empty)).AsTask(cts.Token);
                    if (response.IsSuccessStatusCode)
                    {
                        ShowFeedback("Ignored app");
                    }
                }
            }
            catch
            {
                SetOfflineUi();
            }
            finally
            {
                _actionInFlight = false;
                await RefreshStatusAsync();
            }
        }
    }
}
