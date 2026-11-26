using System;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Gaming.XboxGameBar;
using Windows.Data.Json;
using Windows.Foundation;
using Windows.UI;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Media;
using Windows.UI.Xaml.Navigation;
using Windows.Web.Http;
using Windows.Web.Http.Filters;

namespace WonderWrapWidget
{
    public sealed partial class ReplayWidgetPage : Page
    {
        private const string BaseUrl = "http://127.0.0.1:47821";

        // Signature Woneder Lime (#A8FF00) + polished dark palette
        private static readonly SolidColorBrush WonederLimeBrush =
            new SolidColorBrush(Color.FromArgb(255, 168, 255, 0));   // #A8FF00
        private static readonly SolidColorBrush WonederLimeBorderBrush =
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
        private string _hotkey = "Alt+F10";
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
            StatusDot.Fill = isError ? ErrorBrush : WonederLimeBrush;
            StatusMetaText.Foreground = isError ? ErrorBrush : WonederLimeBrush;
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
                StatusDot.Fill = WonederLimeBrush;
                StatusMetaText.Foreground = WonederLimeBrush;
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
                        SetOnlineUi(ramMb, bufSec, muted, mode, game, outDir, recSec);
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

        private void SetOnlineUi(double ramMb, int bufferSec, bool muted, string mode, string game, string outDir, double recSec)
        {
            _isOnline = true;
            _lastRamMb = ramMb;
            _bufferSec = bufferSec;
            _mode = mode;
            _activeGame = game;
            _outputDir = outDir;
            _recordingSec = recSec;

            bool isRecording = mode == "recording";
            bool isReplay = mode == "replay";

            SaveIcon.Foreground = isReplay ? WonederLimeBrush : SubtleGrayBrush;
            SaveButton.BorderBrush = isReplay ? WonederLimeBorderBrush : DefaultTileBorderBrush;

            RecordDot.Visibility = isRecording ? Visibility.Collapsed : Visibility.Visible;
            RecordDot.Fill = IconActiveBrush;
            StopSquare.Visibility = isRecording ? Visibility.Visible : Visibility.Collapsed;
            RecordButton.BorderBrush = isRecording ? RecordingRedBrush : DefaultTileBorderBrush;

            string gameSuffix = string.IsNullOrEmpty(game) ? string.Empty : " (" + game + ")";
            ToolTipService.SetToolTip(ScreenshotButton, "Take screenshot" + gameSuffix);
            ToolTipService.SetToolTip(SaveButton, isReplay
                ? string.Format("Save last {0}s{1} ({2})", bufferSec, gameSuffix, _hotkey)
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

                    if (s.ContainsKey("buffer_sec")) SelectComboByTag(BufferCombo, ((int)s["buffer_sec"].GetNumber()).ToString());
                    if (s.ContainsKey("fps")) SelectComboByTag(FpsCombo, ((int)s["fps"].GetNumber()).ToString());
                    if (s.ContainsKey("video_bitrate")) SelectComboByTag(VideoBitrateCombo, s["video_bitrate"].GetString());
                    if (s.ContainsKey("audio_bitrate")) SelectComboByTag(AudioBitrateCombo, s["audio_bitrate"].GetString());
                    if (s.ContainsKey("hotkey"))
                    {
                        _hotkey = s["hotkey"].GetString();
                        SelectComboByTag(HotkeyCombo, _hotkey);
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
            SettingsIcon.Foreground = _settingsOpen ? WonederLimeBrush : IconActiveBrush;
            SettingsButton.BorderBrush = _settingsOpen ? WonederLimeBorderBrush : DefaultTileBorderBrush;

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

        private async void SettingsControl_Changed(object sender, RoutedEventArgs e)
        {
            if (_loadingSettings || !_isOnline) return;

            var payload = new JsonObject();
            if (BufferCombo.SelectedItem is ComboBoxItem bItem && int.TryParse(bItem.Tag as string, out int bSec))
                payload["buffer_sec"] = JsonValue.CreateNumberValue(bSec);
            if (FpsCombo.SelectedItem is ComboBoxItem fItem && int.TryParse(fItem.Tag as string, out int fpsVal))
                payload["fps"] = JsonValue.CreateNumberValue(fpsVal);
            if (VideoBitrateCombo.SelectedItem is ComboBoxItem vItem && vItem.Tag is string vb)
                payload["video_bitrate"] = JsonValue.CreateStringValue(vb);
            if (AudioBitrateCombo.SelectedItem is ComboBoxItem aItem && aItem.Tag is string ab)
                payload["audio_bitrate"] = JsonValue.CreateStringValue(ab);
            if (HotkeyCombo.SelectedItem is ComboBoxItem hItem && hItem.Tag is string hk)
            {
                _hotkey = hk;
                payload["hotkey"] = JsonValue.CreateStringValue(hk);
            }
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
