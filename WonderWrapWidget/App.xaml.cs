using System;
using Microsoft.Gaming.XboxGameBar;
using Windows.ApplicationModel;
using Windows.ApplicationModel.Activation;
using Windows.UI.Core;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Navigation;

namespace WonderWrapWidget
{
    sealed partial class App : Application
    {
        private XboxGameBarWidget _gameBarWidget;

        public App()
        {
            InitializeComponent();
            Suspending += OnSuspending;
        }

        protected override void OnActivated(IActivatedEventArgs args)
        {
            XboxGameBarWidgetActivatedEventArgs widgetArgs = null;
            if (args.Kind == ActivationKind.Protocol)
            {
                var protocolArgs = args as IProtocolActivatedEventArgs;
                if (protocolArgs != null &&
                    protocolArgs.Uri.Scheme.Equals("ms-gamebarwidget", StringComparison.OrdinalIgnoreCase))
                {
                    widgetArgs = args as XboxGameBarWidgetActivatedEventArgs;
                }
            }

            if (widgetArgs != null)
            {
                if (widgetArgs.IsLaunchActivation)
                {
                    var rootFrame = new Frame();
                    rootFrame.NavigationFailed += OnNavigationFailed;
                    Window.Current.Content = rootFrame;

                    _gameBarWidget = new XboxGameBarWidget(
                        widgetArgs,
                        Window.Current.CoreWindow,
                        rootFrame);

                    rootFrame.Navigate(typeof(ReplayWidgetPage), _gameBarWidget);
                    Window.Current.Closed += WidgetWindow_Closed;
                    Window.Current.Activate();
                }
                return;
            }

            EnsureStandaloneWindow();
        }

        protected override void OnLaunched(LaunchActivatedEventArgs e)
        {
            EnsureStandaloneWindow();
        }

        private void EnsureStandaloneWindow()
        {
            var rootFrame = Window.Current.Content as Frame;
            if (rootFrame == null)
            {
                rootFrame = new Frame();
                rootFrame.NavigationFailed += OnNavigationFailed;
                Window.Current.Content = rootFrame;
            }

            if (rootFrame.Content == null)
            {
                rootFrame.Navigate(typeof(ReplayWidgetPage));
            }

            Window.Current.Activate();
        }

        private void WidgetWindow_Closed(object sender, CoreWindowEventArgs e)
        {
            _gameBarWidget = null;
            Window.Current.Closed -= WidgetWindow_Closed;
        }

        private void OnNavigationFailed(object sender, NavigationFailedEventArgs e)
        {
            throw new Exception("Failed to load Page " + e.SourcePageType.FullName);
        }

        private void OnSuspending(object sender, SuspendingEventArgs e)
        {
            var deferral = e.SuspendingOperation.GetDeferral();
            _gameBarWidget = null;
            deferral.Complete();
        }
    }
}
