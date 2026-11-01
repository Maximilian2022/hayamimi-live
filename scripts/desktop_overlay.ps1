<#
.SYNOPSIS
    早耳 (hayamimi) - デスクトップ透過オーバーレイ字幕ウィンドウ
.DESCRIPTION
    画面最前面に字幕（リアルタイム音声認識・複数言語翻訳）を表示します。
    ドラッグ移動、自由リサイズ、クリックスルー（マウス透過）、背景透過切替、設定永続化、タスクトレイ対応。
#>

[CmdletBinding()]
param(
    [string]$ServerUrl = "http://127.0.0.1:8833",
    [switch]$Demo
)

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Drawing, System.Windows.Forms

# Win32 API Helper (Smart App Control 完全対応: Add-Type による動的 C# コンパイルを排除)
if (-not ([System.Management.Automation.PSTypeName]'OverlayWin32').Type) {
    try {
        $dynAssembly = [AppDomain]::CurrentDomain.DefineDynamicAssembly(
            [System.Reflection.AssemblyName]::new("OverlayWin32Dyn"),
            [System.Reflection.Emit.AssemblyBuilderAccess]::Run
        )
        $dynModule = $dynAssembly.DefineDynamicModule("OverlayWin32Module")
        $dynType = $dynModule.DefineType("OverlayWin32", "Public, BeforeFieldInit")

        $mSetFg = $dynType.DefinePInvokeMethod("SetForegroundWindow", "user32.dll", "Public, Static",
            [System.Reflection.CallingConventions]::Standard, [bool], @([IntPtr]),
            [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Auto)
        $mSetFg.SetImplementationFlags("PreserveSig")

        $mRegHotKey = $dynType.DefinePInvokeMethod("RegisterHotKey", "user32.dll", "Public, Static",
            [System.Reflection.CallingConventions]::Standard, [bool], @([IntPtr], [int], [uint32], [uint32]),
            [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Auto)
        $mRegHotKey.SetImplementationFlags("PreserveSig")

        $mUnregHotKey = $dynType.DefinePInvokeMethod("UnregisterHotKey", "user32.dll", "Public, Static",
            [System.Reflection.CallingConventions]::Standard, [bool], @([IntPtr], [int]),
            [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Auto)
        $mUnregHotKey.SetImplementationFlags("PreserveSig")

        if ([IntPtr]::Size -eq 8) {
            $mGetLong = $dynType.DefinePInvokeMethod("GetWindowLongPtr", "user32.dll", "Public, Static",
                [System.Reflection.CallingConventions]::Standard, [IntPtr], @([IntPtr], [int]),
                [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Auto)
            $mGetLong.SetImplementationFlags("PreserveSig")

            $mSetLong = $dynType.DefinePInvokeMethod("SetWindowLongPtr", "user32.dll", "Public, Static",
                [System.Reflection.CallingConventions]::Standard, [IntPtr], @([IntPtr], [int], [IntPtr]),
                [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Auto)
            $mSetLong.SetImplementationFlags("PreserveSig")
        } else {
            $mGetLong = $dynType.DefinePInvokeMethod("GetWindowLong", "user32.dll", "Public, Static",
                [System.Reflection.CallingConventions]::Standard, [int], @([IntPtr], [int]),
                [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Auto)
            $mGetLong.SetImplementationFlags("PreserveSig")

            $mSetLong = $dynType.DefinePInvokeMethod("SetWindowLong", "user32.dll", "Public, Static",
                [System.Reflection.CallingConventions]::Standard, [int], @([IntPtr], [int], [int]),
                [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Auto)
            $mSetLong.SetImplementationFlags("PreserveSig")
        }

        [void]$dynType.CreateType()
    } catch {
        try {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class OverlayWin32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);
    [DllImport("user32.dll")] public static extern bool UnregisterHotKey(IntPtr hWnd, int id);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtr")] public static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int nIndex);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtr")] public static extern IntPtr SetWindowLongPtr(IntPtr hWnd, int nIndex, IntPtr dwNewLong);
}
"@ -ErrorAction SilentlyContinue
        } catch {}
    }
}

function Set-ClickThroughHelper([IntPtr]$hWnd, [bool]$enable) {
    if ($hWnd -eq [IntPtr]::Zero) { return }
    try {
        $GWL_EXSTYLE = -20
        $WS_EX_TRANSPARENT = 0x00000020
        if ([IntPtr]::Size -eq 8) {
            $cur = [OverlayWin32]::GetWindowLongPtr($hWnd, $GWL_EXSTYLE).ToInt64()
            $newVal = if ($enable) { $cur -bor $WS_EX_TRANSPARENT } else { $cur -band (-bnot $WS_EX_TRANSPARENT) }
            [OverlayWin32]::SetWindowLongPtr($hWnd, $GWL_EXSTYLE, [IntPtr]$newVal) | Out-Null
        } else {
            $cur = [OverlayWin32]::GetWindowLong($hWnd, $GWL_EXSTYLE)
            $newVal = if ($enable) { $cur -bor $WS_EX_TRANSPARENT } else { $cur -band (-bnot $WS_EX_TRANSPARENT) }
            [OverlayWin32]::SetWindowLong($hWnd, $GWL_EXSTYLE, [int]$newVal) | Out-Null
        }
    } catch {}
}

# ログファイルパス & ロギング関数 (logs/launcher.log へ集約)
$logsDir = Join-Path $PSScriptRoot "..\logs"
if (-not (Test-Path $logsDir)) {
    try { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null } catch {}
}
$script:LauncherLogFile = Join-Path $logsDir "launcher.log"

function Write-OverlayLog {
    param([string]$Message, [string]$Level = "INFO")
    $now = Get-Date
    $timestamp = $now.ToString("yyyy-MM-dd HH:mm:ss")
    $logLine = "[$timestamp] [$Level] [デスクトップ字幕] $Message"
    try {
        [System.IO.File]::AppendAllText($script:LauncherLogFile, "$logLine`r`n", [System.Text.Encoding]::UTF8)
    } catch {}
}

Write-OverlayLog "起動処理を開始しました (PID: $PID, ServerUrl: $ServerUrl, Demo: $Demo)" "INFO"

# 設定ファイルパス
$configDir = Join-Path $PSScriptRoot "..\config"
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}
$configFile = Join-Path $configDir "desktop_overlay.json"

# 設定の読み込み
$savedConfig = @{
    Left = $null
    Top = $null
    Width = 1100
    Height = 280
    FontSize = 30
    TransparentBg = $false
}
if (Test-Path $configFile) {
    try {
        $json = Get-Content $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($json.Width) { $savedConfig.Width = [double]$json.Width }
        if ($json.Height) { $savedConfig.Height = [double]$json.Height }
        if ($null -ne $json.Left) { $savedConfig.Left = [double]$json.Left }
        if ($null -ne $json.Top) { $savedConfig.Top = [double]$json.Top }
        if ($json.FontSize) { $savedConfig.FontSize = [double]$json.FontSize }
        if ($null -ne $json.TransparentBg) { $savedConfig.TransparentBg = [bool]$json.TransparentBg }
    } catch {}
}

# XAML UI
[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Hayamimi Live デスクトップ字幕オーバーレイ"
        WindowStyle="None"
        AllowsTransparency="True"
        Background="Transparent"
        Topmost="True"
        ShowInTaskbar="True"
        Focusable="True"
        Width="$($savedConfig.Width)" Height="$($savedConfig.Height)"
        MinWidth="450" MinHeight="140">
    <Window.Resources>
        <Style x:Key="ControlBtn" TargetType="Button">
            <Setter Property="Background" Value="#E01E293B"/>
            <Setter Property="Foreground" Value="#F8FAFC"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Padding" Value="9,4"/>
            <Setter Property="Margin" Value="3,0"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="BorderBrush" Value="#475569"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}"
                                BorderThickness="{TemplateBinding BorderThickness}"
                                CornerRadius="6">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
            <Style.Triggers>
                <Trigger Property="IsMouseOver" Value="True">
                    <Setter Property="Background" Value="#F0334155"/>
                    <Setter Property="BorderBrush" Value="#38BDF8"/>
                </Trigger>
            </Style.Triggers>
        </Style>
    </Window.Resources>

    <!-- メインカード（フロスト半透明パネル） -->
    <Border Name="CardBorder"
            Background="#E60F172A"
            CornerRadius="14"
            BorderBrush="#38BDF8" BorderThickness="2"
            Margin="8">
        <Border.Effect>
            <DropShadowEffect Color="#000000" BlurRadius="22" ShadowDepth="4" Opacity="0.85"/>
        </Border.Effect>

        <Grid Name="MainGrid">
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
            </Grid.RowDefinitions>

            <!-- 上部コントロールバー -->
            <Border Name="ControlBar" Grid.Row="0"
                    Background="#901E293B" CornerRadius="12,12,0,0"
                    Padding="12,7" HorizontalAlignment="Stretch">
                <DockPanel LastChildFill="False">
                    <StackPanel Orientation="Horizontal" DockPanel.Dock="Left" VerticalAlignment="Center">
                        <TextBlock Text="💬 早耳 字幕" Foreground="#38BDF8" FontWeight="Bold" FontSize="12.5" VerticalAlignment="Center" Margin="4,0,10,0"/>
                        <TextBlock Text="⠿ ドラッグ移動" Foreground="#94A3B8" FontSize="11" VerticalAlignment="Center" Margin="0,0,10,0" Cursor="SizeAll" Name="DragHandle"/>
                    </StackPanel>

                    <StackPanel Orientation="Horizontal" DockPanel.Dock="Right" VerticalAlignment="Center">
                        <Button Name="BtnToggleBg" Style="{StaticResource ControlBtn}" Content="🎨 背景: 半透明"/>
                        <Button Name="BtnClickThrough" Style="{StaticResource ControlBtn}" Content="📌 マウス透過: OFF" ToolTip="字幕へのマウスクリックを下のアプリに通します"/>
                        <Button Name="BtnFontSmaller" Style="{StaticResource ControlBtn}" Content="A-"/>
                        <Button Name="BtnFontBigger" Style="{StaticResource ControlBtn}" Content="A+"/>
                        <Button Name="BtnHideMenu" Style="{StaticResource ControlBtn}" Content="🔼 隠す" ToolTip="メニューバーを隠します (右クリックや上部ホバー、Escキーで再表示可能)"/>
                        <Button Name="BtnWebSettings" Style="{StaticResource ControlBtn}" Content="⚙ 設定"/>
                        <Button Name="BtnClose" Style="{StaticResource ControlBtn}" Content="✕ 閉じる"/>
                    </StackPanel>
                </DockPanel>
            </Border>

            <!-- メニュー非表示時の再表示用ボタン (常に右上に重ねて表示) -->
            <Button Name="BtnShowMenu" Grid.RowSpan="2" HorizontalAlignment="Right" VerticalAlignment="Top"
                    Margin="0,6,10,0" Padding="12,5" Content="▼ メニューを表示"
                    Visibility="Collapsed" Style="{StaticResource ControlBtn}"
                    Background="#F01E293B" BorderBrush="#38BDF8" BorderThickness="1.5"
                    Opacity="0.95" ToolTip="メニューバーを再表示します (クリック、右クリック、上部ホバー、Escキー、Hキーで操作可能)"/>

            <!-- マウス透過中（クリックスルー中）の案内バッジ -->
            <Border Name="ClickThroughNotice" Grid.RowSpan="2" HorizontalAlignment="Center" VerticalAlignment="Top"
                    Background="#D9059669" CornerRadius="0,0,8,8" Padding="14,4" Margin="0,0,0,0"
                    Visibility="Collapsed" IsHitTestVisible="False">
                <TextBlock Text="📌 マウス透過中 (タスクトレイの早耳アイコンをダブルクリック、または Ctrl+Shift+H で解除)"
                           Foreground="#FFFFFF" FontSize="11" FontWeight="Bold"/>
            </Border>

            <!-- 字幕表示エリア -->
            <Grid Grid.Row="1" Margin="20,8,20,14" VerticalAlignment="Center">
                <StackPanel Name="SubtitleContainer" VerticalAlignment="Center" HorizontalAlignment="Stretch">
                    <!-- 確定字幕 -->
                    <TextBlock Name="FinalText" Text=""
                               TextAlignment="Center" TextWrapping="Wrap"
                               Foreground="#FFFFFF" FontSize="$($savedConfig.FontSize)" FontWeight="Bold"
                               FontFamily="LINE Seed JP, IBM Plex Sans JP, Source Han Sans JP, 源ノ角ゴシック JP, Noto Sans JP, Zen Kaku Gothic New, Meiryo, Yu Gothic UI, sans-serif"
                               LineHeight="38">
                        <TextBlock.Effect>
                            <DropShadowEffect Color="#000000" BlurRadius="8" ShadowDepth="0" Opacity="1.0"/>
                        </TextBlock.Effect>
                    </TextBlock>

                    <!-- 発話中（仮）字幕 -->
                    <TextBlock Name="PartialText" Text=""
                               TextAlignment="Center" TextWrapping="Wrap"
                               Foreground="#E2E8F0" FontSize="22" FontStyle="Italic"
                               FontFamily="LINE Seed JP, IBM Plex Sans JP, Source Han Sans JP, 源ノ角ゴシック JP, Noto Sans JP, Zen Kaku Gothic New, Meiryo, Yu Gothic UI, sans-serif"
                               Opacity="0.85" Margin="0,4,0,0">
                        <TextBlock.Effect>
                            <DropShadowEffect Color="#000000" BlurRadius="6" ShadowDepth="0" Opacity="0.95"/>
                        </TextBlock.Effect>
                    </TextBlock>

                    <!-- 翻訳字幕エリア（複数言語） -->
                    <TextBlock Name="TransText" Text=""
                               TextAlignment="Center" TextWrapping="Wrap"
                               Foreground="#38BDF8" FontSize="20" FontWeight="SemiBold"
                               FontFamily="LINE Seed JP, IBM Plex Sans JP, Source Han Sans JP, 源ノ角ゴシック JP, Noto Sans JP, Zen Kaku Gothic New, Meiryo, Yu Gothic UI, sans-serif"
                               LineHeight="28" Margin="0,6,0,0">
                        <TextBlock.Effect>
                            <DropShadowEffect Color="#000000" BlurRadius="6" ShadowDepth="0" Opacity="1.0"/>
                        </TextBlock.Effect>
                    </TextBlock>
                </StackPanel>

                <!-- 接続待機ガイド -->
                <TextBlock Name="ConnectingNotice" Text="早耳サーバーに接続中... (アプリを起動してください)"
                           Foreground="#94A3B8" FontSize="13" FontStyle="Italic"
                           HorizontalAlignment="Center" VerticalAlignment="Center"
                           Visibility="Collapsed"/>

                <!-- 右下リサイズグリップ (ドラッグでサイズ変更) -->
                <Canvas HorizontalAlignment="Right" VerticalAlignment="Bottom" Width="20" Height="20" Margin="0,0,-12,-8">
                    <Path Data="M 14,0 L 20,0 L 20,20 L 0,20 L 0,14 L 14,14 Z M 8,8 L 12,8 L 12,12 L 8,12 Z"
                          Fill="#94A3B8" Cursor="SizeNWSE" Name="ResizeGrip"
                          ToolTip="ドラッグでサイズ変更 (右クリックでメニュー表示)"/>
                </Canvas>
            </Grid>
        </Grid>
    </Border>
</Window>
"@

$reader = [System.Xml.XmlNodeReader]::new($xaml)
$window = [System.Windows.Markup.XamlReader]::Load($reader)

# コントロール取得
$mainGrid = $window.FindName("MainGrid")
$cardBorder = $window.FindName("CardBorder")
$controlBar = $window.FindName("ControlBar")
$dragHandle = $window.FindName("DragHandle")
$btnToggleBg = $window.FindName("BtnToggleBg")
$btnClickThrough = $window.FindName("BtnClickThrough")
$btnFontSmaller = $window.FindName("BtnFontSmaller")
$btnFontBigger = $window.FindName("BtnFontBigger")
$btnHideMenu = $window.FindName("BtnHideMenu")
$btnShowMenu = $window.FindName("BtnShowMenu")
$clickThroughNotice = $window.FindName("ClickThroughNotice")
$btnWebSettings = $window.FindName("BtnWebSettings")
$btnClose = $window.FindName("BtnClose")
$resizeGrip = $window.FindName("ResizeGrip")
$connectingNotice = $window.FindName("ConnectingNotice")
$subtitleContainer = $window.FindName("SubtitleContainer")

$finalText = $window.FindName("FinalText")
$partialText = $window.FindName("PartialText")
$transText = $window.FindName("TransText")

# 位置復元または中央配置
$primaryScreen = [System.Windows.Forms.Screen]::PrimaryScreen
if ($null -ne $savedConfig.Left -and $null -ne $savedConfig.Top) {
    $window.Left = $savedConfig.Left
    $window.Top = $savedConfig.Top
} else {
    $window.Left = $primaryScreen.WorkingArea.X + ($primaryScreen.WorkingArea.Width - $window.Width) / 2
    $window.Top = $primaryScreen.WorkingArea.Y + ($primaryScreen.WorkingArea.Height - $window.Height) / 2
}

# ドラッグ移動 & クリック時フォーカス獲得
$dragAction = {
    param($eventSender, $e)
    try { $window.Focus() | Out-Null } catch {}
    if ($e.LeftButton -eq [System.Windows.Input.MouseButtonState]::Pressed) {
        $window.DragMove()
    }
}
$mainGrid.Add_MouseLeftButtonDown($dragAction)
$dragHandle.Add_MouseLeftButtonDown($dragAction)
$cardBorder.Add_MouseLeftButtonDown($dragAction)

# 右下リサイズグリップのドラッグ処理 & クリック救済
$script:isResizing = $false
$script:resizeStartPoint = [System.Windows.Point]::new(0, 0)
$script:resizeStartSize = [System.Windows.Size]::new(0, 0)

$resizeGrip.Add_MouseLeftButtonDown({
    param($eventSender, $e)
    try { $window.Focus() | Out-Null } catch {}
    # メニューが隠れている時にクリックされた場合は再表示
    if ($script:isMenuHidden) {
        &$setMenuVisibility $false
        Write-OverlayLog "右下の三角形グリップのクリックを検知し、メニューバーを再表示しました。" "INFO"
    }
    $script:isResizing = $true
    $script:resizeStartPoint = [System.Windows.Forms.Cursor]::Position
    $script:resizeStartSize = [System.Windows.Size]::new($window.Width, $window.Height)
    $resizeGrip.CaptureMouse() | Out-Null
    $e.Handled = $true
})

$resizeGrip.Add_MouseRightButtonDown({
    param($eventSender, $e)
    &$setMenuVisibility $false
    Write-OverlayLog "右下グリップの右クリックを検知し、メニューバーを再表示しました。" "INFO"
    $e.Handled = $true
})

$resizeGrip.Add_MouseMove({
    param($eventSender, $e)
    if ($script:isResizing) {
        $curPos = [System.Windows.Forms.Cursor]::Position
        $deltaX = $curPos.X - $script:resizeStartPoint.X
        $deltaY = $curPos.Y - $script:resizeStartPoint.Y
        $newW = [Math]::Max($window.MinWidth, $script:resizeStartSize.Width + $deltaX)
        $newH = [Math]::Max($window.MinHeight, $script:resizeStartSize.Height + $deltaY)
        $window.Width = $newW
        $window.Height = $newH
    }
})

$resizeGrip.Add_MouseLeftButtonUp({
    param($eventSender, $e)
    if ($script:isResizing) {
        $script:isResizing = $false
        $resizeGrip.ReleaseMouseCapture()
    }
})

# 上部マウスホバーによるメニューバー自動表示 (オートハイド)
$cardBorder.Add_MouseMove({
    param($s, $e)
    if ($script:isMenuHidden -and -not $script:isClickThrough) {
        $pos = $e.GetPosition($cardBorder)
        if ($pos.Y -le 45) {
            &$setMenuVisibility $false
            Write-OverlayLog "上部マウスホバーを検知し、メニューバーを自動再表示しました。" "INFO"
        }
    }
})

# 背景スタイル切替
$script:isTransparentBg = $savedConfig.TransparentBg
$applyBgStyle = {
    param([bool]$transparent)
    $script:isTransparentBg = $transparent
    if ($transparent) {
        $cardBorder.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#01000000")
        $cardBorder.BorderThickness = New-Object System.Windows.Thickness(0)
        $controlBar.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#400F172A")
        $btnToggleBg.Content = "🎨 背景: 完全透明"
        Write-OverlayLog "背景スタイルを [完全透明] に変更しました。" "INFO"
    } else {
        $cardBorder.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#E60F172A")
        $cardBorder.BorderThickness = New-Object System.Windows.Thickness(2)
        $controlBar.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#901E293B")
        $btnToggleBg.Content = "🎨 背景: 半透明"
        Write-OverlayLog "背景スタイルを [半透明] に変更しました。" "INFO"
    }
}
&$applyBgStyle $script:isTransparentBg

$btnToggleBg.Add_Click({
    &$applyBgStyle (-not $script:isTransparentBg)
})

# メニューバーの表示/非表示管理
$script:isMenuHidden = $false

$setMenuVisibility = {
    param([bool]$hidden)
    $script:isMenuHidden = $hidden
    if ($hidden) {
        $controlBar.Visibility = [System.Windows.Visibility]::Collapsed
        if (-not $script:isClickThrough) {
            $btnShowMenu.Visibility = [System.Windows.Visibility]::Visible
        } else {
            $btnShowMenu.Visibility = [System.Windows.Visibility]::Collapsed
        }
        Write-OverlayLog "メニューバーを隠しました (Esc / H / 上部ホバー / 右クリック / 右上ボタンで再表示可能)" "INFO"
    } else {
        $controlBar.Visibility = [System.Windows.Visibility]::Visible
        $btnShowMenu.Visibility = [System.Windows.Visibility]::Collapsed
        Write-OverlayLog "メニューバーを再表示しました。" "INFO"
    }
    if ($script:trayItemToggleMenu) {
        $script:trayItemToggleMenu.Text = if ($hidden) { "メニューバーを表示 (ON)" } else { "メニューバーを隠す (OFF)" }
    }
}

$btnHideMenu.Add_Click({
    &$setMenuVisibility $true
})

$btnShowMenu.Add_Click({
    &$setMenuVisibility $false
})

# クリックスルー管理 (マウス完全透過)
$script:isClickThrough = $false
$script:hWnd = [IntPtr]::Zero

$updateClickThroughUI = {
    param([bool]$enabled)
    $script:isClickThrough = $enabled
    if ($script:hWnd -ne [IntPtr]::Zero) {
        Set-ClickThroughHelper $script:hWnd $enabled
    }
    if ($enabled) {
        $btnClickThrough.Content = "📌 マウス透過: ON"
        $btnClickThrough.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#CC059669")
        $controlBar.Visibility = [System.Windows.Visibility]::Collapsed
        $btnShowMenu.Visibility = [System.Windows.Visibility]::Collapsed
        $clickThroughNotice.Visibility = [System.Windows.Visibility]::Visible
        Write-OverlayLog "マウス透過を有効化しました (ON) - タスクトレイアイコンのダブルクリックまたは Ctrl+Shift+H で解除可能" "INFO"
        try {
            $notifyIcon.ShowBalloonTip(3500, "早耳 - マウス透過 ON", "字幕のマウス透過が有効です。`n解除するにはタスクトレイの早耳アイコンをダブルクリックするか、Ctrl+Shift+H を押してください。", [System.Windows.Forms.ToolTipIcon]::Info)
        } catch {}
    } else {
        $btnClickThrough.Content = "📌 マウス透過: OFF"
        $btnClickThrough.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#E01E293B")
        $clickThroughNotice.Visibility = [System.Windows.Visibility]::Collapsed
        $controlBar.Visibility = [System.Windows.Visibility]::Visible
        $btnShowMenu.Visibility = [System.Windows.Visibility]::Collapsed
        $script:isMenuHidden = $false
        Write-OverlayLog "マウス透過を解除しました (OFF)" "INFO"
    }
    if ($script:trayItemToggle) {
        $script:trayItemToggle.Text = if ($enabled) { "マウス透過を解除 (OFF)" } else { "マウス透過を有効化 (ON)" }
    }
}

$btnClickThrough.Add_Click({
    &$updateClickThroughUI (-not $script:isClickThrough)
})

# フォントサイズ調整
$btnFontBigger.Add_Click({
    $finalText.FontSize = [Math]::Min($finalText.FontSize + 2, 56)
    $partialText.FontSize = [Math]::Min($partialText.FontSize + 2, 44)
    $transText.FontSize = [Math]::Min($transText.FontSize + 4, 40)
    Write-OverlayLog "フォントサイズを拡大しました: FontSize=$($finalText.FontSize)" "INFO"
})

$btnFontSmaller.Add_Click({
    $finalText.FontSize = [Math]::Max($finalText.FontSize - 2, 14)
    $partialText.FontSize = [Math]::Max($partialText.FontSize - 2, 12)
    $transText.FontSize = [Math]::Max($transText.FontSize - 2, 10)
    Write-OverlayLog "フォントサイズを縮小しました: FontSize=$($finalText.FontSize)" "INFO"
})

# 設定ボタン
$btnWebSettings.Add_Click({
    Write-OverlayLog "Web設定画面を開きます: $ServerUrl/settings" "INFO"
    [System.Diagnostics.Process]::Start("$ServerUrl/settings") | Out-Null
})

# 閉じるボタン
$btnClose.Add_Click({
    Write-OverlayLog "閉じるボタンが押されました。" "INFO"
    $window.Close()
})

# 右クリックコンテキストメニュー (WPF ContextMenu)
$cardContextMenu = New-Object System.Windows.Controls.ContextMenu
$cardContextMenu.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F00F172A")
$cardContextMenu.BorderBrush = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#38BDF8")
$cardContextMenu.BorderThickness = New-Object System.Windows.Thickness(1)

$menuItemToggleBar = New-Object System.Windows.Controls.MenuItem
$menuItemToggleBar.Header = "👁 メニューバーを表示 / 隠す"
$menuItemToggleBar.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F8FAFC")
$menuItemToggleBar.Add_Click({
    &$setMenuVisibility (-not $script:isMenuHidden)
})
$cardContextMenu.Items.Add($menuItemToggleBar) | Out-Null

$menuItemToggleBg = New-Object System.Windows.Controls.MenuItem
$menuItemToggleBg.Header = "🎨 背景切替 (半透明 / 完全透明)"
$menuItemToggleBg.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F8FAFC")
$menuItemToggleBg.Add_Click({
    &$applyBgStyle (-not $script:isTransparentBg)
})
$cardContextMenu.Items.Add($menuItemToggleBg) | Out-Null

$menuItemClickThrough = New-Object System.Windows.Controls.MenuItem
$menuItemClickThrough.Header = "📌 マウス透過を有効化 (ON)"
$menuItemClickThrough.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F8FAFC")
$menuItemClickThrough.Add_Click({
    &$updateClickThroughUI $true
})
$cardContextMenu.Items.Add($menuItemClickThrough) | Out-Null

$cardContextMenu.Items.Add((New-Object System.Windows.Controls.Separator)) | Out-Null

$menuItemFontUp = New-Object System.Windows.Controls.MenuItem
$menuItemFontUp.Header = "🔤 文字を大きく (A+)"
$menuItemFontUp.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F8FAFC")
$menuItemFontUp.Add_Click({
    $finalText.FontSize = [Math]::Min($finalText.FontSize + 2, 56)
    $partialText.FontSize = [Math]::Min($partialText.FontSize + 2, 44)
    $transText.FontSize = [Math]::Min($transText.FontSize + 2, 40)
})
$cardContextMenu.Items.Add($menuItemFontUp) | Out-Null

$menuItemFontDown = New-Object System.Windows.Controls.MenuItem
$menuItemFontDown.Header = "🔤 文字を小さく (A-)"
$menuItemFontDown.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F8FAFC")
$menuItemFontDown.Add_Click({
    $finalText.FontSize = [Math]::Max($finalText.FontSize - 2, 14)
    $partialText.FontSize = [Math]::Max($partialText.FontSize - 2, 12)
    $transText.FontSize = [Math]::Max($transText.FontSize - 2, 10)
})
$cardContextMenu.Items.Add($menuItemFontDown) | Out-Null

$cardContextMenu.Items.Add((New-Object System.Windows.Controls.Separator)) | Out-Null

$menuItemSettings = New-Object System.Windows.Controls.MenuItem
$menuItemSettings.Header = "⚙ Web設定画面を開く"
$menuItemSettings.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F8FAFC")
$menuItemSettings.Add_Click({
    [System.Diagnostics.Process]::Start("$ServerUrl/settings") | Out-Null
})
$cardContextMenu.Items.Add($menuItemSettings) | Out-Null

$menuItemExit = New-Object System.Windows.Controls.MenuItem
$menuItemExit.Header = "✕ 終了"
$menuItemExit.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F8FAFC")
$menuItemExit.Add_Click({
    $window.Close()
})
$cardContextMenu.Items.Add($menuItemExit) | Out-Null

$cardBorder.ContextMenu = $cardContextMenu
$mainGrid.ContextMenu = $cardContextMenu

# キーボードショートカット (トンネリングで確実に捕捉: H で切り替え, Esc でメニュー再表示)
$window.Add_PreviewKeyDown({
    param($s, $e)
    if ($e.Key -eq [System.Windows.Input.Key]::H) {
        &$setMenuVisibility (-not $script:isMenuHidden)
        Write-OverlayLog "Hキー押下を検知しました (メニュー表示トグル: isMenuHidden=$script:isMenuHidden)" "INFO"
        $e.Handled = $true
    } elseif ($e.Key -eq [System.Windows.Input.Key]::Escape) {
        &$setMenuVisibility $false
        Write-OverlayLog "Escキー押下を検知しました (メニューバー再表示)" "INFO"
        $e.Handled = $true
    }
})

# タスクバートレイアイコン
$notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$notifyIcon.Text = "Hayamimi Live - デスクトップ字幕 (ダブルクリックで復帰)"
$iconStream = [System.Drawing.SystemIcons]::Application
$notifyIcon.Icon = $iconStream
$notifyIcon.Visible = $true

# トレイアイコンのダブルクリックでクリック透過解除 ＆ メニューバー強制再表示
$notifyIcon.Add_DoubleClick({
    Write-OverlayLog "タスクトレイアイコンのダブルクリックを検知しました。透過解除＆メニュー再表示を実行します。" "INFO"
    $window.Dispatcher.Invoke([Action]{
        &$updateClickThroughUI $false
        &$setMenuVisibility $false
        [OverlayWin32]::SetForegroundWindow($script:hWnd) | Out-Null
        $window.Activate() | Out-Null
        $window.Focus() | Out-Null
    })
})

$contextMenu = New-Object System.Windows.Forms.ContextMenuStrip
$script:trayItemToggle = $contextMenu.Items.Add("マウス透過を有効化 (ON)")
$script:trayItemToggle.Add_Click({
    $window.Dispatcher.Invoke([Action]{
        &$updateClickThroughUI (-not $script:isClickThrough)
    })
})

$script:trayItemToggleMenu = $contextMenu.Items.Add("メニューバーを隠す (OFF)")
$script:trayItemToggleMenu.Add_Click({
    $window.Dispatcher.Invoke([Action]{
        &$setMenuVisibility (-not $script:isMenuHidden)
    })
})

$trayItemReset = $contextMenu.Items.Add("位置を画面中央にリセット")
$trayItemReset.Add_Click({
    $window.Dispatcher.Invoke([Action]{
        $s = [System.Windows.Forms.Screen]::PrimaryScreen
        $window.Left = $s.WorkingArea.X + ($s.WorkingArea.Width - $window.Width) / 2
        $window.Top = $s.WorkingArea.Y + ($s.WorkingArea.Height - $window.Height) / 2
        Write-OverlayLog "位置を画面中央にリセットしました。" "INFO"
    })
})

$contextMenu.Items.Add("-") | Out-Null
$trayItemSettings = $contextMenu.Items.Add("Web設定画面を開く")
$trayItemSettings.Add_Click({
    [System.Diagnostics.Process]::Start("$ServerUrl/settings") | Out-Null
})

$contextMenu.Items.Add("-") | Out-Null
$trayItemExit = $contextMenu.Items.Add("オーバーレイを終了")
$trayItemExit.Add_Click({
    $window.Dispatcher.Invoke([Action]{
        $window.Close()
    })
})

$notifyIcon.ContextMenuStrip = $contextMenu

# グローバルホットキー定数 (Ctrl + Shift + H)
$script:HOTKEY_ID = 9001
$MOD_CONTROL = 0x0002
$MOD_SHIFT = 0x0004
$VK_H = 0x48

# 設定の自動保存関数
$saveSettings = {
    $cfg = @{
        Left = $window.Left
        Top = $window.Top
        Width = $window.Width
        Height = $window.Height
        FontSize = $finalText.FontSize
        TransparentBg = $script:isTransparentBg
    }
    try {
        $cfg | ConvertTo-Json | Set-Content -Path $configFile -Encoding UTF8
        Write-OverlayLog "設定を保存しました: Width=$($window.Width), Height=$($window.Height), FontSize=$($finalText.FontSize)" "INFO"
    } catch {
        Write-OverlayLog "設定の保存に失敗しました: $_" "ERROR"
    }
}

# ウィンドウ破棄時に設定保存 & トレイアイコン解放 & ホットキー解除
$window.Add_Closed({
    Write-OverlayLog "ウィンドウの終了処理を開始します。" "INFO"
    if ($script:isHotKeyRegistered -and $script:hWnd -ne [IntPtr]::Zero) {
        [OverlayWin32]::UnregisterHotKey($script:hWnd, $script:HOTKEY_ID) | Out-Null
    }
    &$saveSettings
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    if ($script:sseCancelToken) {
        $script:sseCancelToken.Cancel()
    }
    if ($script:ssePowerShell) {
        try { $script:ssePowerShell.Stop() } catch {}
        try { $script:ssePowerShell.Dispose() } catch {}
    }
    if ($script:sseRunspace) {
        try { $script:sseRunspace.Close() } catch {}
        try { $script:sseRunspace.Dispose() } catch {}
    }
    Write-OverlayLog "ウィンドウを正常に終了しました。" "INFO"
})

# ハンドル取得 & グローバルホットキー登録
$window.Add_SourceInitialized({
    $helper = New-Object System.Windows.Interop.WindowInteropHelper($window)
    $script:hWnd = $helper.Handle
    try { [OverlayWin32]::SetForegroundWindow($script:hWnd) | Out-Null } catch {}

    # Win32 メッセージフック (WM_HOTKEY: 0x0312 の受信)
    try {
        $source = [System.Windows.Interop.HwndSource]::FromHwnd($script:hWnd)
        $source.AddHook({
            param($hwnd, $msg, $wParam, $lParam, [ref]$handled)
            if ($msg -eq 0x0312 -and $wParam.ToInt32() -eq $script:HOTKEY_ID) {
                Write-OverlayLog "グローバルホットキー (Ctrl+Shift+H) を検知しました。マウス透過解除＆メニュー再表示を実行します。" "INFO"
                $window.Dispatcher.Invoke([Action]{
                    &$updateClickThroughUI $false
                    &$setMenuVisibility $false
                    [OverlayWin32]::SetForegroundWindow($script:hWnd) | Out-Null
                    $window.Activate() | Out-Null
                    $window.Focus() | Out-Null
                })
                $handled = $true
            }
        })
        $script:isHotKeyRegistered = [OverlayWin32]::RegisterHotKey($script:hWnd, $script:HOTKEY_ID, ($MOD_CONTROL -bor $MOD_SHIFT), $VK_H)
        if ($script:isHotKeyRegistered) {
            Write-OverlayLog "グローバルホットキー (Ctrl+Shift+H) を正常に登録しました。" "INFO"
        } else {
            Write-OverlayLog "グローバルホットキーの登録に失敗しました (他アプリと競合の可能性があります)。" "WARN"
        }
    } catch {
        Write-OverlayLog "ホットキー初期化例外: $_" "WARN"
    }
})

# デモモード判定
if ($Demo) {
    $finalText.Text = "早耳のデスクトップ透過字幕オーバーレイへようこそ！"
    $partialText.Text = "これは発話中にリアルタイム表示される仮テキストのプレビューです..."
    $transText.Text = "→ [en] Welcome to Hayamimi desktop transparent subtitle overlay!`n→ [zh] 欢迎使用早耳桌面透明字幕。"
} else {
    $connectingNotice.Visibility = [System.Windows.Visibility]::Visible

    $clearTimer = New-Object System.Windows.Threading.DispatcherTimer
    $clearTimer.Interval = [TimeSpan]::FromSeconds(6)
    $clearTimer.Add_Tick({
        $finalText.Text = ""
        $partialText.Text = ""
        $transText.Text = ""
        $clearTimer.Stop()
    })

    try {
        $wc = New-Object System.Net.WebClient
        $wc.Encoding = [System.Text.Encoding]::UTF8
        $styleJson = $wc.DownloadString("$ServerUrl/overlay-style")
        $s = ConvertFrom-Json $styleJson
        if ($s.size) { $finalText.FontSize = [double]$s.size }
        if ($s.color) { $finalText.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString($s.color) }
        if ($s.font) { $finalText.FontFamily = New-Object System.Windows.Media.FontFamily($s.font) }
    } catch {}

    $script:sseCancelToken = New-Object System.Threading.CancellationTokenSource
    $script:transLines = New-Object System.Collections.Generic.List[string]

    # PowerShell 7 (pwsh) および 5.1 のマルチスレッド互換のため専用 Runspace で SSE リスナーを起動
    $script:sseRunspace = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace()
    $script:sseRunspace.Open()
    $script:sseRunspace.SessionStateProxy.SetVariable("window", $window)
    $script:sseRunspace.SessionStateProxy.SetVariable("connectingNotice", $connectingNotice)
    $script:sseRunspace.SessionStateProxy.SetVariable("clearTimer", $clearTimer)
    $script:sseRunspace.SessionStateProxy.SetVariable("finalText", $finalText)
    $script:sseRunspace.SessionStateProxy.SetVariable("partialText", $partialText)
    $script:sseRunspace.SessionStateProxy.SetVariable("transText", $transText)
    $script:sseRunspace.SessionStateProxy.SetVariable("transLines", $script:transLines)
    $script:sseRunspace.SessionStateProxy.SetVariable("ServerUrl", $ServerUrl)
    $script:sseRunspace.SessionStateProxy.SetVariable("cancelToken", $script:sseCancelToken)
    $script:sseRunspace.SessionStateProxy.SetVariable("LauncherLogFile", $script:LauncherLogFile)

    $script:ssePowerShell = [System.Management.Automation.PowerShell]::Create()
    $script:ssePowerShell.Runspace = $script:sseRunspace

    $script:ssePowerShell.AddScript({
        param()
        $token = $cancelToken.Token

        function Write-SubLog([string]$msg, [string]$lvl="INFO") {
            try {
                $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
                [System.IO.File]::AppendAllText($LauncherLogFile, "[$ts] [$lvl] [デスクトップ字幕] $msg`r`n", [System.Text.Encoding]::UTF8)
            } catch {}
        }

        Write-SubLog "早耳サーバー ($ServerUrl) へのイベント接続スレッドを開始しました。" "INFO"

        while (-not $token.IsCancellationRequested) {
            $resp = $null
            $stream = $null
            $sr = $null
            try {
                $req = [System.Net.HttpWebRequest]::Create("$ServerUrl/events")
                $req.Timeout = 10000
                $req.ReadWriteTimeout = 30000
                $req.Proxy = $null

                $resp = $req.GetResponse()
                $stream = $resp.GetResponseStream()
                $sr = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)

                Write-SubLog "早耳サーバーのイベントストリーム (/events) に正常に接続しました。" "INFO"

                $window.Dispatcher.Invoke([Action]{
                    $connectingNotice.Visibility = [System.Windows.Visibility]::Collapsed
                })

                while (-not $token.IsCancellationRequested -and -not $sr.EndOfStream) {
                    $line = $sr.ReadLine()
                    if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith(":")) { continue }
                    if ($line.StartsWith("data: ")) {
                        $jsonStr = $line.Substring(6).Trim()
                        try {
                            $ev = ConvertFrom-Json $jsonStr
                            $window.Dispatcher.Invoke([Action]{
                                $clearTimer.Stop()
                                if ($ev.type -eq "partial") {
                                    $partialText.Text = $ev.text
                                    $clearTimer.Start()
                                } elseif ($ev.type -eq "final") {
                                    $finalText.Text = $ev.text
                                    $partialText.Text = ""
                                    $transLines.Clear()
                                    $transText.Text = ""
                                    $clearTimer.Start()
                                } elseif ($ev.type -eq "translation") {
                                    $transLines.Add("→ [$($ev.lang)] $($ev.text)")
                                    $transText.Text = [string]::Join("`n", $transLines)
                                    $clearTimer.Start()
                                }
                            })
                        } catch {}
                    }
                }
            } catch {
                Write-SubLog "早耳サーバー接続待機中 (未起動または再接続試行): $($_.Exception.Message)" "WARN"
                $window.Dispatcher.Invoke([Action]{
                    if ([string]::IsNullOrEmpty($finalText.Text) -and [string]::IsNullOrEmpty($partialText.Text)) {
                        $connectingNotice.Visibility = [System.Windows.Visibility]::Visible
                    }
                })
                [System.Threading.Thread]::Sleep(2500)
            } finally {
                if ($sr) { $sr.Dispose() }
                if ($stream) { $stream.Dispose() }
                if ($resp) { $resp.Dispose() }
            }
        }
        Write-SubLog "イベント接続スレッドを停止しました。" "INFO"
    }) | Out-Null

    $script:sseAsyncResult = $script:ssePowerShell.BeginInvoke()
}

$window.ShowDialog() | Out-Null
