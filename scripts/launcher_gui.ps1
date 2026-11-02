$script:AppVersion = "2.0.0"
$repoRoot = Split-Path -Parent $PSScriptRoot
$versionTxtFile = Join-Path $repoRoot "version.txt"
if (Test-Path $versionTxtFile) {
    try {
        $readVer = (Get-Content -Path $versionTxtFile -Raw -Encoding UTF8).Trim()
        if ($readVer) { $script:AppVersion = $readVer }
    } catch {}
}
$version = "GUI v$script:AppVersion"
$script:VersionCheckUrl = "https://raw.githubusercontent.com/Maximilian2022/hayamimi-live/main/version.txt"

# --- 既存ユーザーの Git Remote URL 自動更新 (hayamimi -> hayamimi-live) ---
try {
    $currentOrigin = (git remote get-url origin 2>$null)
    if ($currentOrigin -and $currentOrigin -match "Maximilian2022/hayamimi(\.git)?$") {
        git remote set-url origin "https://github.com/Maximilian2022/hayamimi-live.git" 2>$null
    }
} catch {}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# --- スマートアプリコントロール (SAC) 対策: 動的 C# コンパイル (Add-Type -TypeDefinition) を排除 ---
# Add-Type -TypeDefinition は %TEMP% に未署名の一時 .dll を生成するため、Windows 11 の
# スマートアプリコントロール (SAC) によるブロックやクラッシュの原因となる。
# コンソール非表示は start.vbs / start.bat (-WindowStyle Hidden) で担保し、
# プロセス出力ポンプは .NET 標準の ReadLineAsync() で安全に処理する。

<#
.SYNOPSIS
    hayamimi (早耳) - WSL Containers (WSL3) ランチャー GUI
.DESCRIPTION
    WPFを用いたモダンなダークUIランチャー。
    WSL Containers (wslc) を制御して hayamimi コンテナを起動・停止し、
    コンテナログ同期、起動完了後のブラウザ自動表示を行います。
#>

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName System.Drawing

# --- WPF ハードウェアアクセラレーション起因の Win32 メッセージクォータ溢れ (エラー 1816) を完全防止 ---
[System.Windows.Media.RenderOptions]::ProcessRenderMode = [System.Windows.Interop.RenderMode]::SoftwareOnly

[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

# --- Windows Defender「コントロールされたフォルダー アクセス」ブロック防止 ---
# OneDrive や保護フォルダー配下での Python バイトコードキャッシュ (__pycache__) 作成を抑止
$env:PYTHONDONTWRITEBYTECODE = "1"
# Python 出力の UTF-8 固定 (Windows CP932 / Shift-JIS 日本語ログ文字化け完全防止)
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# 1. パスの動的解決 (他環境での実行に対応)
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir -and $MyInvocation.MyCommand.Path) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $ScriptDir) {
    $ScriptDir = (Get-Location).Path
}
$ProjectRoot = Split-Path -Parent $ScriptDir

# logs フォルダの自動作成
$LogsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}
$LogFile = Join-Path $LogsDir "launcher.log"
$ContainerLogFile = Join-Path $LogsDir "container.log"

# 過去のバグで発生した二重ネストフォルダ (scripts/scripts, docs/docs 等) の自動修復
try {
    $nestedCandidateDirs = @("scripts", "docs", "tests", "demo", "mobile", "nginx", ".github")
    foreach ($cand in $nestedCandidateDirs) {
        $pDir = Join-Path $ProjectRoot $cand
        $nDir = Join-Path $pDir $cand
        if ((Test-Path $pDir) -and (Test-Path $nDir) -and (Get-Item $nDir).PSIsContainer) {
            $childItems = Get-ChildItem -Path $nDir
            foreach ($ci in $childItems) {
                $targetCiPath = Join-Path $pDir $ci.Name
                if (-not (Test-Path $targetCiPath)) {
                    Move-Item -Path $ci.FullName -Destination $targetCiPath -Force
                }
            }
            Remove-Item -Path $nDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} catch {}

$ContainerName = "hayamimi_app"
$ImageName = "hayamimi:latest"
$MicUrl = "http://localhost:8833/mic"
$ObsUrl = "http://localhost:8833/"
$DashboardUrl = "http://localhost:8833/dashboard"
$ProfileUrl = "http://localhost:8833/profile"

# WPF TextBox メモリ肥大化・描画ハング防止用トリミング設定 (約1,500〜2,000行を維持)
$script:MaxLogTextLength = 80000
$script:TrimTargetLength = 60000

function Trim-LogTextBox {
    if (-not $logTextBox) { return }
    $len = $logTextBox.Text.Length
    if ($len -gt $script:MaxLogTextLength) {
        $cutIdx = $len - $script:TrimTargetLength
        $newlineIdx = $logTextBox.Text.IndexOf("`n", $cutIdx)
        if ($newlineIdx -gt 0 -and $newlineIdx -lt ($len - 100)) {
            $logTextBox.Text = $logTextBox.Text.Substring($newlineIdx + 1)
        } else {
            $logTextBox.Text = $logTextBox.Text.Substring($cutIdx)
        }
    }
}

# ランチャー用ロギング関数 (単一メッセージ)
function Write-AppLog {
    param([string]$Message, [string]$Level = "INFO")
    $now = Get-Date
    $timestamp = $now.ToString("yyyy-MM-dd HH:mm:ss")
    $timeOnly = $now.ToString("HH:mm:ss")
    $logLine = "[$timestamp] [$Level] $Message"

    try {
        [System.IO.File]::AppendAllText($LogFile, "$logLine`r`n", [System.Text.Encoding]::UTF8)
    } catch {}

    if ($logTextBox -and $logScrollViewer) {
        $logTextBox.AppendText("[$timeOnly] $Message`r`n")
        Trim-LogTextBox
        $logScrollViewer.ScrollToEnd()
    }
}

# ランチャー用ロギング関数 (複数行バッチ・メッセージキュー溢れ防止)
function Write-AppLogBatch {
    param([System.Collections.IEnumerable]$Messages, [string]$Level = "INFO")
    if (-not $Messages) { return }
    $now = Get-Date
    $timestamp = $now.ToString("yyyy-MM-dd HH:mm:ss")
    $timeOnly = $now.ToString("HH:mm:ss")

    $fileSb = [System.Text.StringBuilder]::new()
    $guiSb = [System.Text.StringBuilder]::new()
    $count = 0

    foreach ($msg in $Messages) {
        if (-not $msg) { continue }
        [void]$fileSb.AppendLine("[$timestamp] [$Level] $msg")
        [void]$guiSb.AppendLine("[$timeOnly] $msg")
        $count++
    }
    if ($count -eq 0) { return }

    try {
        [System.IO.File]::AppendAllText($LogFile, $fileSb.ToString(), [System.Text.Encoding]::UTF8)
    } catch {}

    if ($logTextBox -and $logScrollViewer) {
        $logTextBox.AppendText($guiSb.ToString())
        Trim-LogTextBox
        $logScrollViewer.ScrollToEnd()
    }
}

# --- スマートアプリコントロール (SAC) & Mark of the Web (Zone.Identifier) 自動解除ヘルパー ---
function Unblock-ProjectFiles {
    param([string]$RootDir = $ProjectRoot)
    try {
        if (-not $RootDir -or -not (Test-Path $RootDir)) { return }
        # ルート直下および主要サブフォルダのスクリプト・バイナリから Zone.Identifier ストリームを除去
        # ルート直下ファイルのみ (Recurse なしで .venv の約9,000ファイルを完全スキップ: P-1)
        Get-ChildItem -LiteralPath $RootDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -in @(".ps1", ".bat", ".vbs", ".py", ".cmd") } |
            ForEach-Object { try { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue } catch {} }

        $subDirs = @(
            (Join-Path $RootDir "scripts"),
            (Join-Path $RootDir "tools"),
            (Join-Path $RootDir ".venv\Scripts")
        )
        foreach ($d in $subDirs) {
            if (Test-Path $d) {
                Get-ChildItem -LiteralPath $d -Recurse -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.Extension -in @(".ps1", ".bat", ".vbs", ".py", ".exe", ".dll", ".pyd") } |
                    ForEach-Object {
                        try { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue } catch {}
                    }
            }
        }
        # PowerShell 5.1 互換性のため .ps1 ファイルに正確に単一の UTF-8 BOM (EF BB BF) を保証 (多重BOM自己治癒)
        $ps1Files = @(
            (Join-Path $RootDir "setup.ps1"),
            (Join-Path $RootDir "scripts\launcher_gui.ps1"),
            (Join-Path $RootDir "scripts\desktop_overlay.ps1")
        )
        foreach ($pf in $ps1Files) {
            if (Test-Path $pf) {
                try {
                    $b = [System.IO.File]::ReadAllBytes($pf)
                    $offset = 0
                    while ($offset + 3 -le $b.Length -and $b[$offset] -eq 239 -and $b[$offset+1] -eq 187 -and $b[$offset+2] -eq 191) {
                        $offset += 3
                    }
                    if ($offset -ne 3) {
                        $contentWithoutBom = if ($offset -gt 0) { $b[$offset..($b.Length - 1)] } else { $b }
                        $singleBom = [byte[]]@(239, 187, 191)
                        [System.IO.File]::WriteAllBytes($pf, ($singleBom + $contentWithoutBom))
                    }
                } catch {}
            }
        }
    } catch {}
}

$script:UvBlockedBySac = $false

# スマートアプリコントロール (SAC) でブロックされない 안전한 uv 実行可能ファイルの検証・取得関数
function Get-SafeUvCommand {
    if ($script:UvBlockedBySac) { return $null }
    $cand = $null
    try {
        $sysUv = (where.exe uv 2>$null | Select-Object -First 1)
        if ($sysUv -and (Test-Path $sysUv)) {
            $cand = $sysUv
        } else {
            $localUv = Join-Path $ProjectRoot "tools\uv\uv.exe"
            if (Test-Path $localUv) {
                $cand = $localUv
            }
        }
    } catch {}

    if (-not $cand) { return $null }

    try {
        Unblock-File -LiteralPath $cand -ErrorAction SilentlyContinue
        $pinfo = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfo.FileName = $cand
        $pinfo.Arguments = "--version"
        $pinfo.RedirectStandardOutput = $true
        $pinfo.RedirectStandardError = $true
        $pinfo.UseShellExecute = $false
        $pinfo.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($pinfo)
        $proc.WaitForExit(2000) | Out-Null
        if ($proc.ExitCode -eq 0) {
            return $cand
        }
    } catch {
        # スマートアプリコントロール (SAC) 等による起動ブロックを検知した場合
        $script:UvBlockedBySac = $true
        Write-AppLog "スマートアプリコントロール等のセキュリティポリシーにより uv.exe が制限されているため、公式 Python (pip) に自動フォールバックします。" "WARN"
        # 次回以降 SAC のブロックポップアップが出ないようローカルの未署名 uv.exe をリネーム隔離
        $localUv = Join-Path $ProjectRoot "tools\uv\uv.exe"
        if (Test-Path $localUv) {
            try { Move-Item -LiteralPath $localUv -Destination "$localUv.blocked" -Force -ErrorAction SilentlyContinue } catch {}
        }
        return $null
    }
    return $null
}

# UI メッセージループ維持 & 外部プロセス非同期逐次ロギング待機関数 (Add-Type不要 / パイプ詰まりデッドロック完全根絶)
function Wait-ProcessWithUiPump {
    param(
        [System.Diagnostics.Process]$Proc,
        [string]$LogPrefix = "[処理中]"
    )
    if (-not $Proc) { return }

    $outTask = $null
    $errTask = $null
    try { $outTask = $Proc.StandardOutput.ReadLineAsync() } catch {}
    try { $errTask = $Proc.StandardError.ReadLineAsync() } catch {}

    while (($null -ne $outTask) -or ($null -ne $errTask) -or (-not $Proc.HasExited)) {
        # WPF UI メッセージループを明示的に実行し、OSからの「応答なし」判定を根絶
        try {
            [System.Windows.Threading.Dispatcher]::CurrentDispatcher.Invoke([Action]{}, [System.Windows.Threading.DispatcherPriority]::Background)
        } catch {}

        # 標準出力の非ブロッキング逐次読み取り
        while (($null -ne $outTask) -and $outTask.IsCompleted) {
            $line = $null
            try { $line = $outTask.GetAwaiter().GetResult() } catch { $line = $null }
            if ($null -ne $line) {
                if ($line.Trim()) {
                    Write-AppLog "$LogPrefix $($line.Trim())" "INFO"
                }
                try { $outTask = $Proc.StandardOutput.ReadLineAsync() } catch { $outTask = $null }
            } else {
                $outTask = $null
            }
        }

        # 標準エラーの非ブロッキング逐次読み取り
        while (($null -ne $errTask) -and $errTask.IsCompleted) {
            $errLine = $null
            try { $errLine = $errTask.GetAwaiter().GetResult() } catch { $errLine = $null }
            if ($null -ne $errLine) {
                if ($errLine.Trim()) {
                    Write-AppLog "$LogPrefix $($errLine.Trim())" "INFO"
                }
                try { $errTask = $Proc.StandardError.ReadLineAsync() } catch { $errTask = $null }
            } else {
                $errTask = $null
            }
        }

        if (($null -ne $outTask) -or ($null -ne $errTask) -or (-not $Proc.HasExited)) {
            Start-Sleep -Milliseconds 40
        }
    }
    try { $Proc.WaitForExit(1000) | Out-Null } catch {}
}

# --- 高精度クラウド同期検出 (レジストリ / Cloud Filter 属性 / パス名非依存) ---
function Test-CloudSyncPath {
    param([string]$Path)
    try {
        if (-not $Path) { return $false }

        # 1. Windows レジストリに登録された全 OneDrive 同期ルート (個人/学校/法人)
        $odRoots = Get-ChildItem "HKCU:\Software\Microsoft\OneDrive\Accounts" -ErrorAction SilentlyContinue |
            ForEach-Object { (Get-ItemProperty $_.PSPath).UserFolder } |
            Where-Object { $_ -and (Test-Path $_) }

        foreach ($r in $odRoots) {
            if ($Path.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }

        # 2. 環境変数 (OneDrive, OneDriveConsumer, OneDriveCommercial)
        $envRoots = @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial) | Where-Object { $_ -and (Test-Path $_) }
        foreach ($er in $envRoots) {
            if ($Path.StartsWith($er, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }

        # 3. リダイレクトされた User Shell Folders (Documents, Desktop)
        $shellFolders = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -ErrorAction SilentlyContinue
        if ($shellFolders) {
            $specialPaths = @($shellFolders.Personal, $shellFolders.Desktop) | Where-Object { $_ -and (Test-Path $_) }
            foreach ($sp in $specialPaths) {
                if ($Path.StartsWith($sp, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $item = Get-Item $sp -Force -ErrorAction SilentlyContinue
                    if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                        return $true
                    }
                }
            }
        }

        # 4. パス文字列フォールバック
        if ($Path -match "(?i)OneDrive|Dropbox|Google Drive|iCloudDrive") {
            return $true
        }
    } catch {}
    return $false
}

# --- 案1: OneDrive モデル永続ピン留め保護 (+p -u: 完全ポータブル) ---
function Protect-ModelFiles {
    <#
    .SYNOPSIS
        OneDrive のファイルオンデマンドによるモデル消失 (0バイトスタブ化) を防ぐため、
        models フォルダに「常にこのデバイスに保持する (+p -u)」属性を付与します。
    #>
    try {
        $modelsDir = Join-Path $ProjectRoot "models"
        if (-not (Test-Path $modelsDir)) { return }

        $isCloud = Test-CloudSyncPath $ProjectRoot
        if ($isCloud) {
            $null = cmd.exe /c "attrib.exe +p -u `"$modelsDir`" /s /d" 2>&1
            Write-AppLog "✔ OneDrive クラウド保護: models フォルダをローカル永続保持に設定しました (attrib +p -u)" "INFO"
        }
    } catch {
        Write-AppLog "モデル保護属性の適用中に警告が発生しました: $_" "WARN"
    }
}

function Test-ModelsReady {
    $modelsDir = Join-Path $ProjectRoot "models"
    $coreRzKey = Join-Path $modelsDir "sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17\tokens.txt"
    $coreLidKey = Join-Path $modelsDir "sherpa-onnx-whisper-tiny\tiny-tokens.txt"
    $fuguKey = Join-Path $modelsDir "mojicast-fugumt-ja-en-ct2\source.spm"
    $m2mKey = Join-Path $modelsDir "mojicast-m2m100-ct2\model.bin"
    $punct4Key = Join-Path $modelsDir "punct-ja-4class-permissive\punct_4class.onnx"
    return (Test-Path $coreRzKey) -and ((Get-Item $coreRzKey -Force).Length -gt 0) -and `
           (Test-Path $coreLidKey) -and ((Get-Item $coreLidKey -Force).Length -gt 0) -and `
           (Test-Path $fuguKey) -and ((Get-Item $fuguKey -Force).Length -gt 0) -and `
           (Test-Path $m2mKey) -and ((Get-Item $m2mKey -Force).Length -gt 0) -and `
           (Test-Path $punct4Key) -and ((Get-Item $punct4Key -Force).Length -gt 0)
}

# --- 案3: 自律セルフヒーリング (モデル実体サイズ検証 & 自動修復) ---
function Ensure-ModelsIntegrity {
    <#
    .SYNOPSIS
        AIモデルの実体存在・ファイルサイズを検証し、OneDrive による0バイト化や欠落を検知した場合は
        自動で再取得 (自己修復) を行い、ローカル永続保持属性 (+p) を付与します。
    #>
    param([bool]$SilentIfReady = $false)
    try {
        $modelsDir = Join-Path $ProjectRoot "models"
        if (-not (Test-Path $modelsDir)) {
            New-Item -ItemType Directory -Path $modelsDir -Force | Out-Null
        }

        if (Test-ModelsReady) {
            Protect-ModelFiles
            if (-not $SilentIfReady) {
                Write-AppLog "✔ Core AI モデル実体を確認しました (完全ポータブル保持)" "INFO"
            }
            return $true
        }

        # 欠落または 0バイト化されている場合は自動修復
        Write-AppLog "⚠ AIモデルの欠落またはオンライン未水和 (0バイト) を検知しました。自動修復を開始します..." "WARN"
        Set-UIState "モデル自己修復中..." $brushOrange $false $false

        $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
        $dlScript = Join-Path $ProjectRoot "scripts\download_models.py"

        if (-not (Test-Path $venvPython)) {
            $venvPython = (where.exe python 2>$null | Select-Object -First 1)
        }

        if ($venvPython -and (Test-Path $dlScript)) {
            $pinfoDl = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoDl.FileName = $venvPython
            $pinfoDl.Arguments = "-B `"$dlScript`" --minimal --translators --punct-4class"
            $pinfoDl.WorkingDirectory = $ProjectRoot
            $pinfoDl.RedirectStandardOutput = $true
            $pinfoDl.RedirectStandardError = $true
            $pinfoDl.UseShellExecute = $false
            $pinfoDl.CreateNoWindow = $true
            $pinfoDl.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
            $procDl = [System.Diagnostics.Process]::Start($pinfoDl)
            Wait-ProcessWithUiPump -Proc $procDl -LogPrefix "[モデル自己修復]"

            if ($procDl.ExitCode -eq 0) {
                Protect-ModelFiles
                Write-AppLog "✔ AIモデルの自己修復が完了しました！" "INFO"
                Set-UIState "停止中" $brushGray $true $false
                return $true
            } else {
                Write-AppLog "❌ AIモデルの自己修復が完了しませんでした (終了コード: $($procDl.ExitCode))。" "WARN"
                Set-UIState "停止中" $brushGray $true $false
                return $false
            }
        } else {
            Write-AppLog "Python 環境が準備されていないため、モデル自己修復をスキップしました。" "WARN"
            return $false
        }
    } catch {
        Write-AppLog "モデル整合性検証中に例外が発生しました: $_" "WARN"
        return $false
    }
}

$script:LastContainerLogLineCount = 0

# WSL Containers (wslc.exe) パス解決関数 (PATH未登録環境や Program Files 直下も自動検出)
function Get-WslcCommand {
    try {
        $cmd = Get-Command "wslc" -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        $whereCmd = where.exe wslc 2>$null
        if ($whereCmd) { return ($whereCmd -split "`r?`n")[0].Trim() }
    } catch {}

    $knownPaths = @(
        "C:\Program Files\WSL\wslc.exe",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\wslc.exe",
        "C:\Windows\System32\wslc.exe"
    )
    foreach ($p in $knownPaths) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$script:WslcCmd = Get-WslcCommand

# コンテナログ更新関数 (logs/container.log に同期出力し、GUIにも新着行を反映)
function Update-ContainerLogFile {
    param([bool]$EchoToGui = $true)
    if (-not $script:WslcCmd) { return }
    try {
        $pinfo = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfo.FileName = $script:WslcCmd
        $pinfo.Arguments = "logs $ContainerName"
        $pinfo.RedirectStandardOutput = $true
        $pinfo.RedirectStandardError = $true
        $pinfo.UseShellExecute = $false
        $pinfo.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($pinfo)
        $rawLogs = $proc.StandardOutput.ReadToEnd()
        $proc.WaitForExit(1500) | Out-Null

        if ($rawLogs) {
            $lines = $rawLogs -split "[\r\n]+"
            $validLines = [System.Collections.Generic.List[string]]::new()
            foreach ($line in $lines) {
                $lStr = "$line"
                if ($lStr.Trim() -and $lStr -notmatch "Error: No such container" -and $lStr -notmatch "failed to find container" -and $lStr -notmatch "Error response from daemon") {
                    $validLines.Add($lStr.Trim())
                }
            }
            if ($validLines.Count -gt $script:LastContainerLogLineCount) {
                $text = ($validLines -join "`r`n") + "`r`n"
                [System.IO.File]::WriteAllText($ContainerLogFile, $text, [System.Text.Encoding]::UTF8)

                if ($EchoToGui -and $logTextBox) {
                    $newContainerLines = [System.Collections.Generic.List[string]]::new()
                    for ($i = $script:LastContainerLogLineCount; $i -lt $validLines.Count; $i++) {
                        $item = $validLines[$i]
                        if ($item.Trim()) {
                            $newContainerLines.Add("[コンテナ] $item")
                        }
                    }
                    if ($newContainerLines.Count -gt 0) {
                        Write-AppLogBatch -Messages $newContainerLines -Level "CONTAINER"
                    }
                }
                $script:LastContainerLogLineCount = $validLines.Count
            }
        }
    } catch {}
}

# XAML 定義
[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Hayamimi Live ランチャー (GUI v1.6.5)"
        Height="780" Width="780"
        MinHeight="680" MinWidth="720"
        WindowStartupLocation="CenterScreen"
        ResizeMode="CanResizeWithGrip"
        Background="#0B0F17"
        Foreground="#ECE5D8">
    <Window.Resources>
        <FontFamily x:Key="MainFont">Yu Gothic UI, 'Segoe UI', Meiryo UI, sans-serif</FontFamily>
        <FontFamily x:Key="MonoFont">'Cascadia Code', Consolas, monospace</FontFamily>
        
        <!-- 基本ボタンスタイル (丸み・ホバー微発光) -->
        <Style TargetType="Button">
            <Setter Property="FontFamily" Value="{StaticResource MainFont}"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Padding" Value="14,8"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="border"
                                Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}"
                                BorderThickness="{TemplateBinding BorderThickness}"
                                CornerRadius="7"
                                Padding="{TemplateBinding Padding}"
                                SnapsToDevicePixels="True">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="border" Property="Opacity" Value="0.90"/>
                            </Trigger>
                            <Trigger Property="IsPressed" Value="True">
                                <Setter TargetName="border" Property="Opacity" Value="0.75"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="border" Property="Opacity" Value="0.35"/>
                                <Setter Property="Cursor" Value="Arrow"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>

    <Grid Margin="20,18,20,18">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/> <!-- ヘッダー (Row 0) -->
            <RowDefinition Height="Auto"/> <!-- ステータス &amp; 設定カード (Row 1) -->
            <RowDefinition Height="Auto"/> <!-- 操作ボタンエリア (Row 2) -->
            <RowDefinition Height="Auto"/> <!-- Web画面クイックリンク (Row 3) -->
            <RowDefinition Height="*"/>    <!-- ログコンソール (Row 4, 伸縮) -->
        </Grid.RowDefinitions>

        <!-- 0. ヘッダー -->
        <Border Grid.Row="0" Margin="0,0,0,14" Padding="0,0,0,12" BorderBrush="#1E2638" BorderThickness="0,0,0,1">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <StackPanel Orientation="Vertical">
                    <StackPanel Orientation="Horizontal">
                        <TextBlock Text="Hayamimi Live" FontSize="26" FontWeight="ExtraBold" Foreground="#F8FAFC" FontFamily="{StaticResource MainFont}"/>
                        <TextBlock Text=" (早耳 Live)" FontSize="22" FontWeight="Bold" Foreground="#FF6B4A" FontFamily="{StaticResource MainFont}" Margin="6,2,0,0"/>
                    </StackPanel>
                    <TextBlock Text="リアルタイム音声認識 &amp; 多言語同時翻訳 (ローカル完結・高速ASR)" 
                               FontSize="12" Foreground="#8B949E" Margin="2,4,0,0"/>
                </StackPanel>
                <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
                    <Border x:Name="GpuBadgeBorder" Background="#0C241E" CornerRadius="12" Padding="12,5" Margin="0,0,8,0" BorderBrush="#10B981" BorderThickness="1">
                        <TextBlock x:Name="GpuBadge" Text="⚡ GPU: 検出中..." FontSize="11" Foreground="#10B981" FontWeight="SemiBold"/>
                    </Border>
                    <Border Background="#1E1B4B" CornerRadius="12" Padding="10,5" Margin="0,0,8,0" BorderBrush="#4338CA" BorderThickness="1">
                        <TextBlock x:Name="VersionBadge" Text="GUI v1.6.5" FontSize="11" Foreground="#A5B4FC" FontWeight="Bold" FontFamily="{StaticResource MonoFont}"/>
                    </Border>
                    <Border Background="#141B2D" CornerRadius="12" Padding="10,5" BorderBrush="#253350" BorderThickness="1">
                        <TextBlock x:Name="RuntimeBadge" Text="Python venv" FontSize="11" Foreground="#38BDF8" FontWeight="SemiBold"/>
                    </Border>
                </StackPanel>
            </Grid>
        </Border>

        <!-- 1. ステータス &amp; 設定カード -->
        <Border Grid.Row="1" Background="#131924" CornerRadius="10" Padding="16,14" Margin="0,0,0,14" BorderBrush="#21293B" BorderThickness="1">
            <Grid>
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/> <!-- 実行基盤セレクター -->
                    <RowDefinition Height="Auto"/> <!-- 状態表示行 -->
                    <RowDefinition Height="Auto"/> <!-- プログレスバー -->
                    <RowDefinition Height="Auto"/> <!-- アラートバナー -->
                    <RowDefinition Height="Auto"/> <!-- フッター情報 -->
                </Grid.RowDefinitions>

                <!-- 実行基盤 & 推論設定 セレクター (集約・整列) -->
                <Border Grid.Row="0" Margin="0,0,0,12" Background="#0D111A" CornerRadius="8" Padding="14,10" BorderBrush="#1E2638" BorderThickness="1">
                    <Grid>
                        <Grid.RowDefinitions>
                            <RowDefinition Height="Auto"/>
                            <RowDefinition Height="Auto"/>
                        </Grid.RowDefinitions>
                        <!-- 実行基盤 -->
                        <Grid Grid.Row="0" Margin="0,0,0,8">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="Auto"/>
                                <ColumnDefinition Width="*"/>
                            </Grid.ColumnDefinitions>
                            <TextBlock Text="実行基盤:" FontSize="12" FontWeight="SemiBold" Foreground="#94A3B8" VerticalAlignment="Center" Margin="0,0,16,0" Width="68"/>
                            <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
                                <RadioButton x:Name="RbModeVenv" Content="🐍 Python venv (ネイティブ / 推奨)" GroupName="EngineMode" Foreground="#F1F5F9" FontSize="12.5" Margin="0,0,24,0" VerticalAlignment="Center" Cursor="Hand"/>
                                <RadioButton x:Name="RbModeWslc" Content="📦 WSL Containers (WSL3 コンテナ)" GroupName="EngineMode" Foreground="#F1F5F9" FontSize="12.5" VerticalAlignment="Center" Cursor="Hand"/>
                            </StackPanel>
                        </Grid>
                        <!-- 推論設定 (CPU/NPU) -->
                        <Grid Grid.Row="1">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="Auto"/>
                                <ColumnDefinition Width="*"/>
                            </Grid.ColumnDefinitions>
                            <TextBlock Text="推論設定:" FontSize="12" FontWeight="SemiBold" Foreground="#94A3B8" VerticalAlignment="Center" Margin="0,0,16,0" Width="68"/>
                            <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
                                <RadioButton x:Name="RbDeviceCpu" Content="⚙ CPU 起動" GroupName="DeviceMode" Foreground="#F1F5F9" FontSize="12.5" Margin="0,0,24,0" VerticalAlignment="Center" Cursor="Hand"/>
                                <RadioButton x:Name="RbDeviceNpu" Content="⚡ NPU 起動" GroupName="DeviceMode" Foreground="#F1F5F9" FontSize="12.5" VerticalAlignment="Center" Cursor="Hand"/>
                            </StackPanel>
                        </Grid>
                    </Grid>
                </Border>

                <!-- 状態表示行 -->
                <Grid Grid.Row="1" Margin="2,0,2,0">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>
                    
                    <Ellipse x:Name="StatusLed" Grid.Column="0" Width="14" Height="14" Fill="#64748B" Margin="0,0,12,0" VerticalAlignment="Center"/>
                    
                    <StackPanel Grid.Column="1" VerticalAlignment="Center">
                        <TextBlock x:Name="StatusLabel" Text="実行状態" FontSize="11" Foreground="#64748B"/>
                        <TextBlock x:Name="StatusText" Text="停止中" FontSize="16" FontWeight="Bold" Foreground="#F8FAFC" Margin="0,2,0,0"/>
                    </StackPanel>

                    <StackPanel Grid.Column="2" Orientation="Horizontal" VerticalAlignment="Center">
                        <Border Background="#1A2234" CornerRadius="6" Padding="10,4" Margin="0,0,8,0" BorderBrush="#26334D" BorderThickness="1">
                            <TextBlock Text="HTTP: 8833" FontSize="11" Foreground="#94A3B8" FontFamily="{StaticResource MonoFont}"/>
                        </Border>
                        <Border Background="#1A2234" CornerRadius="6" Padding="10,4" BorderBrush="#26334D" BorderThickness="1">
                            <TextBlock Text="WS: 8766" FontSize="11" Foreground="#94A3B8" FontFamily="{StaticResource MonoFont}"/>
                        </Border>
                    </StackPanel>
                </Grid>

                <!-- 初期化・モデルダウンロード プログレスバー -->
                <ProgressBar x:Name="InitProgressBar" Grid.Row="2" Height="4" Margin="0,10,0,2" Foreground="#10B981" Background="#0D111A" BorderThickness="0" IsIndeterminate="True" Visibility="Collapsed"/>

                <!-- アシスト・診断バナー (必要な時だけアクションボタンを1箇所に集約表示) -->
                <Border x:Name="WslcAlertBanner" Grid.Row="3" Margin="0,10,0,0" Padding="12,10" Background="#241315" BorderBrush="#EF4444" BorderThickness="1" CornerRadius="8" Visibility="Collapsed">
                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <StackPanel Grid.Column="0" VerticalAlignment="Center">
                            <TextBlock x:Name="WslcAlertTitle" Text="⚠ WSL 3.0 / 仮想化基盤が無効です" FontSize="12.5" FontWeight="Bold" Foreground="#F87171"/>
                            <TextBlock x:Name="WslcAlertDesc" Text="hayamimi の実行に必要な仮想化機能と WSL 3.0 を自動セットアップできます。" FontSize="11.5" Foreground="#CBD5E1" Margin="0,3,0,0" TextWrapping="Wrap"/>
                        </StackPanel>
                        <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center" Margin="12,0,0,0">
                            <Button x:Name="BtnBiosGuide" Content="📖 BIOS 設定ガイド" Background="#334155" Foreground="#F8FAFC" FontSize="11.5" FontWeight="SemiBold" Padding="11,6" Margin="0,0,8,0" Visibility="Collapsed"/>
                            <Button x:Name="BtnUpdateWsl" Content="📥 仮想化 &amp; WSL3 自動設定" Background="#10B981" Foreground="#041E15" FontSize="12" FontWeight="Bold" Padding="14,7"/>
                        </StackPanel>
                    </Grid>
                </Border>

                <!-- フッター &amp; WSL3 追加セットアップボタン (常時アクセス可能) -->
                <Border Grid.Row="4" Margin="0,10,0,0" Padding="10,7" Background="#0D111A" CornerRadius="7" BorderBrush="#1C2434" BorderThickness="1">
                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBlock Grid.Column="0" Text="💡 リアルタイム翻訳言語（英語・中国語・韓国語等）は Webのマイク画面 (/mic) で自由に変更できます。" 
                                   FontSize="11" Foreground="#8B949E" VerticalAlignment="Center"/>
                        <Button x:Name="BtnInstallWslAlways" Grid.Column="1" Content="📦 WSL 3.0 を追加セットアップ" Background="#1A2234" BorderBrush="#273550" BorderThickness="1" Foreground="#94A3B8" FontSize="11" Padding="10,4" Margin="10,0,0,0" ToolTip="WSL Containers 環境の有効化・インストールを行います"/>
                    </Grid>
                </Border>
            </Grid>
        </Border>

        <!-- 2. 操作ボタンエリア -->
        <Grid Grid.Row="2" Margin="0,0,0,14">
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="10"/>
                <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="1.25*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="1.0*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="1.05*"/>
            </Grid.ColumnDefinitions>

            <!-- 起動ボタン -->
            <Button x:Name="BtnStart" Grid.Row="0" Grid.Column="0" Background="#10B981" Foreground="#041E15" Height="48">
                <StackPanel Orientation="Horizontal">
                    <TextBlock Text="▶" FontSize="16" Margin="0,0,8,0" VerticalAlignment="Center"/>
                    <TextBlock x:Name="BtnStartText" Text="アプリ起動 &amp; Webを開く" FontSize="14.5" FontWeight="Bold" VerticalAlignment="Center"/>
                </StackPanel>
            </Button>

            <!-- 再起動ボタン -->
            <Button x:Name="BtnRestart" Grid.Row="0" Grid.Column="2" Background="#0284C7" Foreground="#FFFFFF" Height="48" ToolTip="実行中のアプリを停止し、ポートや残留プロセスを解放して再起動します">
                <StackPanel Orientation="Horizontal">
                    <TextBlock Text="🔄" FontSize="14" Margin="0,0,6,0" VerticalAlignment="Center"/>
                    <TextBlock x:Name="BtnRestartText" Text="アプリ再起動" FontSize="14.5" FontWeight="Bold" VerticalAlignment="Center"/>
                </StackPanel>
            </Button>

            <!-- 終了ボタン -->
            <Button x:Name="BtnStop" Grid.Row="0" Grid.Column="4" Background="#EF4444" Foreground="#FFFFFF" Height="48" IsEnabled="False">
                <StackPanel Orientation="Horizontal">
                    <TextBlock Text="■" FontSize="15" Margin="0,0,8,0" VerticalAlignment="Center"/>
                    <TextBlock x:Name="BtnStopText" Text="アプリ終了 &amp; 閉じる" FontSize="14.5" FontWeight="Bold" VerticalAlignment="Center"/>
                </StackPanel>
            </Button>

            <!-- アップデート・リビルドエリア -->
            <Grid Grid.Row="2" Grid.ColumnSpan="5">
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="8"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <TextBlock x:Name="UpdateDescText" Grid.Column="0" Text="※ GitHubの最新コード取得と依存パッケージ更新を行えます。" FontSize="11" Foreground="#64748B" VerticalAlignment="Center"/>
                <Button x:Name="BtnUpdateApp" Grid.Column="1" Background="#141E2E" BorderBrush="#23344D" BorderThickness="1" Foreground="#38BDF8" Height="30" Padding="12,4" FontSize="11" ToolTip="GitHubから最新のソースコードを取得します">
                    <StackPanel Orientation="Horizontal">
                        <TextBlock x:Name="BtnUpdateAppIcon" Text="📥" FontSize="11" Margin="0,0,5,0" VerticalAlignment="Center"/>
                        <TextBlock x:Name="BtnUpdateAppText" Text="本体を最新版に更新" FontSize="11" FontWeight="SemiBold" VerticalAlignment="Center"/>
                    </StackPanel>
                </Button>
                <Button x:Name="BtnRebuild" Grid.Column="3" Background="#141E2E" BorderBrush="#23344D" BorderThickness="1" Foreground="#94A3B8" Height="30" Padding="12,4" FontSize="11" ToolTip="最新のソースコードを反映します">
                    <StackPanel Orientation="Horizontal">
                        <TextBlock Text="🔄" FontSize="11" Margin="0,0,5,0" VerticalAlignment="Center"/>
                        <TextBlock Text="依存パッケージ再導入" FontSize="11" FontWeight="SemiBold" VerticalAlignment="Center"/>
                    </StackPanel>
                </Button>
            </Grid>
        </Grid>

        <!-- 3. Web画面クイックリンク -->
        <Border Grid.Row="3" Background="#111622" CornerRadius="8" Padding="14,9" Margin="0,0,0,14" BorderBrush="#1C2434" BorderThickness="1">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <TextBlock Grid.Column="0" Text="Web画面:" FontSize="12" FontWeight="SemiBold" Foreground="#64748B" VerticalAlignment="Center" Margin="0,0,14,0"/>
                <WrapPanel Grid.Column="1" Orientation="Horizontal">
                    <Button x:Name="BtnOpenMic" Background="#152033" BorderBrush="#2563EB" BorderThickness="1" Foreground="#60A5FA" Padding="11,5" FontSize="11.5" Margin="0,0,10,4">
                        <StackPanel Orientation="Horizontal">
                            <TextBlock Text="🎤" Margin="0,0,6,0" VerticalAlignment="Center"/>
                            <TextBlock Text="マイク入力 (/mic)" FontWeight="SemiBold" VerticalAlignment="Center"/>
                        </StackPanel>
                    </Button>
                    <Button x:Name="BtnOpenObs" Background="#152033" BorderBrush="#2563EB" BorderThickness="1" Foreground="#60A5FA" Padding="11,5" FontSize="11.5" Margin="0,0,10,4">
                        <StackPanel Orientation="Horizontal">
                            <TextBlock Text="📺" Margin="0,0,6,0" VerticalAlignment="Center"/>
                            <TextBlock Text="OBS字幕 (/)" FontWeight="SemiBold" VerticalAlignment="Center"/>
                        </StackPanel>
                    </Button>
                    <Button x:Name="BtnOpenDashboard" Background="#152033" BorderBrush="#2563EB" BorderThickness="1" Foreground="#60A5FA" Padding="11,5" FontSize="11.5" Margin="0,0,10,4">
                        <StackPanel Orientation="Horizontal">
                            <TextBlock Text="📊" Margin="0,0,6,0" VerticalAlignment="Center"/>
                            <TextBlock Text="ダッシュボード (/dashboard)" FontWeight="SemiBold" VerticalAlignment="Center"/>
                        </StackPanel>
                    </Button>
                    <Button x:Name="BtnOpenProfile" Background="#152033" BorderBrush="#2563EB" BorderThickness="1" Foreground="#60A5FA" Padding="11,5" FontSize="11.5" Margin="0,0,10,4">
                        <StackPanel Orientation="Horizontal">
                            <TextBlock Text="👤" Margin="0,0,6,0" VerticalAlignment="Center"/>
                            <TextBlock Text="話者プロファイル (/profile)" FontWeight="SemiBold" VerticalAlignment="Center"/>
                        </StackPanel>
                    </Button>
                    <Button x:Name="BtnOpenOverlay" Background="#152033" BorderBrush="#0284C7" BorderThickness="1" Foreground="#38BDF8" Padding="11,5" FontSize="11.5" Margin="0,0,10,4">
                        <StackPanel Orientation="Horizontal">
                            <TextBlock Text="🖥️" Margin="0,0,6,0" VerticalAlignment="Center"/>
                            <TextBlock Text="透過デスクトップ字幕" FontWeight="SemiBold" VerticalAlignment="Center"/>
                        </StackPanel>
                    </Button>
                </WrapPanel>
            </Grid>
        </Border>

        <!-- 4. ログコンソール (MinHeight 240px、タイトルとボタン群の絶対非重複グリッド) -->
        <Border Grid.Row="4" Background="#090D14" CornerRadius="8" BorderBrush="#1C2433" BorderThickness="1" MinHeight="240">
            <Grid>
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                </Grid.RowDefinitions>
                
                <!-- ログヘッダーバー -->
                <Border Grid.Row="0" Background="#0F1420" Padding="12,7" CornerRadius="8,8,0,0" BorderBrush="#1C2433" BorderThickness="0,0,0,1">
                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="Auto"/>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <StackPanel Grid.Column="0" Orientation="Horizontal" VerticalAlignment="Center">
                            <TextBlock Text="📋 実行ログ" FontSize="12" FontWeight="Bold" Foreground="#94A3B8" VerticalAlignment="Center"/>
                            <TextBlock Text=" (リアルタイム出力)" FontSize="10.5" Foreground="#64748B" Margin="6,1,0,0" VerticalAlignment="Center"/>
                        </StackPanel>
                        
                        <StackPanel Grid.Column="2" Orientation="Horizontal" VerticalAlignment="Center">
                            <Button x:Name="BtnOpenPythonLog" Background="#152033" BorderBrush="#23344D" BorderThickness="1" Foreground="#38BDF8" Padding="7,3" Margin="0,0,5,0" FontSize="11" ToolTip="Python実行ログ (logs/python.log) を開く">
                                <TextBlock Text="📄 python.log"/>
                            </Button>
                            <Button x:Name="BtnOpenContainerLog" Background="#152033" BorderBrush="#23344D" BorderThickness="1" Foreground="#38BDF8" Padding="7,3" Margin="0,0,5,0" FontSize="11" ToolTip="コンテナログ (logs/container.log) を開く">
                                <TextBlock Text="📦 container.log"/>
                            </Button>
                            <Button x:Name="BtnOpenLauncherLog" Background="#152033" BorderBrush="#23344D" BorderThickness="1" Foreground="#38BDF8" Padding="7,3" Margin="0,0,5,0" FontSize="11" ToolTip="ランチャーログ (logs/launcher.log) を開く">
                                <TextBlock Text="🚀 launcher.log"/>
                            </Button>
                            <Button x:Name="BtnOpenLogsDir" Background="#152033" BorderBrush="#23344D" BorderThickness="1" Foreground="#94A3B8" Padding="7,3" Margin="0,0,5,0" FontSize="11" ToolTip="logs フォルダをエクスプローラーで開く">
                                <TextBlock Text="📁 logsフォルダ"/>
                            </Button>
                            <Button x:Name="BtnClearLog" Background="#152033" BorderBrush="#23344D" BorderThickness="1" Foreground="#64748B" Padding="7,3" FontSize="11" ToolTip="画面上のログ表示をクリア">
                                <TextBlock Text="🧹 クリア"/>
                            </Button>
                        </StackPanel>
                    </Grid>
                </Border>
                
                <!-- ログ表示エリア -->
                <ScrollViewer Grid.Row="1" x:Name="LogScrollViewer" VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Auto" Margin="10,8">
                    <TextBox x:Name="LogTextBox"
                             Background="Transparent"
                             Foreground="#94A3B8"
                             BorderThickness="0"
                             IsReadOnly="True"
                             TextWrapping="Wrap"
                             FontFamily="{StaticResource MonoFont}"
                             FontSize="11.5"
                             CaretBrush="Transparent"/>
                </ScrollViewer>
            </Grid>
        </Border>
    </Grid>
</Window>
"@

$reader = [System.Xml.XmlNodeReader]::new($xaml)
$window = [System.Windows.Markup.XamlReader]::Load($reader)

# --- WPF UIスレッド未処理例外の安全捕捉 (プロセスクラッシュ完全防止) ---
$window.Dispatcher.Add_UnhandledExceptionFilter({
    param($eventSender, $e)
    try {
        $e.RequestCatch = $true
    } catch {}
})
$window.Dispatcher.Add_UnhandledException({
    param($eventSender, $e)
    try {
        Write-AppLog "UIエラーが発生しましたが回復しました: $($e.Exception.Message)" "ERROR"
        $e.Handled = $true
    } catch {}
})

# コントロール取得
$rbModeVenv = $window.FindName("RbModeVenv")
$rbModeWslc = $window.FindName("RbModeWslc")
$rbDeviceCpu = $window.FindName("RbDeviceCpu")
$rbDeviceNpu = $window.FindName("RbDeviceNpu")
$btnSetupEnv = $window.FindName("BtnSetupEnv")
$statusLabel = $window.FindName("StatusLabel")
$statusLed = $window.FindName("StatusLed")
$statusText = $window.FindName("StatusText")
$runtimeBadge = $window.FindName("RuntimeBadge")
$versionBadge = $window.FindName("VersionBadge")
if ($versionBadge) {
    $versionBadge.Text = $version
}
$window.Title = "Hayamimi Live (早耳 Live) ランチャー ($version)"
$btnStart = $window.FindName("BtnStart")
$btnStartText = $window.FindName("BtnStartText")
$btnRestart = $window.FindName("BtnRestart")
$btnRestartText = $window.FindName("BtnRestartText")
$btnStop = $window.FindName("BtnStop")
$btnStopText = $window.FindName("BtnStopText")
$btnUpdateApp = $window.FindName("BtnUpdateApp")
$btnUpdateAppText = $window.FindName("BtnUpdateAppText")
$btnUpdateAppIcon = $window.FindName("BtnUpdateAppIcon")
$updateDescText = $window.FindName("UpdateDescText")
$btnRebuild = $window.FindName("BtnRebuild")
$btnOpenMic = $window.FindName("BtnOpenMic")
$btnOpenObs = $window.FindName("BtnOpenObs")
$btnOpenDashboard = $window.FindName("BtnOpenDashboard")
$btnOpenProfile = $window.FindName("BtnOpenProfile")
$btnOpenOverlay = $window.FindName("BtnOpenOverlay")
$logTextBox = $window.FindName("LogTextBox")
$logScrollViewer = $window.FindName("LogScrollViewer")
$btnClearLog = $window.FindName("BtnClearLog")
$btnOpenPythonLog = $window.FindName("BtnOpenPythonLog")
$btnOpenContainerLog = $window.FindName("BtnOpenContainerLog")
$btnOpenLauncherLog = $window.FindName("BtnOpenLauncherLog")
$btnOpenLogsDir = $window.FindName("BtnOpenLogsDir")
$wslcAlertBanner = $window.FindName("WslcAlertBanner")
$wslcAlertTitle = $window.FindName("WslcAlertTitle")
$wslcAlertDesc = $window.FindName("WslcAlertDesc")
$btnUpdateWsl = $window.FindName("BtnUpdateWsl")
$btnBiosGuide = $window.FindName("BtnBiosGuide")
$gpuBadge = $window.FindName("GpuBadge")
$gpuBadgeBorder = $window.FindName("GpuBadgeBorder")
$initProgressBar = $window.FindName("InitProgressBar")
$btnInstallWslAlways = $window.FindName("BtnInstallWslAlways")
$updateDescText = $window.FindName("UpdateDescText")

# 実行基盤 & 推論デバイス状態変数
$script:CurrentEngineMode = "Venv" # "Venv" または "Wslc"
$script:CurrentDeviceMode = "Cpu"  # "Cpu" または "Gpu" (デフォルトは安全・省リソースな CPU)
$script:GpuShortName = ""
$script:ConfigFile = Join-Path $ProjectRoot "config\launcher_config.json"
$script:ProcPython = $null
$script:PythonLogFile = Join-Path $LogsDir "python.log"
$script:LastPythonLogLineCount = 0
$script:LastPythonLogByteOffset = 0L

# ランタイムバッジ
$psVer = $PSVersionTable.PSVersion.Major
if ($psVer -ge 7) {
    $runtimeBadge.Text = "PowerShell 7 (pwsh)"
} else {
    $runtimeBadge.Text = "PowerShell 5.1"
}

# ブラシ定義 (モダンカラーパレット)
$brushGray   = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#64748B")
$brushGreen  = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#10B981")
$brushOrange = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F59E0B")
$brushRed    = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#EF4444")
$brushBlue   = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#38BDF8")

$script:HasNvidiaGpu = $false
$script:HasNpu = $false
$script:NpuName = ""

# デバイスバッジ表示の更新 (選択中のモードと連動)
function Update-DeviceBadgeDisplay {
    if ($script:CurrentDeviceMode -eq "Npu" -and $script:HasNpu) {
        $name = if ($script:NpuName) { $script:NpuName } else { "NPU" }
        if ($gpuBadge) {
            $gpuBadge.Text = "⚡ NPU 起動: $name"
            $gpuBadge.Foreground = $brushGreen
        }
        if ($gpuBadgeBorder) {
            $gpuBadgeBorder.BorderBrush = $brushGreen
            $gpuBadgeBorder.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#0C241E")
        }
    } else {
        # CPU モード
        if ($gpuBadge) {
            $gpuBadge.Text = "⚙ CPU 起動 (ReazonSpeech)"
            $gpuBadge.Foreground = $brushBlue
        }
        if ($gpuBadgeBorder) {
            $gpuBadgeBorder.BorderBrush = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#0284C7")
            $gpuBadgeBorder.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#0F1E33")
        }
    }
}

# ハードウェア (NPU / メイン GPU) 状態の検出とラジオボタン・バッジ更新
function Update-GpuDeviceBadge {
    try {
        # 1. NPU (Neural Processing Unit) の検出
        # 注意: "Input" などの単語に "npu" が含まれる USB 入力デバイス (Logitech USB Input Device 等) を誤検知しないよう厳格に判定
        $npu = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | Where-Object {
            ($_.Name -match "AI Boost|\bNPU\b|\bIPU\b" -or $_.PNPClass -eq "ComputeAccelerator") -and
            $_.PNPClass -notmatch "Mouse|Keyboard|HIDClass|Media" -and
            $_.Name -notmatch "Input|Receiver|Mouse|Keyboard|Audio|Controller"
        } | Select-Object -First 1

        if ($npu -and $npu.Status -eq "OK") {
            $script:HasNpu = $true
            $script:NpuName = $npu.Name
            if ($rbDeviceNpu) {
                $rbDeviceNpu.IsEnabled = $true
                $rbDeviceNpu.Visibility = [System.Windows.Visibility]::Visible
                $rbDeviceNpu.Content = "⚡ NPU 起動"
                $rbDeviceNpu.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#F1F5F9")
                $rbDeviceNpu.Opacity = 1.0
                $rbDeviceNpu.Cursor = [System.Windows.Input.Cursors]::Hand
                $rbDeviceNpu.ToolTip = "NPU: $($npu.Name) (CPU・GPU負荷ゼロ)"
            }
            Write-AppLog "✔ NPU 検出: $($npu.Name) (ReazonSpeech NPU 高速推論対応 / CPU・GPU負荷ゼロ)" "INFO"
        } else {
            $script:HasNpu = $false
            $script:NpuName = ""
            if ($rbDeviceNpu) {
                $rbDeviceNpu.IsEnabled = $false
                # 非搭載時は暗いスレートグレー & 半透明(Opacity 0.35)で選択不能であることを明示
                $rbDeviceNpu.Content = "⚡ NPU 起動 (非搭載)"
                $rbDeviceNpu.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#475569")
                $rbDeviceNpu.Opacity = 0.35
                $rbDeviceNpu.Cursor = [System.Windows.Input.Cursors]::No
                $rbDeviceNpu.ToolTip = "システムに NPU が検出されませんでした (CPU 起動のみ利用可能です)"
            }
            if ($rbDeviceCpu) {
                $rbDeviceCpu.IsChecked = $true
            }
            $script:CurrentDeviceMode = "Cpu"
            Write-AppLog "ℹ NPU 未検出: CPU 起動モード (ReazonSpeech) で動作します" "INFO"
        }

        # 2. メイン GPU の検出と保護確認
        $gpus = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue
        $nvidiaGpu = $gpus | Where-Object { $_.Name -match "NVIDIA|GeForce|RTX" } | Select-Object -First 1
        if ($nvidiaGpu) {
            $script:HasNvidiaGpu = $true
            $script:GpuShortName = $nvidiaGpu.Name -replace "NVIDIA\s*", ""
            Write-AppLog "✔ メイン GPU 検出: $($nvidiaGpu.Name) (配信・ゲーム用に完全保護 / 負荷0%)" "INFO"
        }

        Update-DeviceBadgeDisplay
    } catch {
        $script:HasNpu = $false
        $script:NpuName = ""
        if ($rbDeviceNpu) {
            $rbDeviceNpu.IsEnabled = $false
            $rbDeviceNpu.Content = "⚡ NPU 起動 (非搭載)"
            $rbDeviceNpu.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#475569")
            $rbDeviceNpu.Opacity = 0.35
            $rbDeviceNpu.Cursor = [System.Windows.Input.Cursors]::No
            $rbDeviceNpu.ToolTip = "システムに NPU が検出されませんでした"
        }
        if ($rbDeviceCpu) {
            $rbDeviceCpu.IsChecked = $true
        }
        $script:CurrentDeviceMode = "Cpu"
    }
}

# ポート使用状況チェック関数 (競合事前検知)
function Test-PortInUse {
    param([int]$Port)
    try {
        $tcp = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $tcp.Start()
        $tcp.Stop()
        return $false
    } catch {
        return $true
    }
}

function Set-UIState {
    param([string]$State, [System.Windows.Media.Brush]$Brush, [bool]$CanStart, [bool]$CanStop, [object]$CanRestart = $null)
    $statusText.Text = $State
    $statusLed.Fill = $Brush
    $btnStart.IsEnabled = $CanStart
    $btnStop.IsEnabled = $CanStop
    if ($btnRestart) {
        if ($null -ne $CanRestart) {
            $btnRestart.IsEnabled = [bool]$CanRestart
        } else {
            $btnRestart.IsEnabled = ($CanStart -or $CanStop)
        }
    }
}

# 安全な確認ダイアログ表示関数 (Ownerウィンドウ明示 & 例外フォールバック)
function Show-AppConfirm {
    param(
        [string]$Message,
        [string]$Title = "確認 - Hayamimi Live",
        [System.Windows.MessageBoxButton]$Buttons = [System.Windows.MessageBoxButton]::YesNo,
        [System.Windows.MessageBoxImage]$Icon = [System.Windows.MessageBoxImage]::Question
    )
    try {
        if ($window -and $window.IsVisible) {
            return [System.Windows.MessageBox]::Show($window, $Message, $Title, $Buttons, $Icon)
        } else {
            return [System.Windows.MessageBox]::Show($Message, $Title, $Buttons, $Icon)
        }
    } catch {
        Write-AppLog "確認ダイアログの表示をスキップし、処理を続行します: $_" "WARN"
        return [System.Windows.MessageBoxResult]::Yes
    }
}

# 仮想化・Hypervisor 状態のインテリジェント診断 (Hyper-V稼働時のWMI False偽陽性を完全防止)
function Get-VirtualizationDiagnostic {
    $diag = [PSCustomObject]@{
        HypervisorPresent = $false
        FirmwareVirtualization = $null
        CpuName = ""
        CpuVendor = "Unknown"
        IsReady = $false
        Reason = ""
        Details = ""
    }

    # 1. CPU 情報およびベンダーの取得
    try {
        $proc = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($proc) {
            $diag.CpuName = "$($proc.Name)".Trim()
            $diag.FirmwareVirtualization = $proc.VirtualizationFirmwareEnabled
            if ($proc.Manufacturer -match "Intel" -or $proc.Name -match "Intel") {
                $diag.CpuVendor = "Intel"
            } elseif ($proc.Manufacturer -match "AMD" -or $proc.Name -match "AMD|Ryzen") {
                $diag.CpuVendor = "AMD"
            }
        }
    } catch {}

    # 2. Hypervisor の稼働確認
    # (※ Windows 10/11 で Hyper-V/VBS が動作している場合、Win32_Processor の VirtualizationFirmwareEnabled は
    #     WMI 仕様により必ず False または null になるため、HypervisorPresent が True の場合は CPU 仮想化は確実に有効である)
    try {
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
        if ($cs) {
            $diag.HypervisorPresent = [bool]$cs.HypervisorPresent
        }
    } catch {}

    # 3. WSL Containers / HCS スモークテスト最優先判定 (実動作確認)
    $testWslc = Get-WslcCommand
    if ($testWslc) {
        try {
            $pinfo = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfo.FileName = $testWslc
            $pinfo.Arguments = "images"
            $pinfo.RedirectStandardOutput = $true
            $pinfo.RedirectStandardError = $true
            $pinfo.UseShellExecute = $false
            $pinfo.CreateNoWindow = $true
            $procWslc = [System.Diagnostics.Process]::Start($pinfo)
            $stdout = $procWslc.StandardOutput.ReadToEnd()
            $stderr = $procWslc.StandardError.ReadToEnd()
            $procWslc.WaitForExit(2000) | Out-Null

            $combined = "$stdout $stderr"
            if ($procWslc.ExitCode -eq 0 -and $combined -notmatch "HCS_E_SERVICE_NOT_AVAILABLE|0x80370102|service is not available") {
                # wslc images が正常に動作するなら、ハードウェア仮想化も HCS も 100% 準備完了
                $diag.IsReady = $true
                return $diag
            }

            if ($combined -match "HCS_E_SERVICE_NOT_AVAILABLE|0x80370102|必要な機能がインストールされていない|service is not available") {
                $diag.Reason = "HcsServiceUnavailable"
                $diag.Details = "HCS_E_SERVICE_NOT_AVAILABLE"
                return $diag
            }
        } catch {}
    }

    # 4. 本当に BIOS で無効な場合のみ検出 (Hypervisor が動いておらず、かつ Firmware で無効と返された場合)
    if (-not $diag.HypervisorPresent -and $diag.FirmwareVirtualization -eq $false) {
        $diag.Reason = "BiosDisabled"
        return $diag
    }

    # 5. vmcompute サービス (Hyper-V Host Compute Service / VirtualMachinePlatform) の存在確認
    $hasVmCompute = (Test-Path "HKLM:\SYSTEM\CurrentControlSet\Services\vmcompute")
    if (-not $hasVmCompute) {
        $diag.Reason = "WindowsFeatureDisabled"
        return $diag
    }

    # 6. Hypervisor が稼働していれば Ready
    if ($diag.HypervisorPresent) {
        $diag.IsReady = $true
        return $diag
    }

    $diag.IsReady = $true
    return $diag
}

# BIOS / UEFI 仮想化有効化ガイドのポップアップ表示
function Show-BiosVirtualizationGuide {
    param([object]$Diag)
    $cpuVendor = if ($Diag -and $Diag.CpuVendor) { $Diag.CpuVendor } else { "Intel" }
    
    $settingName = if ($cpuVendor -eq "AMD") {
        "「SVM Mode」または「AMD-V / SVM」"
    } else {
        "「Intel Virtualization Technology (VT-x)」または「Intel (VMX)」"
    }

    $cpuDesc = if ($Diag -and $Diag.CpuName) { $Diag.CpuName } else { "お使いの CPU" }

    $guideMsg = "【CPU ハードウェア仮想化が無効です】`r`n`r`n" +
               "hayamimi (WSL Containers) を実行するには、PC の BIOS/UEFI 設定で仮想化支援機能を有効化（Enabled）にする必要があります。`r`n`r`n" +
               "■ 検出された CPU: $cpuDesc`r`n" +
               "■ 設定対象の項目名: $settingName`r`n`r`n" +
               "【有効化の手順】`r`n" +
               "1. PC を再起動し、起動直後にメーカーロゴが表示されたら [F2]、[F12]、または [Delete] キーを連打して BIOS/UEFI 画面を開きます。`r`n" +
               "2. 詳細設定 (Advanced / CPU Configuration / Overclocking) メニューを探します。`r`n" +
               "3. $settingName を見つけ、「有効 (Enabled)」に変更します。`r`n" +
               "4. [F10] キーを押して設定を保存し、PC を通常通り再起動してください。`r`n`r`n" +
               "※ BIOS 設定完了後、この警告は自動的に解消されます。"

    Show-AppConfirm -Message $guideMsg -Title "BIOS/UEFI 仮想化設定ガイド - hayamimi" -Buttons ([System.Windows.MessageBoxButton]::OK) -Icon ([System.Windows.MessageBoxImage]::Information) | Out-Null
}

# WSL3 & 仮想化アラートバナーの状態更新関数
function Update-WslcBannerState {
    $script:WslcCmd = Get-WslcCommand
    $script:VirtDiag = Get-VirtualizationDiagnostic

    if ($script:WslcCmd -and $script:VirtDiag.IsReady) {
        if ($wslcAlertBanner) { $wslcAlertBanner.Visibility = "Collapsed" }
        return $true
    }

    if ($wslcAlertBanner) {
        $wslcAlertBanner.Visibility = "Visible"
        if (-not $script:VirtDiag.IsReady -and $script:VirtDiag.Reason -eq "BiosDisabled") {
            if ($wslcAlertTitle) { $wslcAlertTitle.Text = "⚠ CPUの仮想化支援 (BIOS: VT-x / AMD-V) が無効です" }
            if ($wslcAlertDesc) { $wslcAlertDesc.Text = "BIOS/UEFI で $($script:VirtDiag.CpuVendor) 仮想化設定を有効化してください。未設定のままではコンテナを起動できません。" }
            if ($btnBiosGuide) { $btnBiosGuide.Visibility = "Visible" }
            if ($btnUpdateWsl) { $btnUpdateWsl.Content = "📥 仮想化 & WSL3 を自動設定" }
        } elseif (-not $script:VirtDiag.IsReady -and $script:VirtDiag.Reason -eq "HcsServiceUnavailable") {
            if ($wslcAlertTitle) { $wslcAlertTitle.Text = "⚠ 仮想化基盤 (HCS) が未準備です (PC再起動が必要です)" }
            if ($wslcAlertDesc) { $wslcAlertDesc.Text = "仮想化機能のインストール後に「PCの再起動」が行われていません。PCを再起動するか、修復を実行してください。" }
            if ($btnBiosGuide) { $btnBiosGuide.Visibility = "Visible" }
            if ($btnUpdateWsl) { $btnUpdateWsl.Content = "📥 仮想化基盤 & WSL3 を修復" }
        } elseif (-not $script:VirtDiag.IsReady -and $script:VirtDiag.Reason -eq "WindowsFeatureDisabled") {
            if ($wslcAlertTitle) { $wslcAlertTitle.Text = "⚠ Windows の仮想化基盤 (VirtualMachinePlatform) が無効です" }
            if ($wslcAlertDesc) { $wslcAlertDesc.Text = "WSL Containers の実行には Windows の仮想マシンプラットフォームが必要です。ワンクリックで自動有効化できます。" }
            if ($btnBiosGuide) { $btnBiosGuide.Visibility = "Collapsed" }
            if ($btnUpdateWsl) { $btnUpdateWsl.Content = "📥 仮想化基盤 & WSL3 を有効化" }
        } else {
            if ($wslcAlertTitle) { $wslcAlertTitle.Text = "⚠ WSL 3.0 (WSL Containers) がインストールされていません" }
            if ($wslcAlertDesc) { $wslcAlertDesc.Text = "hayamimi の実行に必要な WSL 3.0 をワンクリックで自動インストールできます。" }
            if ($btnBiosGuide) { $btnBiosGuide.Visibility = "Collapsed" }
            if ($btnUpdateWsl) { $btnUpdateWsl.Content = "📥 WSL3 を自動インストール" }
        }
    }
    return $false
}

# 設定ファイル (config/launcher_config.json) の読み込み
function Get-SavedLauncherConfig {
    if (Test-Path $script:ConfigFile) {
        try {
            $content = Get-Content -Path $script:ConfigFile -Raw -Encoding UTF8
            if ($content -and $content.Trim()) {
                return $content | ConvertFrom-Json
            }
        } catch {}
    }
    # バックアップファイル (.bak) からの自動自己修復
    $bakFile = $script:ConfigFile + ".bak"
    if (Test-Path $bakFile) {
        try {
            $bakContent = Get-Content -Path $bakFile -Raw -Encoding UTF8
            if ($bakContent -and $bakContent.Trim()) {
                $res = $bakContent | ConvertFrom-Json
                Copy-Item -Path $bakFile -Destination $script:ConfigFile -Force -ErrorAction SilentlyContinue
                return $res
            }
        } catch {}
    }
    return $null
}

# 実行基盤設定の読み込み
function Get-SavedEngineMode {
    $cfg = Get-SavedLauncherConfig
    if ($cfg -and $cfg.engine_mode -in @("Venv", "Wslc")) {
        return $cfg.engine_mode
    }
    return $null
}

# 演算デバイス設定の読み込み (デフォルト: CPU)
function Get-SavedDeviceMode {
    $cfg = Get-SavedLauncherConfig
    if ($cfg -and $cfg.device_mode -in @("Cpu", "Gpu")) {
        return $cfg.device_mode
    }
    return "Cpu" # デフォルトは最も安全・軽量な CPU 起動
}

# ランチャー設定 (実行基盤 & 演算デバイス) のアトミック永続化保存
function Save-LauncherConfig {
    param(
        [string]$EngineMode = $script:CurrentEngineMode,
        [string]$DeviceMode = $script:CurrentDeviceMode
    )
    if ($script:IsInitializing) { return }
    try {
        $configDir = Join-Path $ProjectRoot "config"
        if (-not (Test-Path $configDir)) {
            New-Item -ItemType Directory -Path $configDir -Force | Out-Null
        }
        $existing = Get-SavedLauncherConfig
        $obj = [ordered]@{}
        if ($existing) {
            foreach ($prop in $existing.PSObject.Properties) {
                $obj[$prop.Name] = $prop.Value
            }
        }
        $obj["engine_mode"] = if ($EngineMode) { $EngineMode } else { "Venv" }
        $obj["device_mode"] = if ($DeviceMode) { $DeviceMode } else { "Cpu" }
        $json = $obj | ConvertTo-Json -Depth 4

        # 既存正常ファイルの .bak 作成
        if ((Test-Path $script:ConfigFile) -and (Get-Item $script:ConfigFile).Length -gt 0) {
            Copy-Item -Path $script:ConfigFile -Destination ($script:ConfigFile + ".bak") -Force -ErrorAction SilentlyContinue
        }

        # 一時ファイルへのアトミック書き込み
        $tmpFile = [System.IO.Path]::Combine($configDir, ".tmp_launcher_config_" + [System.Guid]::NewGuid().ToString("N") + ".json")
        [System.IO.File]::WriteAllText($tmpFile, $json, [System.Text.Encoding]::UTF8)
        Move-Item -Path $tmpFile -Destination $script:ConfigFile -Force
        Write-AppLog "ランチャー設定を保存しました: engine_mode=$($obj['engine_mode']), device_mode=$($obj['device_mode'])" "DEBUG"
    } catch {}
}

function Save-EngineMode {
    param([string]$Mode)
    Save-LauncherConfig -EngineMode $Mode
}

function Save-DeviceMode {
    param([string]$Mode)
    Save-LauncherConfig -DeviceMode $Mode
}

# 実行基盤の初期モード判定 (保存設定優先 & デフォルトは Python venv)
function Get-InitialEngineMode {
    $saved = Get-SavedEngineMode
    if ($saved) { return $saved }

    # 既定値: Python venv (ネイティブ実行 / 再起動不要・推奨)
    # ※ WSL Containers はユーザーが明示的にラジオボタンで切り替えた場合にのみ選択される
    return "Venv"
}

# 実行基盤に応じたアラートバナー状態更新関数
function Update-EngineBannerState {
    if ($script:CurrentEngineMode -eq "Venv") {
        # Python venv モード
        $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
        $hasVenv = (Test-Path $venvPython)

        if ($hasVenv) {
            if ($wslcAlertBanner) { $wslcAlertBanner.Visibility = "Collapsed" }
            if ($btnSetupEnv) { $btnSetupEnv.Visibility = "Collapsed" }
            $runtimeBadge.Text = "Python venv"
            $runtimeBadge.Foreground = $brushGreen
            return $true
        } else {
            if ($wslcAlertBanner) {
                $wslcAlertBanner.Visibility = "Visible"
                $wslcAlertTitle.Text = "⚠ Python 仮想環境 (.venv) が未構築です"
                $wslcAlertDesc.Text = "hayamimi の実行に必要な Python 仮想環境をワンクリックで自動構築できます (PC再起動不要)。"
                if ($btnBiosGuide) { $btnBiosGuide.Visibility = "Collapsed" }
                if ($btnUpdateWsl) {
                    $btnUpdateWsl.Content = "📥 Python 環境を自動構築"
                    $btnUpdateWsl.Visibility = "Visible"
                }
            }
            if ($btnSetupEnv) {
                # 重複防止のためセレクター行のボタンは完全非表示
                $btnSetupEnv.Visibility = "Collapsed"
            }
            $runtimeBadge.Text = "Python (未構築)"
            $runtimeBadge.Foreground = $brushOrange
            return $false
        }
    } else {
        # WSL Containers モード
        $isReady = Update-WslcBannerState
        if ($isReady) {
            $runtimeBadge.Text = "WSL3 Native"
            $runtimeBadge.Foreground = $brushGreen
        }
        if ($btnSetupEnv) { $btnSetupEnv.Visibility = "Collapsed" }
        return $isReady
    }
}

# 実行基盤モードの切り替え関数
function Set-EngineMode {
    param([string]$NewMode)
    $script:CurrentEngineMode = $NewMode
    Save-EngineMode -Mode $NewMode

    if ($NewMode -eq "Venv") {
        Write-AppLog "実行基盤を「Python venv (ネイティブ)」に切り替えました。" "INFO"
        if ($statusLabel) { $statusLabel.Text = "Python状態" }
        if ($btnStartText) { $btnStartText.Text = "アプリ起動 & Webを開く" }
        if ($btnRestartText) { $btnRestartText.Text = "アプリ再起動" }
        if ($btnStopText) { $btnStopText.Text = "アプリ終了 & 閉じる" }
        if ($btnRebuild) { $btnRebuild.Content = "🔄 依存パッケージ再導入" }
        if ($updateDescText) { $updateDescText.Text = "※ GitHubの最新コード取得と依存パッケージ更新を行えます。" }
    } else {
        Write-AppLog "実行基盤を「WSL Containers (WSL3)」に切り替えました。" "INFO"
        if ($statusLabel) { $statusLabel.Text = "コンテナ状態" }
        if ($btnStartText) { $btnStartText.Text = "コンテナ起動 & Webを開く" }
        if ($btnRestartText) { $btnRestartText.Text = "コンテナ再起動" }
        if ($btnStopText) { $btnStopText.Text = "コンテナ終了 & 閉じる" }
        if ($btnRebuild) { $btnRebuild.Content = "🔄 コンテナをリビルド" }
        if ($updateDescText) { $updateDescText.Text = "※ GitHubの最新コード取得とコンテナ更新を行えます。" }
    }

    # 稼働状態の反映
    $isRunning = Test-AppRunning
    if ($isRunning) {
        Set-UIState "稼働中 (8833 / 8766)" $brushGreen $false $true
    } else {
        Set-UIState "停止中" $brushGray $true $false
    }
    Update-EngineBannerState | Out-Null
}

# 推論デバイスモードの切り替え関数
function Set-DeviceMode {
    param([string]$NewMode)
    $script:CurrentDeviceMode = $NewMode
    Save-DeviceMode -Mode $NewMode
    Update-DeviceBadgeDisplay

    if ($NewMode -eq "Npu") {
        Write-AppLog "推論設定を「⚡ NPU 起動」に切り替えました。※NPU活用・CPU負荷ゼロ" "INFO"
    } else {
        Write-AppLog "推論設定を「⚙ CPU 起動」に切り替えました。" "INFO"
    }
}

# 推論デバイス切り替え時の安全ハンドラ (稼働中プロセス停止確認付き)
$script:IsSwitchingDevice = $false
function Invoke-DeviceModeSwitch {
    param(
        [string]$TargetMode,
        [System.Windows.Controls.RadioButton]$TargetRb,
        [System.Windows.Controls.RadioButton]$OtherRb
    )
    if ($script:IsSwitchingDevice) { return }
    if ($script:CurrentDeviceMode -eq $TargetMode) { return }

    if (Test-AppRunning) {
        $curDevName = if ($script:CurrentDeviceMode -eq "Npu") { "NPU 起動" } else { "CPU 起動" }
        $targetDevName = if ($TargetMode -eq "Npu") { "NPU 起動" } else { "CPU 起動" }
        $ask = Show-AppConfirm -Message "現在アプリが稼働中です。`r`n`r`n推論設定を「$targetDevName」に切り替えるには、現在のプロセスを再起動する必要があります。`r`n`r`n稼働中のプロセスを停止して切り替えますか？" -Title "推論設定の切り替え確認 - Hayamimi Live"
        if ($ask -eq [System.Windows.MessageBoxResult]::Yes) {
            $script:IsSwitchingDevice = $true
            if ($script:CurrentEngineMode -eq "Venv") {
                Stop-PythonVenvApp
            } else {
                if ($script:WslcCmd) {
                    try {
                        Start-Process $script:WslcCmd -ArgumentList "stop $ContainerName" -Wait -NoNewWindow
                        Start-Process $script:WslcCmd -ArgumentList "remove $ContainerName" -Wait -NoNewWindow
                    } catch {}
                }
            }
            Set-DeviceMode $TargetMode
            $script:IsSwitchingDevice = $false
        } else {
            # キャンセル時は元の選択に戻す
            $script:IsSwitchingDevice = $true
            $OtherRb.IsChecked = $true
            $script:IsSwitchingDevice = $false
            return
        }
    } else {
        Set-DeviceMode $TargetMode
    }
}

# 実行基盤切り替え時の安全ハンドラ (稼働中プロセス停止確認付き)
$script:IsSwitchingMode = $false
function Invoke-EngineModeSwitch {
    param(
        [string]$TargetMode,
        [System.Windows.Controls.RadioButton]$TargetRb,
        [System.Windows.Controls.RadioButton]$OtherRb
    )
    if ($script:IsSwitchingMode) { return }
    if ($script:CurrentEngineMode -eq $TargetMode) { return }

    if (Test-AppRunning) {
        $curModeName = if ($script:CurrentEngineMode -eq "Venv") { "Python venv" } else { "WSL Containers" }
        $targetModeName = if ($TargetMode -eq "Venv") { "Python venv" } else { "WSL Containers" }
        $ask = Show-AppConfirm -Message "現在 $curModeName が稼働中です。`r`n`r`n実行基盤を $targetModeName に切り替えるには、現在のプロセスを停止する必要があります。`r`n`r`n稼働中のプロセスを停止して切り替えますか？" -Title "実行基盤の切り替え確認 - Hayamimi Live"
        if ($ask -eq [System.Windows.MessageBoxResult]::Yes) {
            $script:IsSwitchingMode = $true
            if ($script:CurrentEngineMode -eq "Venv") {
                Stop-PythonVenvApp
            } else {
                if ($script:WslcCmd) {
                    try {
                        Start-Process $script:WslcCmd -ArgumentList "stop $ContainerName" -Wait -NoNewWindow
                        Start-Process $script:WslcCmd -ArgumentList "remove $ContainerName" -Wait -NoNewWindow
                    } catch {}
                }
            }
            Set-EngineMode $TargetMode
            $script:IsSwitchingMode = $false
        } else {
            # キャンセル時は元の選択に戻す
            $script:IsSwitchingMode = $true
            $OtherRb.IsChecked = $true
            $script:IsSwitchingMode = $false
            return
        }
    } else {
        Set-EngineMode $TargetMode
    }
}

# ファイル共有モード (ReadWrite) での安全なログ行読み取り関数 (ロック競合ゼロ)
function Read-FileSharedLines {
    param([string]$FilePath)
    $lines = @()
    if (-not (Test-Path $FilePath)) { return $lines }
    try {
        $fs = [System.IO.FileStream]::new($FilePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $sr = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8)
        $content = $sr.ReadToEnd()
        $sr.Close()
        $fs.Close()
        if ($content) {
            $lines = $content -split "`r?`n"
        }
    } catch {}
    return $lines
}

# Python ログ更新関数 (logs/python.log から新着ログを差分読み取りして GUI コンソールに反映)
function Update-PythonLogFile {
    param([bool]$EchoToGui = $true)
    if (-not (Test-Path $script:PythonLogFile)) { return }
    try {
        $fileInfo = [System.IO.FileInfo]::new($script:PythonLogFile)
        $fileLen = $fileInfo.Length
        if ($fileLen -lt $script:LastPythonLogByteOffset) {
            # ログファイルが再作成された場合
            $script:LastPythonLogByteOffset = 0L
        }
        if ($fileLen -eq $script:LastPythonLogByteOffset) { return }

        $fs = [System.IO.FileStream]::new($script:PythonLogFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        [void]$fs.Seek($script:LastPythonLogByteOffset, [System.IO.SeekOrigin]::Begin)
        $sr = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8)
        $newContent = $sr.ReadToEnd()
        $script:LastPythonLogByteOffset = $fs.Position
        $sr.Close()
        $fs.Close()

        if ($newContent -and $EchoToGui -and $logTextBox) {
            $lines = $newContent -split "`r?`n"
            $newLines = [System.Collections.Generic.List[string]]::new()
            foreach ($item in $lines) {
                if ($item -and $item.Trim() -and $item -notmatch "^#") {
                    $newLines.Add("[Python] $($item.Trim())")
                }
            }
            if ($newLines.Count -gt 0) {
                Write-AppLogBatch -Messages $newLines -Level "PYTHON"
            }
        }
    } catch {}
}

# コンテナ稼働確認関数 (wslc inspect優先 & wslc list多言語フォールバック)
function Test-ContainerRunning {
    param([string]$TargetName = $ContainerName)
    if (-not $script:WslcCmd) { return $false }
    try {
        # 1. wslc inspect による高精度な生存判定 (JSON の "Running": true を直接判定)
        $pinfoInspect = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfoInspect.FileName = $script:WslcCmd
        $pinfoInspect.Arguments = "inspect $TargetName"
        $pinfoInspect.RedirectStandardOutput = $true
        $pinfoInspect.RedirectStandardError = $true
        $pinfoInspect.UseShellExecute = $false
        $pinfoInspect.CreateNoWindow = $true
        $procInspect = [System.Diagnostics.Process]::Start($pinfoInspect)
        $inspectOut = $procInspect.StandardOutput.ReadToEnd()
        $procInspect.WaitForExit(1500) | Out-Null

        if ($inspectOut) {
            if ($inspectOut -match '"Running"\s*:\s*true' -or $inspectOut -match '"Status"\s*:\s*"running"') {
                return $true
            }
        }

        # 2. wslc list によるフォールバック判定 (多言語・状態柔軟マッチング)
        $pinfoList = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfoList.FileName = $script:WslcCmd
        $pinfoList.Arguments = "list"
        $pinfoList.RedirectStandardOutput = $true
        $pinfoList.RedirectStandardError = $true
        $pinfoList.UseShellExecute = $false
        $pinfoList.CreateNoWindow = $true
        $procList = [System.Diagnostics.Process]::Start($pinfoList)
        $listOut = $procList.StandardOutput.ReadToEnd()
        $procList.WaitForExit(1500) | Out-Null

        if ($listOut) {
            $lines = $listOut -split "[\r\n]+"
            foreach ($line in $lines) {
                if ($line -match "\b$TargetName\b" -and ($line -match "running|Up|実行中|作成済み|created")) {
                    return $true
                }
            }
        }
    } catch {}
    return $false
}

# コンテナイメージ存在確認関数
function Test-HayamimiImageExists {
    if (-not $script:WslcCmd) { return $false }
    try {
        $pinfo = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfo.FileName = $script:WslcCmd
        $pinfo.Arguments = "images"
        $pinfo.RedirectStandardOutput = $true
        $pinfo.UseShellExecute = $false
        $pinfo.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($pinfo)
        $raw = $proc.StandardOutput.ReadToEnd()
        $proc.WaitForExit()
        return ($raw -match "hayamimi")
    } catch {
        return $false
    }
}

# Python プロセス稼働確認関数
function Test-PythonRunning {
    try {
        $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*realtime_transcribe.py*" }
        return ($null -ne $procs -and $procs.Count -gt 0)
    } catch {
        return $false
    }
}

# 統合アプリアプリ稼働確認関数 (モード連動)
function Test-AppRunning {
    if ($script:CurrentEngineMode -eq "Venv") {
        return (Test-PythonRunning)
    } else {
        return (Test-ContainerRunning)
    }
}

# Python 仮想環境のバージョン取得関数
function Get-PythonVenvVersion {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) { return $null }
    try {
        $pinfo = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfo.FileName = $venvPython
        $pinfo.Arguments = "-c `"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')`""
        $pinfo.WorkingDirectory = $ProjectRoot
        $pinfo.RedirectStandardOutput = $true
        $pinfo.UseShellExecute = $false
        $pinfo.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($pinfo)
        $verStr = $proc.StandardOutput.ReadToEnd().Trim()
        $proc.WaitForExit(3000) | Out-Null
        if ($verStr -match '^\d+\.\d+') {
            return $verStr
        }
    } catch {}
    return $null
}

# Python 3.14 環境の最新性確認・自動再構築関数 (3.11 等の旧環境は自動で 3.14 で作り直す)
function Ensure-PythonVenvUpToDate {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        return (Invoke-SetupPythonVenv)
    }

    $curVer = Get-PythonVenvVersion
    if ($curVer -and ($curVer -ne "3.14")) {
        Write-AppLog "=========================================="
        Write-AppLog "【環境更新】旧バージョン (Python $curVer) の仮想環境を検出しました。" "WARN"
        Write-AppLog "hayamimi を最新の Python 3.14 実行環境へ自動アップグレードするため、仮想環境 (.venv) を再構築します..." "INFO"
        Set-UIState "Python 3.14更新中..." $brushOrange $false $false
        return (Invoke-SetupPythonVenv -ForceRecreate $true)
    }
    return $true
}

# Python venv (ネイティブ実行環境) 自動セットアップ関数 (UIフリーズ・応答なし完全防止)
function Invoke-SetupPythonVenv {
    param([bool]$ForceRecreate = $false)

    Write-AppLog "=========================================="
    Write-AppLog "Python venv (ネイティブ実行環境) のセットアップを開始します..." "INFO"
    Set-UIState "Python環境構築中..." $brushOrange $false $false
    if ($initProgressBar) { $initProgressBar.Visibility = "Visible" }

    # 1. uv の準備 (スマートアプリコントロール SAC 対応 & 自動フォールバック付き)
    $uvCmd = Get-SafeUvCommand
    $localUvDir = Join-Path $ProjectRoot "tools\uv"
    $localUvExe = Join-Path $localUvDir "uv.exe"

    if (-not $uvCmd -and -not $script:UvBlockedBySac) {
        Write-AppLog "高速パッケージツール (uv) を取得中 (GitHub Releases)..." "INFO"
        try {
            if (-not (Test-Path $localUvDir)) {
                New-Item -ItemType Directory -Path $localUvDir -Force | Out-Null
            }
            $uvZip = Join-Path $env:TEMP "uv-windows-x64.zip"
            $uvUrl = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
            
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
            
            # WebClient による非同期ダウンロード & UIメッセージループ維持
            $wc = [System.Net.WebClient]::new()
            $wc.DownloadFileAsync([Uri]$uvUrl, $uvZip)
            while ($wc.IsBusy) {
                try {
                    [System.Windows.Threading.Dispatcher]::CurrentDispatcher.Invoke([Action]{}, [System.Windows.Threading.DispatcherPriority]::Background)
                } catch {}
                Start-Sleep -Milliseconds 40
            }
            $wc.Dispose()
            
            try { Unblock-File -LiteralPath $uvZip -ErrorAction SilentlyContinue } catch {}
            Expand-Archive -Path $uvZip -DestinationPath $localUvDir -Force
            Remove-Item -Path $uvZip -Force -ErrorAction SilentlyContinue

            if (Test-Path $localUvExe) {
                try { Unblock-File -LiteralPath $localUvExe -ErrorAction SilentlyContinue } catch {}
                $uvCmd = Get-SafeUvCommand
                if ($uvCmd) {
                    Write-AppLog "✔ uv ツールの配置が完了しました: $uvCmd" "INFO"
                }
            }
        } catch {
            Write-AppLog "uv のダウンロードに失敗しました (標準 Python pip を使用します): $_" "WARN"
        }
    }

    # 2. 仮想環境 .venv の作成
    $venvDir = Join-Path $ProjectRoot ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if ($ForceRecreate -and (Test-Path $venvDir)) {
        Write-AppLog "既存の仮想環境 (.venv) をクリーンアップしています..." "INFO"
        try {
            Remove-Item -Path $venvDir -Recurse -Force -ErrorAction Stop
        } catch {
            Write-AppLog "仮想環境フォルダの削除警告: $_" "WARN"
        }
    }

    if ($ForceRecreate -or -not (Test-Path $venvPython)) {
        Write-AppLog "Python 3.14 仮想環境 (.venv) を作成しています..." "INFO"
        $venvCreated = $false
        if ($uvCmd) {
            try {
                $pinfoVenv = [System.Diagnostics.ProcessStartInfo]::new()
                $pinfoVenv.FileName = $uvCmd
                $pinfoVenv.Arguments = "venv .venv --python 3.14"
                $pinfoVenv.WorkingDirectory = $ProjectRoot
                $pinfoVenv.RedirectStandardOutput = $true
                $pinfoVenv.RedirectStandardError = $true
                $pinfoVenv.UseShellExecute = $false
                $pinfoVenv.CreateNoWindow = $true
                $procVenv = [System.Diagnostics.Process]::Start($pinfoVenv)
                Wait-ProcessWithUiPump -Proc $procVenv -LogPrefix "[venv作成]"
                
                if ($procVenv.ExitCode -eq 0 -and (Test-Path $venvPython)) {
                    Write-AppLog "✔ Python 3.14 仮想環境 (.venv) を作成しました。" "INFO"
                    $venvCreated = $true
                }
            } catch {
                Write-AppLog "uv による venv 作成がブロックされたため、標準 Python にフォールバックします: $_" "WARN"
                $uvCmd = $null
            }
        }
        if (-not $venvCreated) {
            $sysPython = (where.exe python 2>$null | Select-Object -First 1)
            if ($sysPython) {
                try {
                    $pinfoSys = [System.Diagnostics.ProcessStartInfo]::new()
                    $pinfoSys.FileName = $sysPython
                    $pinfoSys.Arguments = "-m venv .venv"
                    $pinfoSys.WorkingDirectory = $ProjectRoot
                    $pinfoSys.RedirectStandardOutput = $true
                    $pinfoSys.RedirectStandardError = $true
                    $pinfoSys.UseShellExecute = $false
                    $pinfoSys.CreateNoWindow = $true
                    $procSys = [System.Diagnostics.Process]::Start($pinfoSys)
                    Wait-ProcessWithUiPump -Proc $procSys -LogPrefix "[venv作成]"
                    if ($procSys.ExitCode -eq 0 -and (Test-Path $venvPython)) {
                        $venvCreated = $true
                    }
                } catch {
                    Write-AppLog "Python による venv 作成エラー: $_" "ERROR"
                }
            }
            if (-not $venvCreated) {
                Write-AppLog "Python 仮想環境の作成に失敗しました。" "ERROR"
                Set-UIState "セットアップ失敗" $brushRed $true $false
                if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
                return $false
            }
        }
    }

    # 3. 依存ライブラリのインストール
    Write-AppLog "依存パッケージ (requirements.txt) をインストールしています (十数秒)..." "INFO"
    $reqFile = Join-Path $ProjectRoot "requirements.txt"
    if (Test-Path $reqFile) {
        $installSuccess = $false
        if ($uvCmd) {
            try {
                $pinfoPip = [System.Diagnostics.ProcessStartInfo]::new()
                $pinfoPip.FileName = $uvCmd
                $pinfoPip.Arguments = "pip install -r `"$reqFile`" --python `"$venvPython`""
                $pinfoPip.WorkingDirectory = $ProjectRoot
                $pinfoPip.RedirectStandardOutput = $true
                $pinfoPip.RedirectStandardError = $true
                $pinfoPip.UseShellExecute = $false
                $pinfoPip.CreateNoWindow = $true
                $pinfoPip.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
                $procPip = [System.Diagnostics.Process]::Start($pinfoPip)
                Wait-ProcessWithUiPump -Proc $procPip -LogPrefix "[pip導入]"
                if ($procPip.ExitCode -eq 0) { $installSuccess = $true }
            } catch {
                Write-AppLog "uv pip がブロックされたため、標準 pip にフォールバックします: $_" "WARN"
                $uvCmd = $null
            }
        }
        if (-not $installSuccess -and (Test-Path $venvPython)) {
            try {
                Unblock-File -LiteralPath $venvPython -ErrorAction SilentlyContinue
                $pinfoPipSys = [System.Diagnostics.ProcessStartInfo]::new()
                $pinfoPipSys.FileName = $venvPython
                $pinfoPipSys.Arguments = "-B -m pip install -r `"$reqFile`""
                $pinfoPipSys.WorkingDirectory = $ProjectRoot
                $pinfoPipSys.RedirectStandardOutput = $true
                $pinfoPipSys.RedirectStandardError = $true
                $pinfoPipSys.UseShellExecute = $false
                $pinfoPipSys.CreateNoWindow = $true
                $pinfoPipSys.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
                $procPipSys = [System.Diagnostics.Process]::Start($pinfoPipSys)
                Wait-ProcessWithUiPump -Proc $procPipSys -LogPrefix "[pip導入]"
                if ($procPipSys.ExitCode -eq 0) { $installSuccess = $true }
            } catch {
                Write-AppLog "pip の起動に失敗しました: $_" "ERROR"
            }
        }

        if (-not $installSuccess) {
            Write-AppLog "パッケージのインストールに失敗しました。" "ERROR"
            Set-UIState "インストール失敗" $brushRed $true $false
            if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
            return $false
        }
        Unblock-ProjectFiles -RootDir $ProjectRoot
        Write-AppLog "✔ 依存パッケージのインストールが完了しました！" "INFO"
    }

    # 4. AIモデルの実体検証 & セルフヒーリング
    Ensure-ModelsIntegrity | Out-Null

    Write-AppLog "✔ Python venv 環境のセットアップがすべて完了しました！" "INFO"
    Write-AppLog "=========================================="
    Set-UIState "停止中" $brushGray $true $false
    if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
    Update-EngineBannerState | Out-Null
    return $true
}

# Python venv アプリの起動処理
function Start-PythonVenvApp {
    # 仮想環境の存在および Python 3.14 への整合性確認 (3.11 環境は自動再構築)
    $isOk = Ensure-PythonVenvUpToDate
    if (-not $isOk) {
        Write-AppLog "Python 仮想環境の準備に失敗したため起動を中止しました。" "ERROR"
        Set-UIState "セットアップ失敗" $brushRed $true $false
        return
    }

    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-AppLog "Python 仮想環境 (.venv) が見つかりません。先にセットアップを行ってください。" "WARN"
        return
    }

    # NPU 起動時の依存パッケージ整合性チェック & 自動補完 (過去ユーザーのシームレスアップデート対応)
    if ($script:CurrentDeviceMode -eq "Npu" -and $script:HasNpu) {
        $hasOpenVino = & "$venvPython" -B -c "import openvino" 2>$null; $?
        if (-not $hasOpenVino) {
            Write-AppLog "==========================================" "INFO"
            Write-AppLog "【NPU初回準備】NPU推論に必要な OpenVINO ライブラリを自動配備しています (数秒で完了します)..." "INFO"
            Set-UIState "NPU準備中..." $brushOrange $false $false
            if ($initProgressBar) { $initProgressBar.Visibility = "Visible" }

            $uvCmd = Get-SafeUvCommand
            $installed = $false
            if ($uvCmd) {
                try {
                    $pinfoPip = [System.Diagnostics.ProcessStartInfo]::new()
                    $pinfoPip.FileName = $uvCmd
                    $pinfoPip.Arguments = "pip install --python `"$venvPython`" openvino>=2026.0.0"
                    $pinfoPip.WorkingDirectory = $ProjectRoot
                    $pinfoPip.RedirectStandardOutput = $true
                    $pinfoPip.RedirectStandardError = $true
                    $pinfoPip.UseShellExecute = $false
                    $pinfoPip.CreateNoWindow = $true
                    $procPip = [System.Diagnostics.Process]::Start($pinfoPip)
                    Wait-ProcessWithUiPump -Proc $procPip -LogPrefix "[NPU導入]"
                    if ($procPip.ExitCode -eq 0) { $installed = $true }
                } catch {}
            }
            if (-not $installed) {
                try {
                    $pinfoPipSys = [System.Diagnostics.ProcessStartInfo]::new()
                    $pinfoPipSys.FileName = $venvPython
                    $pinfoPipSys.Arguments = "-m pip install openvino>=2026.0.0"
                    $pinfoPipSys.WorkingDirectory = $ProjectRoot
                    $pinfoPipSys.RedirectStandardOutput = $true
                    $pinfoPipSys.RedirectStandardError = $true
                    $pinfoPipSys.UseShellExecute = $false
                    $pinfoPipSys.CreateNoWindow = $true
                    $procPipSys = [System.Diagnostics.Process]::Start($pinfoPipSys)
                    Wait-ProcessWithUiPump -Proc $procPipSys -LogPrefix "[NPU導入]"
                    if ($procPipSys.ExitCode -eq 0) { $installed = $true }
                } catch {}
            }
            if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
            if ($installed) {
                Write-AppLog "✔ OpenVINO の自動導入が完了しました！NPU 起動を続行します。" "INFO"
            } else {
                Write-AppLog "⚠ OpenVINO の導入に失敗したため、安全な CPU 起動モードにフォールバックします。" "WARN"
                $script:CurrentDeviceMode = "Cpu"
                if ($rbDeviceCpu) { $rbDeviceCpu.IsChecked = $true }
            }
        }
    }

    # 必須モデル存在事前チェック & セルフヒーリング
    $hasModels = Ensure-ModelsIntegrity
    if (-not $hasModels) {
        Set-UIState "モデル取得失敗" $brushRed $true $false
        return
    }

    # 既存プロセス停止確認
    Stop-PythonVenvApp -Silent $true

    # ポート競合事前チェック
    if (Test-PortInUse 8833) {
        Write-AppLog "【警告】HTTPポート 8833 が既に使用されています。既存プロセスまたはコンテナが稼働していないか確認してください。" "WARN"
        $askPort = Show-AppConfirm -Message "HTTPポート 8833 が別プロセスで使用されています。`r`n`r`nポート競合により起動に失敗する可能性があります。そのまま起動を試みますか？" -Title "ポート競合の確認 - hayamimi"
        if ($askPort -ne [System.Windows.MessageBoxResult]::Yes) {
            Set-UIState "停止中" $brushGray $true $false
            return
        }
    }

    Set-UIState "起動中..." $brushOrange $false $false
    if ($initProgressBar) { $initProgressBar.Visibility = "Visible" }
    Write-AppLog "=========================================="
    Write-AppLog "Python venv モードで hayamimi を起動しています..." "INFO"

    # ログファイルの初期化
    if (Test-Path $script:PythonLogFile) {
        Remove-Item -Path $script:PythonLogFile -Force -ErrorAction SilentlyContinue
    }
    $utf8Bom = [byte[]]@(0xEF, 0xBB, 0xBF)
    [System.IO.File]::WriteAllBytes($script:PythonLogFile, $utf8Bom)
    [System.IO.File]::AppendAllText($script:PythonLogFile, "# hayamimi python log`r`n", [System.Text.UTF8Encoding]::new($false))
    $script:LastPythonLogLineCount = 0
    $script:LastPythonLogByteOffset = 0L
    # 4-class 句読点モデル (models/punct-ja-4class-permissive) の存在確認 & 初回自動ダウンロード (約40MB)
    $punct4Dir = Join-Path $ProjectRoot "models\punct-ja-4class-permissive"
    $punct4Onnx = Join-Path $punct4Dir "punct_4class.onnx"
    $punct4Tok = Join-Path $punct4Dir "hf\tokenizer.json"
    if (-not (Test-Path $punct4Onnx) -or -not (Test-Path $punct4Tok)) {
        Write-AppLog "4-class 句読点モデルを準備しています (約40MB)..." "INFO"
        $dlScript = Join-Path $ProjectRoot "scripts\download_models.py"
        if (Test-Path $dlScript) {
            $pinfoDl = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoDl.FileName = $venvPython
            $pinfoDl.Arguments = "-B `"$dlScript`" --minimal --punct-4class"
            $pinfoDl.WorkingDirectory = $ProjectRoot
            $pinfoDl.RedirectStandardOutput = $true
            $pinfoDl.RedirectStandardError = $true
            $pinfoDl.UseShellExecute = $false
            $pinfoDl.CreateNoWindow = $true
            $pinfoDl.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
            $procDl = [System.Diagnostics.Process]::Start($pinfoDl)
            Wait-ProcessWithUiPump -Proc $procDl -LogPrefix "[4class句読点取得]"
            Protect-ModelFiles
        }
    }

    $transScript = Join-Path $ProjectRoot "scripts\realtime_transcribe.py"
    $venvDir = Split-Path $venvPython
    $venvScripts = Join-Path $venvDir "Scripts"

    # 推論デバイス & ASR エンジン設定
    $engineArg = "--engine reazonspeech"
    $devEnv = "cpu"

    if ($script:CurrentDeviceMode -eq "Npu" -and $script:HasNpu) {
        $devEnv = "npu"
        $deviceOptArg = "--device npu"
        Write-AppLog "推論デバイス: NPU 起動 ($($script:NpuName) / CPU・GPU負荷ゼロ)" "INFO"
    } else {
        $devEnv = "cpu"
        $deviceOptArg = "--device cpu"
        Write-AppLog "推論デバイス: CPU 起動 (ReazonSpeech / ゲーム・配信優先)" "INFO"
    }

    $cmdArgs = "/s /c `"chcp 65001 >nul && `"$venvPython`" -B `"$transScript`" --input ws --serve 8833 --ws-host 0.0.0.0 --ws-port 8766 --punct-model 4class $engineArg $deviceOptArg >> `"$script:PythonLogFile`" 2>&1`""

    $pinfo = [System.Diagnostics.ProcessStartInfo]::new()
    $pinfo.FileName = "cmd.exe"
    $pinfo.Arguments = $cmdArgs
    $pinfo.WorkingDirectory = $ProjectRoot
    $pinfo.UseShellExecute = $false
    $pinfo.CreateNoWindow = $true
    $pinfo.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
    $pinfo.Environment["PYTHONIOENCODING"] = "utf-8"
    $pinfo.Environment["PYTHONUTF8"] = "1"
    $pinfo.Environment["HAYAMIMI_DEVICE"] = $devEnv
    $curSysPath = [Environment]::GetEnvironmentVariable("PATH")
    $pinfo.Environment["PATH"] = "$venvScripts;$venvDir;$curSysPath"

    $script:ProcPython = [System.Diagnostics.Process]::Start($pinfo)
    $script:HealthCheckAttempts = 0
    $script:ReadyConsecutiveCount = 0
    $healthTimer.Start()
}

# Python venv アプリの停止処理
function Stop-PythonVenvApp {
    param([bool]$Silent = $false)
    if (-not $Silent) {
        Set-UIState "停止処理中..." $brushOrange $false $false
        Write-AppLog "Python プロセスを停止しています..." "INFO"
    }

    if ($script:ProcPython -and -not $script:ProcPython.HasExited) {
        try {
            Start-Process taskkill.exe -ArgumentList "/PID $($script:ProcPython.Id) /T /F" -Wait -NoNewWindow
        } catch {}
    }

    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*realtime_transcribe.py*" } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {}

    $script:ProcPython = $null
    if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
    if (-not $Silent) {
        Write-AppLog "Python プロセスを停止しました。" "INFO"
        Set-UIState "停止中" $brushGray $true $false
    }
}

# hayamimi コンテナ停止処理
function Stop-HayamimiContainer {
    param([bool]$Silent = $false)
    if (-not $Silent) {
        Write-AppLog "hayamimi コンテナを停止しています..." "INFO"
    }
    Update-ContainerLogFile
    if ($script:WslcCmd) {
        try {
            $pinfoStop = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoStop.FileName = $script:WslcCmd
            $pinfoStop.Arguments = "stop $ContainerName"
            $pinfoStop.UseShellExecute = $false
            $pinfoStop.CreateNoWindow = $true
            $procStop = [System.Diagnostics.Process]::Start($pinfoStop)
            $procStop.WaitForExit()
        } catch {}

        try {
            $pinfoRm = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoRm.FileName = $script:WslcCmd
            $pinfoRm.Arguments = "remove $ContainerName"
            $pinfoRm.UseShellExecute = $false
            $pinfoRm.CreateNoWindow = $true
            $procRm = [System.Diagnostics.Process]::Start($pinfoRm)
            $procRm.WaitForExit()
        } catch {}
    }
    if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
    if (-not $Silent) {
        Write-AppLog "コンテナを正常に終了・クリーンアップしました。" "INFO"
        Set-UIState "停止中" $brushGray $true $false
    }
}

# 残留プロセスおよび占有ポート (8833, 8766) の徹底クリーンアップ関数
function Cleanup-ResidualProcesses {
    param([bool]$LogOutput = $true)
    if ($LogOutput) {
        Write-AppLog "残留プロセスおよび待機ポート (8833, 8766) のクリーンアップを実行しています..." "INFO"
    }

    # 1. realtime_transcribe / subtitle_server を含むプロセスの強制終了
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -match "realtime_transcribe\.py|subtitle_server\.py"
        } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {}

    # 2. ポート 8833 (HTTP) および 8766 (WebSocket) を占有しているプロセスの特定と安全な解放 (M-11)
    try {
        $ports = @(8833, 8766)
        if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
            foreach ($p in $ports) {
                $conns = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue
                if ($conns) {
                    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
                    foreach ($procId in $pids) {
                        if ($procId -gt 4 -and $procId -ne $PID) {
                            $targetProc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                            if ($targetProc) {
                                $procName = $targetProc.ProcessName.ToLower()
                                $isHayamimiProcess = $false
                                if ($procName -match 'python|cmd') {
                                    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
                                    if ($cim -and ($cim.CommandLine -match 'hayamimi|realtime_transcribe|subtitle_server')) {
                                        $isHayamimiProcess = $true
                                    } elseif ($procName -match 'python' -and $targetProc.Path -like '*hayamimi*') {
                                        $isHayamimiProcess = $true
                                    }
                                }
                                if ($isHayamimiProcess) {
                                    if ($LogOutput) {
                                        Write-AppLog "hayamimi の残留プロセス ($($targetProc.ProcessName), PID: $procId) を解放します..." "INFO"
                                    }
                                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                                } else {
                                    if ($LogOutput) {
                                        Write-AppLog "警告: ポート $p は別プロセス ($($targetProc.ProcessName), PID: $procId) に使用されています（安全のため自動終了は行いません）。" "WARN"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    } catch {}

    # 3. WSL Containers のコンテナ強制クリーンアップ
    if ($script:WslcCmd) {
        try {
            $pinfoRm = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoRm.FileName = $script:WslcCmd
            $pinfoRm.Arguments = "stop $ContainerName"
            $pinfoRm.UseShellExecute = $false
            $pinfoRm.CreateNoWindow = $true
            $p = [System.Diagnostics.Process]::Start($pinfoRm)
            $p.WaitForExit()
        } catch {}
        try {
            $pinfoRm2 = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoRm2.FileName = $script:WslcCmd
            $pinfoRm2.Arguments = "remove $ContainerName"
            $pinfoRm2.UseShellExecute = $false
            $pinfoRm2.CreateNoWindow = $true
            $p2 = [System.Diagnostics.Process]::Start($pinfoRm2)
            $p2.WaitForExit()
        } catch {}
    }
}

# アプリ再起動メイン関数
function Invoke-RestartApp {
    try {
        Write-AppLog "【再起動】アプリの再起動処理を開始します..." "INFO"
        Set-UIState "再起動中..." $brushOrange $false $false $false

        if ($healthTimer) { $healthTimer.Stop() }

        # 1. 稼働中アプリの停止
        if ($script:CurrentEngineMode -eq "Venv") {
            Stop-PythonVenvApp -Silent $true
        } else {
            Stop-HayamimiContainer -Silent $true
        }

        # 2. 残留プロセス・ポートの徹底クリーンアップ
        Cleanup-ResidualProcesses -LogOutput $true

        # 3. OSソケット解放待機 (TIME_WAIT対策)
        Start-Sleep -Milliseconds 800

        # 4. アプリ再起動の実行
        Write-AppLog "【再起動】プロセスクリーンアップ完了。起動処理を開始します..." "INFO"
        if ($script:CurrentEngineMode -eq "Venv") {
            Start-PythonVenvApp
        } else {
            Start-HayamimiContainer
        }
    } catch {
        Write-AppLog "再起動処理中にエラーが発生しました: $_" "ERROR"
        Set-UIState "エラー" $brushRed $true $false $true
    }
}

# WSL 3.0 (WSL Containers) 自動インストール関数
function Invoke-InstallWslContainers {
    try {
        Write-AppLog "WSL 3.0 / WSL Containers の自動セットアップを開始します..."
        $installScript = @'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "  hayamimi - WSL 3.0 / WSL Containers 自動セットアップ" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""

# 0. Windows 仮想化基盤 (VirtualMachinePlatform) & Hypervisor の有効化
$script:RebootNeeded = $false
Write-Host "[ステップ 1/5] Windows 仮想化機能 (VirtualMachinePlatform) を有効化しています..." -ForegroundColor Yellow
try {
    Write-Host "実行中: DISM (VirtualMachinePlatform & Subsystem-Linux)..."
    $pDism1 = Start-Process dism.exe -ArgumentList "/online /enable-feature /featurename:VirtualMachinePlatform /all /norestart" -Wait -PassThru -NoNewWindow
    $pDism2 = Start-Process dism.exe -ArgumentList "/online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart" -Wait -PassThru -NoNewWindow
    Start-Process bcdedit.exe -ArgumentList "/set hypervisorlaunchtype auto" -Wait -NoNewWindow

    if ($pDism1.ExitCode -eq 3010 -or $pDism2.ExitCode -eq 3010) {
        $script:RebootNeeded = $true
    }
} catch {
    Write-Host "DISM 機能有効化スキップ: $_" -ForegroundColor DarkGray
}

# 1. WSL 自体の存在確認
$hasWsl = (where.exe wsl 2>$null)
if (-not $hasWsl) {
    Write-Host ""
    Write-Host "[ステップ 2/5] Windows Subsystem for Linux (WSL) 基盤を新規インストールしています..." -ForegroundColor Yellow
    Write-Host "実行中: wsl.exe --install --no-distribution"
    wsl.exe --install --no-distribution
} else {
    Write-Host ""
    Write-Host "[ステップ 2/5] WSL 基盤は検出されました。" -ForegroundColor Green
}

# 2. WSL を最新版に更新
Write-Host ""
Write-Host "[ステップ 3/5] WSL パッケージを最新版に更新しています..." -ForegroundColor Yellow
Write-Host "実行中: wsl.exe --update"
wsl.exe --update

# 3. プレリリース (WSL 3.0 / wslc) パッケージの適用チェック
$hasWslc = (where.exe wslc 2>$null)
if (-not $hasWslc -and -not (Test-Path "C:\Program Files\WSL\wslc.exe")) {
    Write-Host ""
    Write-Host "[ステップ 4/5] WSL 3.0 (wslc) プレリリース版パッケージを取得しています..." -ForegroundColor Yellow
    Write-Host "実行中: wsl.exe --update --pre-release"
    wsl.exe --update --pre-release
}

# 4. フォールバック: GitHub 公式リリースからの直接インストール (Store 制限等の環境向け)
$hasWslc = (where.exe wslc 2>$null)
if (-not $hasWslc -and -not (Test-Path "C:\Program Files\WSL\wslc.exe")) {
    Write-Host ""
    Write-Host "[ステップ 5/5] GitHub から最新の WSL x64 インストーラーを直接取得しています..." -ForegroundColor Yellow
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        $apiUrl = "https://api.github.com/repos/microsoft/WSL/releases"
        $headers = @{ "User-Agent" = "hayamimi-installer" }
        $releases = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 15
        
        $targetAsset = $null
        foreach ($rel in $releases) {
            $msiAsset = $rel.assets | Where-Object { $_.name -match "wsl.*x64.*\.msi" } | Select-Object -First 1
            if ($msiAsset) {
                $targetAsset = $msiAsset
                break
            }
        }

        if ($targetAsset) {
            $tempMsi = [System.IO.Path]::Combine($env:TEMP, $targetAsset.name)
            Write-Host "ダウンロード中: $($targetAsset.browser_download_url)"
            Invoke-WebRequest -Uri $targetAsset.browser_download_url -OutFile $tempMsi
            Write-Host "MSI パッケージをインストール中..."
            Start-Process msiexec.exe -ArgumentList "/i `"$tempMsi`" /quiet /norestart" -Wait
        }
    } catch {
        Write-Host "GitHub からの直接取得エラー: $_" -ForegroundColor Red
    }
}

# 5. PATH 環境変数の確認と登録
$wslDir = "C:\Program Files\WSL"
if (Test-Path (Join-Path $wslDir "wslc.exe")) {
    try {
        $curSysPath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        if ($curSysPath -notlike "*$wslDir*") {
            [Environment]::SetEnvironmentVariable("Path", "$curSysPath;$wslDir", "Machine")
            Write-Host "環境変数 PATH に '$wslDir' を追加しました。" -ForegroundColor Green
        }
    } catch {}
}

Write-Host ""
Write-Host "===================================================" -ForegroundColor Green
Write-Host "  WSL 3.0 & 仮想化基盤のセットアップが完了しました！" -ForegroundColor Green
Write-Host "===================================================" -ForegroundColor Green
if ($script:RebootNeeded) {
    Write-Host "【重要】仮想マシンプラットフォームを新規に有効化したため、" -ForegroundColor Yellow
    Write-Host "        仮想化機能の完全適用には PC の再起動が必要です。" -ForegroundColor Yellow
} else {
    Write-Host "※ 新規に WSL を有効化した場合は、PC の再起動が必要になる場合があります。" -ForegroundColor Cyan
}
Write-Host "このウィンドウを閉じると、ランチャーで自動認識されます。"
Write-Host ""
Read-Host "Enterキーを押して終了してください..."
'@

        $uniqueId = [System.Guid]::NewGuid().ToString("N")
        $scriptBlockFile = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "install_wsl3_$uniqueId.ps1")
        [System.IO.File]::WriteAllText($scriptBlockFile, $installScript, [System.Text.Encoding]::UTF8)

        $proc = Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$scriptBlockFile`"" -PassThru
        $proc.WaitForExit()
        Remove-Item -Path $scriptBlockFile -Force -ErrorAction SilentlyContinue

        # システム環境変数 PATH のリロード (現在のプロセス)
        try {
            $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
            $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
            $env:Path = "$machinePath;$userPath;C:\Program Files\WSL"
        } catch {}

        # 再検出
        $script:WslcCmd = Get-WslcCommand
        if ($script:WslcCmd) {
            Write-AppLog "✔ WSL 3.0 (WSL Containers) が正常にインストール・検出されました: $script:WslcCmd"
            if ($wslcAlertBanner) { $wslcAlertBanner.Visibility = "Collapsed" }
            $runtimeBadge.Text = "WSL3 Native"
            $runtimeBadge.Foreground = $brushGreen
            Set-UIState "停止中" $brushGray $true $false
            return $true
        } else {
            Write-AppLog "WSL更新後も wslc.exe が見つかりませんでした。PC再起動後に再度お試しください。" "WARN"
            return $false
        }
    } catch {
        Write-AppLog "管理者権限プロンプトの起動がキャンセルされたか失敗しました: $_" "WARN"
        return $false
    }
}

# コンテナ起動処理本体
function Start-HayamimiContainer {
    try {
        Set-UIState "起動中..." $brushOrange $false $false
        Write-AppLog "hayamimi コンテナの起動処理を開始します..."

        # container.log の初期化
        $script:LastContainerLogLineCount = 0
        $initHeader = "# === hayamimi コンテナ起動: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===`r`n"
        try {
            [System.IO.File]::WriteAllText($ContainerLogFile, $initHeader, [System.Text.Encoding]::UTF8)
        } catch {}

        # 既存コンテナの安全な停止・削除
        try {
            $pinfoStop = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoStop.FileName = $script:WslcCmd
            $pinfoStop.Arguments = "stop $ContainerName"
            $pinfoStop.UseShellExecute = $false
            $pinfoStop.CreateNoWindow = $true
            $procStop = [System.Diagnostics.Process]::Start($pinfoStop)
            $procStop.WaitForExit()

            $pinfoRm = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoRm.FileName = $script:WslcCmd
            $pinfoRm.Arguments = "remove $ContainerName"
            $pinfoRm.UseShellExecute = $false
            $pinfoRm.CreateNoWindow = $true
            $procRm = [System.Diagnostics.Process]::Start($pinfoRm)
            $procRm.WaitForExit()
        } catch {}

        # models および config ディレクトリ作成 (モデルと設定の永続化)
        $modelsDir = Join-Path $ProjectRoot "models"
        if (-not (Test-Path $modelsDir)) {
            New-Item -ItemType Directory -Path $modelsDir -Force | Out-Null
            Write-AppLog "models ディレクトリを作成しました: $modelsDir"
        }
        $configDir = Join-Path $ProjectRoot "config"
        if (-not (Test-Path $configDir)) {
            New-Item -ItemType Directory -Path $configDir -Force | Out-Null
            Write-AppLog "config ディレクトリを作成しました: $configDir"
        }

        # 推論デバイス オプション (NVIDIA GPU パススルー & HAYAMIMI_DEVICE)
        $useGpu = ($script:CurrentDeviceMode -eq "Gpu" -and $script:HasNvidiaGpu)
        $gpuArg = ""
        $devEnvArg = "-e HAYAMIMI_DEVICE=cpu "
        if ($useGpu) {
            $gpuArg = "--gpus all "
            $devEnvArg = "-e HAYAMIMI_DEVICE=cuda "
            Write-AppLog "GPU 加速モードで起動します: NVIDIA GPU パススルー (--gpus all) を有効化" "INFO"
        } else {
            Write-AppLog "CPU 起動モードで起動します: GPU/VRAM負荷ゼロ (ゲーム・配信優先)" "INFO"
        }

        # コンテナ起動 (wslc run -d)
        Write-AppLog "wslc run を実行中 (ポート 8833, 8766)..."
        $volumeModels = "${modelsDir}:/app/models"
        $volumeConfig = "${configDir}:/app/config"

        $pinfo = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfo.FileName = $script:WslcCmd
        $pinfo.Arguments = "run -d --name $ContainerName $gpuArg$devEnvArg-p 8833:8833 -p 8766:8766 -v `"$volumeModels`" -v `"$volumeConfig`" $ImageName --translate en"
        $pinfo.RedirectStandardOutput = $true
        $pinfo.RedirectStandardError = $true
        $pinfo.UseShellExecute = $false
        $pinfo.CreateNoWindow = $true

        $proc = [System.Diagnostics.Process]::Start($pinfo)
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()

        # 万一 --gpus all による起動が失敗した場合、GPUなしでフォールバック再試行
        if ($proc.ExitCode -ne 0 -and $gpuArg -ne "") {
            Write-AppLog "GPU パススルー起動で問題が発生したため、CPU モードで再試行します..." "WARN"
            $pinfo.Arguments = "run -d --name $ContainerName -e HAYAMIMI_DEVICE=cpu -p 8833:8833 -p 8766:8766 -v `"$volumeModels`" -v `"$volumeConfig`" $ImageName --translate en"
            $proc = [System.Diagnostics.Process]::Start($pinfo)
            $stdout = $proc.StandardOutput.ReadToEnd()
            $stderr = $proc.StandardError.ReadToEnd()
            $proc.WaitForExit()
        }

        if ($proc.ExitCode -ne 0) {
            Write-AppLog "エラー: コンテナの起動に失敗しました (ExitCode: $($proc.ExitCode))" "ERROR"
            if ($stdout) { Write-AppLog "標準出力: $stdout" "ERROR" }
            if ($stderr) { Write-AppLog "標準エラー: $stderr" "ERROR" }
            Update-ContainerLogFile -EchoToGui $true
            Set-UIState "起動失敗" $brushRed $true $false

            # イメージ未検出エラーなら自動ビルドを促す
            if ($stderr -match "IMAGE_NOT_FOUND|pull access denied") {
                $askBuild = Show-AppConfirm -Message "コンテナイメージ ($ImageName) が見つかりませんでした。`r`n`r`n今すぐローカルでイメージをビルドしますか？" -Title "コンテナイメージの構築 - hayamimi"
                if ($askBuild -eq [System.Windows.MessageBoxResult]::Yes) {
                    Invoke-BuildContainerImage -Force $false -AutoStart $true | Out-Null
                }
            }
            return
        }

        $containerId = $stdout.Trim()
        Write-AppLog "コンテナプロセスが開始されました (ID: $containerId)"
        Write-AppLog "※ 初回起動時はモデルのダウンロード (約1.7GB) のため、起動完了まで数分かかります。" "INFO"
        Write-AppLog "※ 進行状況は下のログおよび logs/container.log に随時表示されます。" "INFO"
        Write-AppLog "サーバー初期化とポート8833の待機を確認しています..." "INFO"

        # 起動直後のコンテナ状態を診断ログ出力
        try {
            $pinfoDiag = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoDiag.FileName = $script:WslcCmd
            $pinfoDiag.Arguments = "inspect $ContainerName"
            $pinfoDiag.RedirectStandardOutput = $true
            $pinfoDiag.UseShellExecute = $false
            $pinfoDiag.CreateNoWindow = $true
            $procDiag = [System.Diagnostics.Process]::Start($pinfoDiag)
            $diagRaw = $procDiag.StandardOutput.ReadToEnd()
            $procDiag.WaitForExit(1000) | Out-Null
            if ($diagRaw -match '"Status"\s*:\s*"([^"]+)"') {
                Write-AppLog "コンテナ初期ステータス: $($Matches[1])" "INFO"
            }
        } catch {}

        Update-ContainerLogFile -EchoToGui $true
        $script:HealthCheckAttempts = 0
        $script:ContainerDeadCount = 0
        $script:ReadyConsecutiveCount = 0
        $healthTimer.Start()

    } catch {
        Write-AppLog "起動処理中に例外が発生しました: $($_.Exception.ToString())" "ERROR"
        Set-UIState "エラー" $brushRed $true $false
    }
}

# ビルド状態変数
$script:BuildLogFile = Join-Path $LogsDir "build.log"
$script:LastBuildLogLineCount = 0
$script:ProcBuild = $null
$script:BuildAutoStart = $false

# コンテナイメージのビルド / リビルド関数 (ファイルリダイレクト & タイマー駆動方式)
function Invoke-BuildContainerImage {
    param(
        [bool]$Force = $false,
        [bool]$AutoStart = $false
    )

    if (-not $script:WslcCmd) {
        Write-AppLog "WSL Containers (wslc.exe) が利用できません。先に WSL 3.0 をセットアップしてください。" "ERROR"
        return $false
    }

    if (Test-ContainerRunning) {
        Write-AppLog "ビルドのため、稼働中のコンテナを停止・削除します..."
        try {
            $pinfoStop = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoStop.FileName = $script:WslcCmd
            $pinfoStop.Arguments = "stop $ContainerName"
            $pinfoStop.UseShellExecute = $false
            $pinfoStop.CreateNoWindow = $true
            $procStop = [System.Diagnostics.Process]::Start($pinfoStop)
            $procStop.WaitForExit()

            $pinfoRm = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoRm.FileName = $script:WslcCmd
            $pinfoRm.Arguments = "remove $ContainerName"
            $pinfoRm.UseShellExecute = $false
            $pinfoRm.CreateNoWindow = $true
            $procRm = [System.Diagnostics.Process]::Start($pinfoRm)
            $procRm.WaitForExit()
        } catch {}
    }

    Set-UIState "イメージ構築中..." $brushOrange $false $false
    if ($btnRebuild) { $btnRebuild.IsEnabled = $false }

    $actionName = if ($Force) { "リビルド (再構築)" } else { "初回ビルド" }
    Write-AppLog "=========================================="
    Write-AppLog "コンテナイメージの $actionName を開始します..." "INFO"
    Write-AppLog "ビルドディレクトリ: $ProjectRoot" "INFO"
    Write-AppLog "実行: wslc build -t $ImageName ." "INFO"
    Write-AppLog "※ 環境により数分かかります。完了までそのままお待ちください..." "INFO"

    $script:BuildAutoStart = $AutoStart
    $script:LastBuildLogLineCount = 0

    try {
        if (Test-Path $script:BuildLogFile) {
            Remove-Item -Path $script:BuildLogFile -Force -ErrorAction SilentlyContinue
        }
        [System.IO.File]::WriteAllText($script:BuildLogFile, "# hayamimi build log`r`n", [System.Text.Encoding]::UTF8)

        $pinfoBuild = [System.Diagnostics.ProcessStartInfo]::new()
        $pinfoBuild.FileName = "cmd.exe"
        $pinfoBuild.Arguments = "/s /c `"`"$script:WslcCmd`" build -t $ImageName . > `"$script:BuildLogFile`" 2>&1`""
        $pinfoBuild.WorkingDirectory = $ProjectRoot
        $pinfoBuild.UseShellExecute = $false
        $pinfoBuild.CreateNoWindow = $true

        $script:ProcBuild = [System.Diagnostics.Process]::Start($pinfoBuild)
        $buildTimer.Start()
        return $true
    } catch {
        if ($btnRebuild) { $btnRebuild.IsEnabled = $true }
        Write-AppLog "ビルドプロセスの起動に失敗しました: $_" "ERROR"
        Set-UIState "エラー" $brushRed $true $false
        return $false
    }
}

# バージョン文字列を [version] オブジェクトに変換するヘルパー
function Get-ParsedVersion {
    param([string]$verText)
    if (-not $verText) { return $null }
    if ($verText -match '(\d+\.\d+\.\d+(\.\d+)?)') {
        try {
            return [version]$matches[1]
        } catch {
            return $null
        }
    }
    return $null
}

# GitHub の最新バージョン確認関数
# GitHub の最新バージョン確認関数 (承認済み動詞 Test- を適用)
function Test-AppUpdate {
    param(
        [switch]$PromptUser,
        [switch]$SilentOnLatest
    )

    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

        $url = $script:VersionCheckUrl
        # キャッシュ回避用クエリパラメータ (Unixタイムスタンプ秒)
        $randSec = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $reqUrl = "${url}?_t=${randSec}"

        Write-AppLog "GitHub (version.txt) から最新バージョンを確認中..." "INFO"

        $req = [System.Net.HttpWebRequest]::Create($reqUrl)
        $req.Timeout = 4000
        $req.ReadWriteTimeout = 4000
        $req.UserAgent = "hayamimi-launcher/$script:AppVersion"
        $req.Headers.Add("Cache-Control", "no-cache")

        $resp = $req.GetResponse()
        $stream = $resp.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        $remoteVerRaw = $reader.ReadToEnd().Trim()
        $reader.Close()
        $stream.Close()
        $resp.Close()

        $remoteVer = Get-ParsedVersion $remoteVerRaw
        $localVer = Get-ParsedVersion $script:AppVersion

        if ($remoteVer -and $localVer) {
            if ($remoteVer -gt $localVer) {
                Write-AppLog "📢 新しいバージョン (v$remoteVer) が公開されています！（現在のバージョン: v$localVer）" "WARN"

                # UI の更新ボタンを強調表示
                if ($btnUpdateApp) {
                    $btnUpdateApp.Background = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#064E3B")
                    $btnUpdateApp.BorderBrush = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#10B981")
                    $btnUpdateApp.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#34D399")
                    $btnUpdateApp.ToolTip = "新しいバージョン (v$remoteVer) にアップデートできます"
                }
                if ($btnUpdateAppIcon) {
                    $btnUpdateAppIcon.Text = "🔔"
                }
                if ($btnUpdateAppText) {
                    $btnUpdateAppText.Text = "最新版 (v$remoteVer) に更新"
                }
                if ($updateDescText) {
                    $updateDescText.Text = "📢 新しいバージョン (v$remoteVer) が利用可能です！"
                    $updateDescText.Foreground = [System.Windows.Media.BrushConverter]::new().ConvertFromString("#34D399")
                }

                if ($PromptUser) {
                    $ask = Show-AppConfirm -Message "新しいバージョン (v$remoteVer) が公開されています。`r`n`r`n現在のバージョン: v$localVer`r`n最新のバージョン: v$remoteVer`r`n`r`n今すぐ最新版にアップデートしますか？`r`n（アップデート後、ランチャーは自動で再起動します）" -Title "アップデートの確認 - hayamimi"
                    if ($ask -eq [System.Windows.MessageBoxResult]::Yes) {
                        Invoke-UpdateAppCode -AutoConfirm
                    }
                }
                return $true
            } else {
                if (-not $SilentOnLatest) {
                    Write-AppLog "✔ hayamimi は最新バージョン (v$localVer) です。" "INFO"
                }
                return $false
            }
        } else {
            Write-AppLog "バージョン情報の解析に失敗しました: '$remoteVerRaw'" "WARN"
            return $false
        }
    } catch {
        Write-AppLog "バージョン確認をスキップしました (オフラインまたは接続タイムアウト): $($_.Exception.Message)" "WARN"
        return $false
    }
}

# 本体の最新コード取得（アップデート）関数
function Invoke-UpdateAppCode {
    param([switch]$AutoConfirm)

    try {
        if (-not $AutoConfirm) {
            $confirm = Show-AppConfirm -Message "GitHub (Maximilian2022/hayamimi) から最新のソースコードを取得（アップデート）しますか？`r`n`r`n※ 取得完了後、ランチャーが自動再起動され最新機能が反映されます。" -Title "本体のアップデート - hayamimi"
            if ($confirm -ne [System.Windows.MessageBoxResult]::Yes) { return }
        }

        Write-AppLog "=========================================="
        Write-AppLog "本体の最新コード取得（アップデート）を開始します..." "INFO"
        Set-UIState "更新取得中..." $brushOrange $false $false
        if ($btnUpdateApp) { $btnUpdateApp.IsEnabled = $false }
        if ($btnRebuild) { $btnRebuild.IsEnabled = $false }

        $updateSuccess = $false
        # アップデート前の自動設定バックアップ (万が一の障害防止)
        try {
            $cfgSrc = Join-Path $ProjectRoot "config"
            if (Test-Path $cfgSrc) {
                $backupDir = Join-Path $ProjectRoot ("config_backups\backup_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
                New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
                Copy-Item -Path (Join-Path $cfgSrc "*") -Destination $backupDir -Recurse -Force -ErrorAction SilentlyContinue
                Write-AppLog "アップデート前の設定バックアップを作成しました: $backupDir" "INFO"
            }
        } catch {
            Write-AppLog "設定バックアップ作成中に警告: $_" "WARN"
        }
        $hasGit = $false

        # 1. git の存在確認
        try {
            $gitCmd = where.exe git 2>$null
            if ($gitCmd -and (Test-Path (Join-Path $ProjectRoot ".git"))) {
                $hasGit = $true
            }
        } catch {}

        if ($hasGit) {
            Write-AppLog "Git リポジトリを検出しました。git pull を実行中..." "INFO"
            $pinfoGit = [System.Diagnostics.ProcessStartInfo]::new()
            $pinfoGit.FileName = "git.exe"
            $pinfoGit.Arguments = "pull origin main"
            $pinfoGit.WorkingDirectory = $ProjectRoot
            $pinfoGit.RedirectStandardOutput = $true
            $pinfoGit.RedirectStandardError = $true
            $pinfoGit.UseShellExecute = $false
            $pinfoGit.CreateNoWindow = $true

            $procGit = [System.Diagnostics.Process]::Start($pinfoGit)
            $gitStdout = $procGit.StandardOutput.ReadToEnd()
            $gitStderr = $procGit.StandardError.ReadToEnd()
            $procGit.WaitForExit()

            if ($gitStdout) {
                $gLines = ($gitStdout -split "`r?`n") | Where-Object { $_.Trim() }
                if ($gLines) {
                    Write-AppLogBatch -Messages ($gLines | ForEach-Object { "[Git] $_" }) -Level "INFO"
                }
            }
            if ($procGit.ExitCode -eq 0) {
                Write-AppLog "✔ Git による最新コードの取得が正常に完了しました！" "INFO"
                $updateSuccess = $true
            } else {
                if ($gitStderr) {
                    $eLines = ($gitStderr -split "`r?`n") | Where-Object { $_.Trim() }
                    if ($eLines) {
                        Write-AppLogBatch -Messages ($eLines | ForEach-Object { "[Git エラー] $_" }) -Level "WARN"
                    }
                }
                Write-AppLog "Git pull に失敗したため、GitHub からの直接ダウンロードに切り替えます..." "WARN"
            }
        }

        # 2. Git がない、または git pull 失敗時は GitHub ZIP 直接取得
        if (-not $updateSuccess) {
            Write-AppLog "GitHub から最新ソースコードアーカイブを取得中..." "INFO"
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
            $zipUrl = "https://github.com/Maximilian2022/hayamimi-live/archive/refs/heads/main.zip"
            $tempZip = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "hayamimi-latest.zip")
            $tempExtract = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "hayamimi-extract-" + [System.Guid]::NewGuid().ToString("N"))

            try {
                Invoke-WebRequest -Uri $zipUrl -OutFile $tempZip -UseBasicParsing -TimeoutSec 30
            } catch {
                $fallbackZipUrl = "https://github.com/Maximilian2022/hayamimi/archive/refs/heads/main.zip"
                Invoke-WebRequest -Uri $fallbackZipUrl -OutFile $tempZip -UseBasicParsing -TimeoutSec 30
            }
            # 展開前に ZIP ファイル自体の Mark of the Web (Zone.Identifier) を除去し、スマートアプリコントロールの誤検知を防止
            try { Unblock-File -LiteralPath $tempZip -ErrorAction SilentlyContinue } catch {}
            Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force

            # 展開フォルダ内のファイルをプロジェクトルートに安全上書き (.venv / models / logs / config / scratch / .git は保持)
            $innerDir = Get-ChildItem -Path $tempExtract -Directory | Select-Object -First 1
            if ($innerDir) {
                $srcItems = Get-ChildItem -Path $innerDir.FullName
                foreach ($item in $srcItems) {
                    if ($item.Name -in @(".git", ".venv", "models", "logs", "config", "profiles", "scratch", "config_backups")) { continue }
                    $destPath = Join-Path $ProjectRoot $item.Name
                    if ($item.PSIsContainer) {
                        if (-not (Test-Path $destPath)) {
                            New-Item -ItemType Directory -Path $destPath -Force | Out-Null
                        }
                        Copy-Item -Path (Join-Path $item.FullName "*") -Destination $destPath -Recurse -Force
                    } else {
                        Copy-Item -Path $item.FullName -Destination $destPath -Force
                    }
                }

                # 過去の二重ネストフォルダの自己修復
                $cleanCandidateDirs = @("scripts", "docs", "tests", "demo", "mobile", "nginx", ".github")
                foreach ($cand in $cleanCandidateDirs) {
                    $pDir = Join-Path $ProjectRoot $cand
                    $nDir = Join-Path $pDir $cand
                    if ((Test-Path $pDir) -and (Test-Path $nDir) -and (Get-Item $nDir).PSIsContainer) {
                        $cItems = Get-ChildItem -Path $nDir
                        foreach ($ci in $cItems) {
                            $targetCi = Join-Path $pDir $ci.Name
                            if (-not (Test-Path $targetCi)) {
                                Move-Item -Path $ci.FullName -Destination $targetCi -Force
                            }
                        }
                        Remove-Item -Path $nDir -Recurse -Force -ErrorAction SilentlyContinue
                    }
                }

                Write-AppLog "✔ GitHub アーカイブからの最新コード反映が完了しました！" "INFO"
                $updateSuccess = $true
            }

            # クリーンアップ
            Remove-Item -Path $tempZip -Force -ErrorAction SilentlyContinue
            Remove-Item -Path $tempExtract -Recurse -Force -ErrorAction SilentlyContinue
        }

        if ($btnUpdateApp) { $btnUpdateApp.IsEnabled = $true }
        if ($btnRebuild) { $btnRebuild.IsEnabled = $true }
        Set-UIState "停止中" $brushGray $true $false

        if ($updateSuccess) {
            # 取得・展開された全ファイルから Zone.Identifier (Mark of the Web) を除去し、UTF-8 BOM を保証
            Unblock-ProjectFiles -RootDir $ProjectRoot

            # 既存の .venv に対する依存パッケージの更新確認 (スマートアプリコントロール SAC 安全フォールバック付き)
            $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
            $reqFile = Join-Path $ProjectRoot "requirements.txt"
            if ((Test-Path $venvPython) -and (Test-Path $reqFile)) {
                Write-AppLog "仮想環境 (.venv) の依存パッケージ更新を確認・適用しています..." "INFO"
                $pipDone = $false
                $uvCmd = Get-SafeUvCommand
                if ($uvCmd) {
                    try {
                        $pinfoPip = [System.Diagnostics.ProcessStartInfo]::new()
                        $pinfoPip.FileName = $uvCmd
                        $pinfoPip.Arguments = "pip install -r `"$reqFile`" --python `"$venvPython`""
                        $pinfoPip.WorkingDirectory = $ProjectRoot
                        $pinfoPip.RedirectStandardOutput = $true
                        $pinfoPip.RedirectStandardError = $true
                        $pinfoPip.UseShellExecute = $false
                        $pinfoPip.CreateNoWindow = $true
                        $pinfoPip.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
                        $procPip = [System.Diagnostics.Process]::Start($pinfoPip)
                        Wait-ProcessWithUiPump -Proc $procPip -LogPrefix "[依存更新]"
                        if ($procPip.ExitCode -eq 0) { $pipDone = $true }
                    } catch {
                        Write-AppLog "uv による依存更新が制限されたため、標準 pip に切り替えます: $_" "WARN"
                    }
                }
                if (-not $pipDone) {
                    try {
                        Unblock-File -LiteralPath $venvPython -ErrorAction SilentlyContinue
                        $pinfoPip = [System.Diagnostics.ProcessStartInfo]::new()
                        $pinfoPip.FileName = $venvPython
                        $pinfoPip.Arguments = "-B -m pip install -r `"$reqFile`""
                        $pinfoPip.WorkingDirectory = $ProjectRoot
                        $pinfoPip.RedirectStandardOutput = $true
                        $pinfoPip.RedirectStandardError = $true
                        $pinfoPip.UseShellExecute = $false
                        $pinfoPip.CreateNoWindow = $true
                        $pinfoPip.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
                        $procPip = [System.Diagnostics.Process]::Start($pinfoPip)
                        Wait-ProcessWithUiPump -Proc $procPip -LogPrefix "[依存更新]"
                        $pipDone = $true
                    } catch {
                        Write-AppLog "依存パッケージ自動更新をスキップしました: $_" "WARN"
                    }
                }
                Unblock-ProjectFiles -RootDir $ProjectRoot
                Write-AppLog "✔ 依存パッケージの同期が完了しました。" "INFO"
            }

            $askRestart = Show-AppConfirm -Message "最新のソースコードを取得しました！`r`n`r`nランチャーGUIを最新版で再起動しますか？" -Title "本体更新の完了 - hayamimi"
            if ($askRestart -eq [System.Windows.MessageBoxResult]::Yes) {
                Write-AppLog "ランチャーを再起動して最新コードを適用します..." "INFO"
                try {
                    # スマートアプリコントロール (SAC) 対策: 未署名 VBScript (wscript.exe) はブロックされるため絶対に使用しない。
                    # 現在起動に成功している PowerShell プロセス自身で直接新しいランチャーを起動する。
                    $currentPs = $null
                    try {
                        $currentPs = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
                    } catch {}
                    if (-not $currentPs -or -not (Test-Path $currentPs)) {
                        $currentPs = if ($PSVersionTable.PSEdition -eq "Core") { "pwsh.exe" } else { "powershell.exe" }
                    }
                    Start-Process -FilePath $currentPs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptDir\launcher_gui.ps1`""
                } catch {
                    $startBat = Join-Path $ProjectRoot "start.bat"
                    if (Test-Path $startBat) {
                        Start-Process "cmd.exe" -ArgumentList "/c `"$startBat`""
                    }
                }
                $window.Close()
                return
            }
        }
    } catch {
        Write-AppLog "本体アップデート処理中にエラーが発生しました: $_" "ERROR"
        Set-UIState "エラー" $brushRed $true $false
        if ($btnUpdateApp) { $btnUpdateApp.IsEnabled = $true }
        if ($btnRebuild) { $btnRebuild.IsEnabled = $true }
    }
}

# ビルド監視用タイマー (安全なイベント駆動方式 & バッチログ出力)
$buildTimer = [System.Windows.Threading.DispatcherTimer]::new()
$buildTimer.Interval = [TimeSpan]::FromMilliseconds(500)

$buildTimer.Add_Tick({
    try {
        # 1. ビルドログの読み込みとGUIリアルタイム反映 (ロック共有で安全読み取り & バッチ出力)
        if (Test-Path $script:BuildLogFile) {
            $lines = Read-FileSharedLines -FilePath $script:BuildLogFile
            if ($lines.Length -gt $script:LastBuildLogLineCount) {
                $batch = [System.Collections.Generic.List[string]]::new()
                for ($i = $script:LastBuildLogLineCount; $i -lt $lines.Length; $i++) {
                    $line = $lines[$i]
                    if ($line.Trim() -and $line -notmatch "^# hayamimi build log") {
                        $batch.Add("[ビルド] $line")
                    }
                }
                $script:LastBuildLogLineCount = $lines.Length
                if ($batch.Count -gt 0) {
                    Write-AppLogBatch -Messages $batch -Level "INFO"
                }
            }
        }

        # 2. プロセス終了チェック
        if ($script:ProcBuild -and $script:ProcBuild.HasExited) {
            $buildTimer.Stop()

            # 最後のログを読み切る
            Start-Sleep -Milliseconds 200
            if (Test-Path $script:BuildLogFile) {
                $lines = Read-FileSharedLines -FilePath $script:BuildLogFile
                if ($lines.Length -gt $script:LastBuildLogLineCount) {
                    $batch = [System.Collections.Generic.List[string]]::new()
                    for ($i = $script:LastBuildLogLineCount; $i -lt $lines.Length; $i++) {
                        $line = $lines[$i]
                        if ($line.Trim() -and $line -notmatch "^# hayamimi build log") {
                            $batch.Add("[ビルド] $line")
                        }
                    }
                    $script:LastBuildLogLineCount = $lines.Length
                    if ($batch.Count -gt 0) {
                        Write-AppLogBatch -Messages $batch -Level "INFO"
                    }
                }
            }

            $exitCode = $script:ProcBuild.ExitCode
            if ($btnRebuild) { $btnRebuild.IsEnabled = $true }

            if ($exitCode -eq 0) {
                Write-AppLog "✔ コンテナイメージ ($ImageName) の構築が正常に完了しました！" "INFO"
                Write-AppLog "=========================================="
                Set-UIState "停止中" $brushGray $true $false
                if ($script:BuildAutoStart) {
                    Write-AppLog "コンテナの起動処理へ自動移行します..."
                    Start-HayamimiContainer
                }
            } else {
                Write-AppLog "コンテナイメージのビルドに失敗しました (ExitCode: $exitCode)" "ERROR"
                Set-UIState "ビルド失敗" $brushRed $true $false

                # ビルドログの内容から原因を自動診断
                $buildLogContent = ""
                if (Test-Path $script:BuildLogFile) {
                    try {
                        $buildLogContent = [System.IO.File]::ReadAllText($script:BuildLogFile, [System.Text.Encoding]::UTF8)
                    } catch {}
                }

                if ($buildLogContent -match "HCS_E_SERVICE_NOT_AVAILABLE|0x80370102|必要な機能がインストールされていない") {
                    Write-AppLog "==========================================" "ERROR"
                    Write-AppLog "【エラー原因】Windows 仮想化基盤サービス (HCS: Host Compute Service) が未起動です。" "ERROR"
                    Write-AppLog "【最も多い原因】WSL や仮想化機能をインストールした直後で、PC の再起動が行われていません。" "WARN"
                    Write-AppLog "【対処手順 1】PC を再起動してください。再起動後に自動的に本エラーは解消されます。" "WARN"
                    Write-AppLog "【対処手順 2】PC 再起動後も同じ場合は、BIOS で CPU 仮想化支援 (VT-x / AMD-V) を有効化してください。" "WARN"
                    Write-AppLog "==========================================" "ERROR"
                    Set-UIState "仮想化サービス未起動 (再起動が必要)" $brushRed $true $false

                    $hcsMsg = "【WSL 仮想化基盤エラー (HCS_E_SERVICE_NOT_AVAILABLE)】`r`n`r`n" +
                              "必要な仮想化機能が Windows 上で動作していないため、コンテナのビルドを開始できませんでした。`r`n`r`n" +
                              "■ 最も多い原因:`r`n" +
                              "WSL や仮想マシンプラットフォームを有効化した後、【PC の再起動】が行われていません。`r`n`r`n" +
                              "■ 解決手順:`r`n" +
                              "1. PC を再起動してください。`r`n" +
                              "2. 再起動後も改善しない場合は、BIOS/UEFI で CPU の仮想化支援機能（Intel VT-x または AMD SVM）が有効になっているかご確認ください。`r`n`r`n" +
                              "今すぐ管理者権限で仮想化機能 (VirtualMachinePlatform) の再適用を実行しますか？"

                    $askFix = Show-AppConfirm -Message $hcsMsg -Title "仮想化基盤エラーの解決案内 - hayamimi"
                    if ($askFix -eq [System.Windows.MessageBoxResult]::Yes) {
                        Invoke-InstallWslContainers | Out-Null
                    }
                }
            }
        }
    } catch {
        $buildTimer.Stop()
        Write-AppLog "ビルド監視中に例外が発生しました: $_" "ERROR"
        Set-UIState "エラー" $brushRed $true $false
        if ($btnRebuild) { $btnRebuild.IsEnabled = $true }
    }
})

# ヘルスチェックタイマー (最大10分待機・初期化猶予付き異常終了検知)
$script:HealthCheckAttempts = 0
$script:ContainerDeadCount = 0
$script:ReadyConsecutiveCount = 0
$healthTimer = [System.Windows.Threading.DispatcherTimer]::new()
$healthTimer.Interval = [TimeSpan]::FromMilliseconds(500)

$healthTimer.Add_Tick({
    $script:HealthCheckAttempts++

    # 1. 生存チェック & ログ同期 (毎秒)
    if ($script:HealthCheckAttempts % 2 -eq 0) {
        if ($script:CurrentEngineMode -eq "Venv") {
            Update-PythonLogFile -EchoToGui $true

            # Python プロセス生存チェック
            $isPythonAlive = Test-PythonRunning
            if ($script:ProcPython -and $script:ProcPython.HasExited) {
                $isPythonAlive = $false
            }

            if (-not $isPythonAlive -and $script:HealthCheckAttempts -gt 10) {
                $healthTimer.Stop()
                if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
                Update-PythonLogFile -EchoToGui $true
                Set-UIState "Python異常停止" $brushRed $true $false
                Write-AppLog "==========================================" "ERROR"
                Write-AppLog "エラー: Python プロセスが異常終了しました。" "ERROR"
                if (Test-Path $script:PythonLogFile) {
                    $pLines = Read-FileSharedLines -FilePath $script:PythonLogFile
                    $tailPLines = ($pLines | Where-Object { $_.Trim() } | Select-Object -Last 20) -join "`r`n"
                    if ($tailPLines) {
                        Write-AppLog "【Python 最終出力ログ】:`r`n$tailPLines" "ERROR"
                    }
                }
                Write-AppLog "詳細ログは logs/python.log および logs/launcher.log をご確認ください。" "ERROR"
                if ($ProjectRoot -match "OneDrive|Documents") {
                    Write-AppLog "【ヒント】OneDrive または保護されたフォルダー配下で実行された場合、Windows セキュリティの「コントロールされたフォルダー アクセス」によりブロックされた可能性があります。" "WARN"
                    Write-AppLog "Windows セキュリティ > ウイルスと脅威の防止 > ランサムウェアの防止 でアプリを許可するか、OneDrive 以外のローカルドライブへの配置をご検討ください。" "WARN"
                }
                Write-AppLog "==========================================" "ERROR"
                return
            }
        } else {
            Update-ContainerLogFile -EchoToGui $true

            $isRunning = Test-ContainerRunning
            if ($isRunning) {
                $script:ContainerDeadCount = 0
            } else {
                # 起動直後の最初の15秒間 (attempts <= 30) は状態遷移・起動ラグ猶予のため加算しない
                if ($script:HealthCheckAttempts -gt 30) {
                    $script:ContainerDeadCount++
                }
            }

            # 連続 5 回 (約 5 秒間連続) で非生存が確認された場合のみ異常終了と判定
            if ($script:ContainerDeadCount -ge 5) {
                $healthTimer.Stop()
                if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
                Update-ContainerLogFile -EchoToGui $true
                Set-UIState "コンテナ異常停止" $brushRed $true $false
                Write-AppLog "==========================================" "ERROR"
                Write-AppLog "エラー: コンテナ ($ContainerName) が異常終了しました。" "ERROR"

                # 1. コンテナの終了状態 (ExitCode / State) を取得
                try {
                    $pinfoInsp = [System.Diagnostics.ProcessStartInfo]::new()
                    $pinfoInsp.FileName = $script:WslcCmd
                    $pinfoInsp.Arguments = "inspect $ContainerName"
                    $pinfoInsp.RedirectStandardOutput = $true
                    $pinfoInsp.UseShellExecute = $false
                    $pinfoInsp.CreateNoWindow = $true
                    $procInsp = [System.Diagnostics.Process]::Start($pinfoInsp)
                    $inspRaw = $procInsp.StandardOutput.ReadToEnd()
                    $procInsp.WaitForExit(2000) | Out-Null
                    if ($inspRaw) {
                        if ($inspRaw -match '"ExitCode"\s*:\s*(\d+)') {
                            Write-AppLog "コンテナ終了コード (ExitCode): $($Matches[1])" "ERROR"
                        }
                        if ($inspRaw -match '"OOMKilled"\s*:\s*(true|false)') {
                            Write-AppLog "メモリ不足強制停止 (OOMKilled): $($Matches[1])" "ERROR"
                        }
                    }
                } catch {}

                # 2. コンテナの標準エラー・標準出力ログを取得して表示
                try {
                    $pinfoFinal = [System.Diagnostics.ProcessStartInfo]::new()
                    $pinfoFinal.FileName = $script:WslcCmd
                    $pinfoFinal.Arguments = "logs $ContainerName"
                    $pinfoFinal.RedirectStandardOutput = $true
                    $pinfoFinal.RedirectStandardError = $true
                    $pinfoFinal.UseShellExecute = $false
                    $pinfoFinal.CreateNoWindow = $true
                    $procFinal = [System.Diagnostics.Process]::Start($pinfoFinal)
                    $finalRaw = $procFinal.StandardOutput.ReadToEnd()
                    $finalErr = $procFinal.StandardError.ReadToEnd()
                    $procFinal.WaitForExit(2000) | Out-Null
                    
                    $combinedLogs = ($finalRaw + "`r`n" + $finalErr).Trim()
                    if ($combinedLogs) {
                        $tailLines = ($combinedLogs -split "[\r\n]+" | Where-Object { $_.Trim() } | Select-Object -Last 25) -join "`r`n"
                        if ($tailLines) {
                            Write-AppLog "【コンテナ終了時の最終出力ログ】:`r`n$tailLines" "ERROR"
                        }
                    }
                } catch {}
                Write-AppLog "詳細ログは logs/container.log および logs/launcher.log をご確認ください。" "ERROR"
                Write-AppLog "==========================================" "ERROR"
                return
            }
        }
    }

    # 2. 定期的なステータス表示更新 (10秒ごと)
    if ($script:HealthCheckAttempts % 20 -eq 0) {
        $elapsedSec = [int]($script:HealthCheckAttempts / 2)
        Set-UIState "準備中 (${elapsedSec}s)..." $brushOrange $false $true
        if ($elapsedSec % 30 -eq 0) {
            Write-AppLog "サーバー初期化中 (${elapsedSec}秒経過)... モデルロード完了までそのままお待ちください。" "INFO"
        }
    }

    # 3. サーバー疎通確認 (HTTP /config が 200 OK かつ WebSocket 8766 がリッスン中であることを検証)
    # ※ モデルロード完了後に server.controls がセットされ、初めて /config が 200 OK になる。
    #    この検証により「モデルロード中・サーバー未準備のフライング起動」を完全防止する。
    $isHttpReady = $false
    $isWsReady = $false

    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:8833/config")
        $req.Timeout = 800
        $req.Method = "GET"
        $resp = $req.GetResponse()
        if ($resp) {
            $httpResp = [System.Net.HttpWebResponse]$resp
            if ($httpResp.StatusCode -eq [System.Net.HttpStatusCode]::OK) {
                $isHttpReady = $true
            }
            $httpResp.Close()
        }
    } catch {}

    if ($isHttpReady) {
        try {
            # 8766 (WebSocket: モデルロード完了後に IngestServer が起動) のソケット疎通確認
            $tcpWs = [System.Net.Sockets.TcpClient]::new()
            $asyncWs = $tcpWs.BeginConnect("127.0.0.1", 8766, $null, $null)
            $connectedWs = $asyncWs.AsyncWaitHandle.WaitOne(600, $false)
            if ($connectedWs) {
                $tcpWs.EndConnect($asyncWs)
                $tcpWs.Close()
                $isWsReady = $true
            } else {
                $tcpWs.Close()
            }
        } catch {}
    }

    if ($isHttpReady -and $isWsReady) {
        $script:ReadyConsecutiveCount++
    } else {
        $script:ReadyConsecutiveCount = 0
    }

    # 2回連続確認 (約1秒間安定稼働) で起動完了と判定
    if ($script:ReadyConsecutiveCount -ge 2) {
        $healthTimer.Stop()
        if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
        if ($script:CurrentEngineMode -eq "Venv") {
            Update-PythonLogFile -EchoToGui $true
        } else {
            Update-ContainerLogFile -EchoToGui $true
        }
        Set-UIState "稼働中 (8833 / 8766)" $brushGreen $false $true
        Write-AppLog "✔ サーバーの全モジュール（モデルロード/HTTP/WebSocket）の起動が完了しました！" "INFO"
        Write-AppLog "ブラウザ（MICページ）を開きます: $MicUrl" "INFO"

        # リスナーが完全に安定するまで 500ms 待機
        Start-Sleep -Milliseconds 500

        try {
            Start-Process $MicUrl -ErrorAction Stop
        } catch {
            cmd.exe /c start "" "$MicUrl"
        }
    } elseif ($script:HealthCheckAttempts -ge 1200) {
        # 10分 (1200 * 0.5s = 600s) 経過でタイムアウト
        $healthTimer.Stop()
        if ($initProgressBar) { $initProgressBar.Visibility = "Collapsed" }
        if ($script:CurrentEngineMode -eq "Venv") {
            Update-PythonLogFile -EchoToGui $true
        } else {
            Update-ContainerLogFile -EchoToGui $true
        }
        Set-UIState "待機タイムアウト" $brushRed $true $true
        Write-AppLog "サーバーの応答待機がタイムアウトしました (10分経過)。logs フォルダ内のログをご確認ください。" "ERROR"
    }
})

# 起動ボタン処理
$btnStart.Add_Click({
    try {
        if ($script:CurrentEngineMode -eq "Venv") {
            # Python venv モード (3.14 整合性確認 & 3.11 からの自動再構築)
            $isOk = Ensure-PythonVenvUpToDate
            if (-not $isOk) { return }
            Start-PythonVenvApp
        } else {
            # WSL Containers モード
            # 1. 仮想化基盤 (Hypervisor / BIOS VT-x / HCS) チェック
            $diag = Get-VirtualizationDiagnostic
            if (-not $diag.IsReady) {
                Update-EngineBannerState | Out-Null
                if ($diag.Reason -eq "BiosDisabled") {
                    Show-BiosVirtualizationGuide -Diag $diag
                    return
                } elseif ($diag.Reason -eq "HcsServiceUnavailable") {
                    $askHcs = Show-AppConfirm -Message "仮想化基盤サービス (HCS) が未準備です。`r`n`r`n【最も多い原因】`r`nWSL や仮想化機能のインストール後に【PCの再起動】が行われていません。`r`n`r`nPCを再起動してから再度お試しください。`r`n`r`n今すぐ仮想化機能の修復を実行しますか？" -Title "仮想化基盤の確認 - hayamimi"
                    if ($askHcs -eq [System.Windows.MessageBoxResult]::Yes) {
                        Invoke-InstallWslContainers | Out-Null
                    }
                    return
                } else {
                    $askVmp = Show-AppConfirm -Message "Windows の仮想化基盤 (VirtualMachinePlatform) が無効、またはPCの再起動待ちです。`r`n`r`n今すぐ全自動で仮想化基盤と WSL 3.0 を有効化しますか？`r`n(管理者権限の設定画面が開きます)" -Title "仮想化基盤の有効化 - hayamimi"
                    if ($askVmp -eq [System.Windows.MessageBoxResult]::Yes) {
                        Invoke-InstallWslContainers | Out-Null
                    }
                    return
                }
            }

            # 2. WSL Containers (wslc.exe) 存在確認
            $script:WslcCmd = Get-WslcCommand
            if (-not $script:WslcCmd) {
                Update-EngineBannerState | Out-Null
                Set-UIState "WSL3 未検出" $brushOrange $true $false
                Write-AppLog "【案内】WSL 3.0 / WSL Containers (wslc.exe) が未インストールです。" "WARN"

                $msgRes = Show-AppConfirm -Message "hayamimi を動かすには WSL 3.0 (WSL Containers) が必要です。`r`n`r`n今すぐ自動でインストールを開始しますか？" -Title "WSL 3.0 の自動インストール - hayamimi"
                if ($msgRes -eq [System.Windows.MessageBoxResult]::Yes) {
                    $installed = Invoke-InstallWslContainers
                    if (-not $installed) { return }
                } else {
                    return
                }
            }

            # 3. コンテナイメージの存在確認 (未ビルド環境の場合は全自動ビルドして起動)
            if (-not (Test-HayamimiImageExists)) {
                Write-AppLog "【初回検知】hayamimi コンテナイメージが未構築です。自動ビルドを開始します..." "INFO"
                Invoke-BuildContainerImage -Force $false -AutoStart $true | Out-Null
                return
            }

            # 4. 通常起動
            Start-HayamimiContainer
        }
    } catch {
        Write-AppLog "起動ボタン処理エラー: $_" "ERROR"
        Set-UIState "エラー" $brushRed $true $false
    }
})

# 本体アップデートボタン処理 (GitHubからの最新コード取得)
if ($btnUpdateApp) {
    $btnUpdateApp.Add_Click({
        $hasNew = Test-AppUpdate -PromptUser
        if (-not $hasNew) {
            $ask = Show-AppConfirm -Message "現在は最新バージョン (v$script:AppVersion) です。`r`n`r`nGitHub からソースコードを再取得（強制更新）しますか？" -Title "本体アップデート - hayamimi"
            if ($ask -eq [System.Windows.MessageBoxResult]::Yes) {
                Invoke-UpdateAppCode -AutoConfirm
            }
        }
    })
}

# 再構築・リビルドボタン処理
if ($btnRebuild) {
    $btnRebuild.Add_Click({
        try {
            if ($script:CurrentEngineMode -eq "Venv") {
                $confirm = Show-AppConfirm -Message "Python venv 環境の依存パッケージ (requirements.txt) を再セットアップしますか？" -Title "環境セットアップ - hayamimi"
                if ($confirm -eq [System.Windows.MessageBoxResult]::Yes) {
                    Invoke-SetupPythonVenv -ForceRecreate $false | Out-Null
                }
            } else {
                $confirm = Show-AppConfirm -Message "最新のソースコードをコンテナイメージに反映（リビルド）しますか？`r`n`r`n※ 実行には数分かかる場合があります。" -Title "コンテナのリビルド - hayamimi"
                if ($confirm -eq [System.Windows.MessageBoxResult]::Yes) {
                    Invoke-BuildContainerImage -Force $true -AutoStart $false | Out-Null
                }
            }
        } catch {
            Write-AppLog "再構築処理中にエラーが発生しました: $_" "ERROR"
            Set-UIState "エラー" $brushRed $true $false
            if ($btnRebuild) { $btnRebuild.IsEnabled = $true }
        }
    })
}

# 再起動ボタン処理 (残留プロセス解放・クリーンアップ付き)
if ($btnRestart) {
    $btnRestart.Add_Click({
        Invoke-RestartApp
    })
}

# --- 終了時 辞書自動メンテナンス確認ダイアログ & シャットダウンフロー ---
$script:MaintenanceExitChoice = "CANCEL"

function Show-MaintenanceExitDialog {
    param(
        [int]$PendingVideosCount = 0,
        [int]$StaleRulesCount = 0
    )

    $script:MaintenanceExitChoice = "CANCEL"

    $dlgXaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="辞書メンテナンス確認 - hayamimi"
        Width="510" SizeToContent="Height"
        WindowStartupLocation="CenterOwner"
        WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="NoResize" ShowInTaskbar="False">
    <Border Background="#0F172A" BorderBrush="#334155" BorderThickness="1.5" CornerRadius="12" Margin="10">
        <Border.Effect>
            <DropShadowEffect BlurRadius="24" ShadowDepth="4" Opacity="0.6" Color="#000000"/>
        </Border.Effect>
        <Grid Margin="24">
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>

            <!-- ヘッダー (ドラッグ移動可能) -->
            <Grid x:Name="DlgHeader" Grid.Row="0" Margin="0,0,0,16" Background="Transparent" Cursor="SizeAll">
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <Border Grid.Column="0" Background="#0369A1" CornerRadius="8" Width="40" Height="40" Margin="0,0,14,0">
                    <TextBlock Text="🛠️" FontSize="20" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                </Border>
                <StackPanel Grid.Column="1" VerticalAlignment="Center">
                    <TextBlock Text="辞書の自動更新・整理の確認" FontSize="16" FontWeight="Bold" Foreground="#F8FAFC"/>
                    <TextBlock Text="未処理の自動学習・整理キューがあります" FontSize="12" Foreground="#94A3B8" Margin="0,2,0,0"/>
                </StackPanel>
            </Grid>

            <!-- プロンプトパネル (通常時) -->
            <StackPanel x:Name="PanelPrompt" Grid.Row="1">
                <TextBlock Text="最新のYouTube動画からの語彙学習、および180日以上未ヒットの古いルールの休眠化を安全に実行できます。終了前にメンテナンスを実行しますか？"
                           FontSize="13" Foreground="#CBD5E1" TextWrapping="Wrap" LineHeight="19" Margin="0,0,0,14"/>
                
                <Border Background="#1E293B" BorderBrush="#334155" BorderThickness="1" CornerRadius="8" Padding="14" Margin="0,0,0,16">
                    <StackPanel>
                        <Grid Margin="0,0,0,8">
                            <TextBlock Text="🎥 新着動画からの語彙抽出・マージ" FontSize="13" Foreground="#E2E8F0"/>
                            <TextBlock x:Name="TxtPendingVideos" Text="0 件" FontSize="13" FontWeight="Bold" Foreground="#38BDF8" HorizontalAlignment="Right"/>
                        </Grid>
                        <Grid>
                            <TextBlock Text="📦 180日未ヒットルールの休眠化整理" FontSize="13" Foreground="#E2E8F0"/>
                            <TextBlock x:Name="TxtStaleRules" Text="0 件" FontSize="13" FontWeight="Bold" Foreground="#F59E0B" HorizontalAlignment="Right"/>
                        </Grid>
                    </StackPanel>
                </Border>

                <!-- ボタンエリア -->
                <StackPanel Orientation="Vertical">
                    <Button x:Name="BtnMaintainAndExit" Height="42" Margin="0,0,0,8" Cursor="Hand"
                            Background="#0284C7" Foreground="#FFFFFF" BorderThickness="0">
                        <Button.Resources>
                            <Style TargetType="Border">
                                <Setter Property="CornerRadius" Value="8"/>
                            </Style>
                        </Button.Resources>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Center">
                            <TextBlock Text="🔧" FontSize="15" Margin="0,0,8,0" VerticalAlignment="Center"/>
                            <TextBlock Text="メンテナンスを実行して終了 (推奨)" FontSize="13.5" FontWeight="Bold" VerticalAlignment="Center"/>
                        </StackPanel>
                    </Button>

                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="10"/>
                            <ColumnDefinition Width="*"/>
                        </Grid.ColumnDefinitions>
                        <Button x:Name="BtnImmediateExit" Grid.Column="0" Height="36" Cursor="Hand"
                                Background="#334155" Foreground="#F1F5F9" BorderThickness="0">
                            <Button.Resources>
                                <Style TargetType="Border">
                                    <Setter Property="CornerRadius" Value="6"/>
                                </Style>
                            </Button.Resources>
                            <TextBlock Text="⚡ 今すぐ終了" FontSize="12.5" FontWeight="SemiBold"/>
                        </Button>
                        <Button x:Name="BtnCancelExit" Grid.Column="2" Height="36" Cursor="Hand"
                                Background="Transparent" Foreground="#94A3B8" BorderBrush="#475569" BorderThickness="1">
                            <Button.Resources>
                                <Style TargetType="Border">
                                    <Setter Property="CornerRadius" Value="6"/>
                                </Style>
                            </Button.Resources>
                            <TextBlock Text="キャンセル" FontSize="12.5"/>
                        </Button>
                    </Grid>
                </StackPanel>
            </StackPanel>

            <!-- プログレスパネル (実行中) -->
            <StackPanel x:Name="PanelProgress" Grid.Row="2" Visibility="Collapsed" Margin="0,4,0,4">
                <TextBlock x:Name="TxtProgressStatus" Text="辞書の自動メンテナンスを実行中..." FontSize="13.5" Foreground="#38BDF8" FontWeight="SemiBold" Margin="0,0,0,10"/>
                <ProgressBar x:Name="PbMaintenance" Height="8" IsIndeterminate="True" Background="#1E293B" Foreground="#0284C7" BorderThickness="0" Margin="0,0,0,12">
                    <ProgressBar.Resources>
                        <Style TargetType="Border">
                            <Setter Property="CornerRadius" Value="4"/>
                        </Style>
                    </ProgressBar.Resources>
                </ProgressBar>
                <TextBlock x:Name="TxtProgressDetail" Text="新着動画から語彙を抽出・自動マージしています..." FontSize="12" Foreground="#94A3B8" TextWrapping="Wrap"/>
            </StackPanel>
        </Grid>
    </Border>
</Window>
'@

    try {
        $dlg = [System.Windows.Markup.XamlReader]::Parse($dlgXaml)
        if ($window -and $window.IsVisible) {
            $dlg.Owner = $window
        }

        $header = $dlg.FindName("DlgHeader")
        $pPrompt = $dlg.FindName("PanelPrompt")
        $pProgress = $dlg.FindName("PanelProgress")
        $txtVideos = $dlg.FindName("TxtPendingVideos")
        $txtStale = $dlg.FindName("TxtStaleRules")
        $btnMaintain = $dlg.FindName("BtnMaintainAndExit")
        $btnExit = $dlg.FindName("BtnImmediateExit")
        $btnCancel = $dlg.FindName("BtnCancelExit")
        $txtStatus = $dlg.FindName("TxtProgressStatus")
        $txtDetail = $dlg.FindName("TxtProgressDetail")

        if ($txtVideos) { $txtVideos.Text = "$PendingVideosCount 件" }
        if ($txtStale) { $txtStale.Text = "$StaleRulesCount 件" }

        if ($header) {
            $header.Add_MouseLeftButtonDown({
                param($eventSender, $e)
                try { $dlg.DragMove() } catch {}
            })
        }

        # キャンセルボタン
        $btnCancel.Add_Click({
            $script:MaintenanceExitChoice = "CANCEL"
            $dlg.DialogResult = $false
            $dlg.Close()
        })

        # 今すぐ終了ボタン
        $btnExit.Add_Click({
            $script:MaintenanceExitChoice = "FORCE_EXIT"
            $dlg.DialogResult = $true
            $dlg.Close()
        })

        # メンテナンス実行ボタン
        $btnMaintain.Add_Click({
            $pPrompt.Visibility = "Collapsed"
            $pProgress.Visibility = "Visible"
            $txtStatus.Text = "辞書の自動メンテナンスを実行中..."
            $txtDetail.Text = "新着動画から語彙を抽出・自動マージしています..."

            # API 実行
            try {
                Invoke-RestMethod -Uri "http://localhost:8833/api/maintenance/run" -Method Post -ContentType "application/json" -Body "{}" -TimeoutSec 5 -ErrorAction SilentlyContinue | Out-Null
            } catch {
                Write-AppLog "メンテナンスAPIのキックに失敗しました: $_" "WARN"
            }

            # ポーリングタイマー
            $pollTimer = [System.Windows.Threading.DispatcherTimer]::new()
            $pollTimer.Interval = [TimeSpan]::FromMilliseconds(800)
            $script:maintPollElapsed = 0

            $pollTimer.Add_Tick({
                $script:maintPollElapsed += 1
                try {
                    $st = Invoke-RestMethod -Uri "http://localhost:8833/api/maintenance/status" -Method Get -TimeoutSec 3 -ErrorAction Stop
                    if ($st) {
                        $msg = if ($st.message) { $st.message } elseif ($st.run_message) { $st.run_message } else { $null }
                        if ($msg) {
                            $txtDetail.Text = $msg
                        }
                        $isCompleted = ($st.status -eq "completed") -or (-not $st.running -and $script:maintPollElapsed -gt 2 -and ($st.last_result -or $st.pending_count -eq 0))
                        $isError = ($st.status -eq "error") -or ($st.error -and -not $st.running)

                        if ($isCompleted) {
                            $pollTimer.Stop()
                            $txtStatus.Text = "✅ メンテナンスが完了しました"
                            $txtDetail.Text = if ($msg) { $msg } else { "最新の辞書ルールを保存しました。終了します..." }
                            $closeTimer = [System.Windows.Threading.DispatcherTimer]::new()
                            $closeTimer.Interval = [TimeSpan]::FromMilliseconds(700)
                            $closeTimer.Add_Tick({
                                $closeTimer.Stop()
                                $script:MaintenanceExitChoice = "MAINTAIN"
                                $dlg.DialogResult = $true
                                $dlg.Close()
                            })
                            $closeTimer.Start()
                            return
                        } elseif ($isError) {
                            $pollTimer.Stop()
                            $txtStatus.Text = "⚠️ メンテナンス中にエラーが発生しました"
                            $txtDetail.Text = if ($st.error) { "$($st.error)。そのまま終了します..." } else { "エラーが発生したため、そのまま終了します..." }
                            $closeTimer = [System.Windows.Threading.DispatcherTimer]::new()
                            $closeTimer.Interval = [TimeSpan]::FromMilliseconds(1000)
                            $closeTimer.Add_Tick({
                                $closeTimer.Stop()
                                $script:MaintenanceExitChoice = "MAINTAIN"
                                $dlg.DialogResult = $true
                                $dlg.Close()
                            })
                            $closeTimer.Start()
                            return
                        }
                    }
                } catch {}

                # 最大 300 秒でタイムアウト (動画DLとGemini分析を考慮)
                if ($script:maintPollElapsed -gt 375) {
                    $pollTimer.Stop()
                    $txtStatus.Text = "⏱️ メンテナンスがタイムアウトしました"
                    $txtDetail.Text = "処理を中断し、終了します..."
                    $script:MaintenanceExitChoice = "MAINTAIN"
                    $dlg.DialogResult = $true
                    $dlg.Close()
                }
            })
            $pollTimer.Start()
        })

        $dlg.ShowDialog() | Out-Null
        return $script:MaintenanceExitChoice
    } catch {
        Write-AppLog "メンテナンス確認ダイアログの表示中にエラーが発生しました: $_" "WARN"
        return "FORCE_EXIT"
    }
}

$script:IsShuttingDown = $false

function Invoke-ShutdownApp {
    param([bool]$Force = $false)

    if ($script:IsShuttingDown) { return }
    $script:IsShuttingDown = $true

    try {
        $isRunning = Test-AppRunning
        if (-not $isRunning -or $Force) {
            $script:IsClosing = $true
            $window.Close()
            return
        }

        # 1. ASRリアルタイム統計をプロファイルへフラッシュ同期
        try {
            Invoke-RestMethod -Uri "http://localhost:8833/api/profiles/sync-stats" -Method Post -ContentType "application/json" -Body "{}" -TimeoutSec 2 -ErrorAction SilentlyContinue | Out-Null
        } catch {}

        # 2. メンテナンスキュー状態の確認
        $pendingVideos = 0
        $staleRules = 0
        try {
            $mStatus = Invoke-RestMethod -Uri "http://localhost:8833/api/maintenance/status" -Method Get -TimeoutSec 3 -ErrorAction SilentlyContinue

            # 起動直後等でスキャン中の場合は最大2秒間完了を待機
            $waitCount = 0
            while ($mStatus -and $mStatus.scanning -and $waitCount -lt 4) {
                Start-Sleep -Milliseconds 500
                $mStatus = Invoke-RestMethod -Uri "http://localhost:8833/api/maintenance/status" -Method Get -TimeoutSec 2 -ErrorAction SilentlyContinue
                $waitCount++
            }

            if ($mStatus) {
                if ($null -ne $mStatus.pending_count) {
                    $pendingVideos = [int]$mStatus.pending_count
                } elseif ($null -ne $mStatus.total_pending_videos) {
                    $pendingVideos = [int]$mStatus.total_pending_videos
                } elseif ($null -ne $mStatus.pending_videos) {
                    $pendingVideos = [int]@($mStatus.pending_videos).Count
                }

                if ($null -ne $mStatus.stale_rules_count) {
                    $staleRules = [int]$mStatus.stale_rules_count
                } elseif ($null -ne $mStatus.total_stale_rules) {
                    $staleRules = [int]$mStatus.total_stale_rules
                }
            }
        } catch {}

        # 3. キューがある場合はユーザー選択ダイアログを表示
        if ($pendingVideos -gt 0 -or $staleRules -gt 0) {
            $choice = Show-MaintenanceExitDialog -PendingVideosCount $pendingVideos -StaleRulesCount $staleRules
            if ($choice -eq "CANCEL") {
                $script:IsShuttingDown = $false
                return
            }
        } else {
            Write-AppLog "終了前メンテナンス確認: 未処理の学習・整理キューはありませんでした (即時終了)"
        }

        # 4. サービスの安全停止
        if ($healthTimer -and $healthTimer.IsEnabled) { $healthTimer.Stop() }
        Set-UIState "停止処理中..." $brushOrange $false $false

        if ($script:CurrentEngineMode -eq "Venv") {
            Stop-PythonVenvApp
        } else {
            Stop-HayamimiContainer
        }

        Cleanup-ResidualProcesses -LogOutput $false

        $script:IsClosing = $true
        $window.Close()
    } catch {
        Write-AppLog "終了処理中にエラーが発生しました: $_" "ERROR"
        $script:IsClosing = $true
        $window.Close()
    }
}

# 終了ボタン処理
$btnStop.Add_Click({
    Invoke-ShutdownApp
})

# 実行基盤ラジオボタン処理 (稼働中安全停止確認付き)
if ($rbModeVenv) {
    $rbModeVenv.Add_Checked({
        if ($script:IsInitializing) { return }
        Invoke-EngineModeSwitch -TargetMode "Venv" -TargetRb $rbModeVenv -OtherRb $rbModeWslc
    })
}
if ($rbModeWslc) {
    $rbModeWslc.Add_Checked({
        if ($script:IsInitializing) { return }
        Invoke-EngineModeSwitch -TargetMode "Wslc" -TargetRb $rbModeWslc -OtherRb $rbModeVenv
    })
}

# 推論デバイスラジオボタン処理 (稼働中安全停止確認付き)
if ($rbDeviceCpu) {
    $rbDeviceCpu.Add_Checked({
        if ($script:IsInitializing) { return }
        Invoke-DeviceModeSwitch -TargetMode "Cpu" -TargetRb $rbDeviceCpu -OtherRb $rbDeviceNpu
    })
}
if ($rbDeviceNpu) {
    $rbDeviceNpu.Add_Checked({
        if ($script:IsInitializing) { return }
        Invoke-DeviceModeSwitch -TargetMode "Npu" -TargetRb $rbDeviceNpu -OtherRb $rbDeviceCpu
    })
}

# 常設 WSL 3.0 セットアップボタンの処理
if ($btnInstallWslAlways) {
    $btnInstallWslAlways.Add_Click({
        try {
            Invoke-InstallWslContainers | Out-Null
            Update-EngineBannerState | Out-Null
        } catch {
            Write-AppLog "WSL3 セットアップ処理エラー: $_" "ERROR"
        }
    })
}

# Python 環境セットアップボタンの処理
if ($btnSetupEnv) {
    $btnSetupEnv.Add_Click({
        Invoke-SetupPythonVenv | Out-Null
    })
}

# BIOS 設定ガイドボタンの処理
if ($btnBiosGuide) {
    $btnBiosGuide.Add_Click({
        try {
            Show-BiosVirtualizationGuide -Diag (Get-VirtualizationDiagnostic)
        } catch {
            Write-AppLog "BIOS ガイド表示エラー: $_" "ERROR"
        }
    })
}

# WSL & 仮想化 自動セットアップ / Python セットアップ ボタンの処理
if ($btnUpdateWsl) {
    $btnUpdateWsl.Add_Click({
        try {
            if ($script:CurrentEngineMode -eq "Venv") {
                Invoke-SetupPythonVenv | Out-Null
            } else {
                Invoke-InstallWslContainers | Out-Null
                Update-EngineBannerState | Out-Null
            }
        } catch {
            Write-AppLog "セットアップ処理エラー: $_" "ERROR"
        }
    })
}

# クイックリンク
$btnOpenMic.Add_Click({ Start-Process $MicUrl })
$btnOpenObs.Add_Click({ Start-Process $ObsUrl })
$btnOpenDashboard.Add_Click({ Start-Process $DashboardUrl })
if ($btnOpenProfile) {
    $btnOpenProfile.Add_Click({ Start-Process $ProfileUrl })
}
if ($btnOpenOverlay) {
    $btnOpenOverlay.Add_Click({
        $overlayBat = Join-Path $PSScriptRoot "..\desktop_overlay.bat"
        if (Test-Path $overlayBat) {
            Start-Process -FilePath $overlayBat
        } else {
            Start-Process -FilePath $PS_CMD -ArgumentList "-STA -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\desktop_overlay.ps1`""
        }
        Write-AppLog "🖥️ デスクトップ透過字幕オーバーレイを起動しました。" "INFO"
    })
}
$btnClearLog.Add_Click({ $logTextBox.Clear() })

if ($btnOpenPythonLog) {
    $btnOpenPythonLog.Add_Click({
        if (Test-Path $script:PythonLogFile) {
            # Windows 11 メモ帳の旧 TabState キャッシュ (UTF-16LE 誤認タブ) をクリーンアップ
            try {
                $tabFiles = Get-ChildItem -Path "$env:LOCALAPPDATA\Packages\Microsoft.WindowsNotepad*\LocalState\TabState\*.bin" -ErrorAction SilentlyContinue
                $targetBytes = [System.Text.Encoding]::Unicode.GetBytes("python.log")
                foreach ($tf in $tabFiles) {
                    if ($tf.Name -notmatch "\.0\.bin$|\.1\.bin$") {
                        $raw = [System.IO.File]::ReadAllBytes($tf.FullName)
                        $found = $false
                        for ($k = 0; $k -le ($raw.Length - $targetBytes.Length); $k++) {
                            $match = $true
                            for ($j = 0; $j -lt $targetBytes.Length; $j++) {
                                if ($raw[$k + $j] -ne $targetBytes[$j]) { $match = $false; break }
                            }
                            if ($match) { $found = $true; break }
                        }
                        if ($found) {
                            $baseId = $tf.Name.Split('.')[0]
                            Remove-Item -Path (Join-Path $tf.DirectoryName "$baseId*") -Force -ErrorAction SilentlyContinue
                        }
                    }
                }
            } catch {}
            Start-Process $script:PythonLogFile
        } else {
            Write-AppLog "python.log はまだ生成されていません。" "WARN"
        }
    })
}
$btnOpenContainerLog.Add_Click({
    if (Test-Path $ContainerLogFile) {
        Start-Process $ContainerLogFile
    } else {
        Write-AppLog "container.log はまだ生成されていません。" "WARN"
    }
})
$btnOpenLauncherLog.Add_Click({
    if (Test-Path $LogFile) {
        Start-Process $LogFile
    }
})
$btnOpenLogsDir.Add_Click({
    if (Test-Path $LogsDir) {
        Start-Process "explorer.exe" $LogsDir
    }
})

# ウィンドウクローズ処理
$script:IsClosing = $false
$window.Add_Closing({
    param($eventSender, $e)
    if ($script:IsClosing) { return }

    if (Test-PythonRunning -or Test-ContainerRunning) {
        $e.Cancel = $true
        Invoke-ShutdownApp
        return
    }

    # 終了直前に最新のランチャー設定 (実行基盤 & 推論設定) を確実に永続化
    try {
        Save-LauncherConfig -EngineMode $script:CurrentEngineMode -DeviceMode $script:CurrentDeviceMode
    } catch {}

    $script:IsClosing = $true
    if ($updateCheckTimer -and $updateCheckTimer.IsEnabled) {
        $updateCheckTimer.Stop()
    }
    if ($buildTimer -and $buildTimer.IsEnabled) {
        $buildTimer.Stop()
    }
    if ($script:ProcBuild -and -not $script:ProcBuild.HasExited) {
        try { $script:ProcBuild.Kill() } catch {}
    }
})

# 定期ポーリングタイマー (ログ同期 & 稼働チェック)
$pollTimer = [System.Windows.Threading.DispatcherTimer]::new()
$pollTimer.Interval = [TimeSpan]::FromSeconds(3)
$pollTimer.Add_Tick({
    if (-not $healthTimer.IsEnabled -and -not $script:IsClosing) {
        $isRunning = Test-AppRunning
        if ($isRunning) {
            if ($script:CurrentEngineMode -eq "Venv") {
                Update-PythonLogFile -EchoToGui $true
            } else {
                Update-ContainerLogFile -EchoToGui $true
            }
            if ($statusText.Text -notmatch "稼働中") {
                Set-UIState "稼働中 (8833 / 8766)" $brushGreen $false $true
            }
        } elseif (-not $isRunning -and $statusText.Text -match "稼働中") {
            Set-UIState "停止中" $brushGray $true $false
        }

        # 外部プロセス (デスクトップ字幕など) が launcher.log に追記した新着ログを GUI コンソールに反映
        try {
            if (Test-Path $LogFile) {
                $fi = Get-Item $LogFile
                if ($null -eq $script:LastLauncherLogPos) {
                    $script:LastLauncherLogPos = $fi.Length
                } elseif ($fi.Length -gt $script:LastLauncherLogPos) {
                    $fs = [System.IO.FileStream]::new($LogFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
                    $fs.Seek($script:LastLauncherLogPos, [System.IO.SeekOrigin]::Begin) | Out-Null
                    $sr = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8)
                    $newText = $sr.ReadToEnd()
                    $script:LastLauncherLogPos = $fs.Position
                    $sr.Dispose()
                    $fs.Dispose()

                    if (-not [string]::IsNullOrWhiteSpace($newText)) {
                        $lines = $newText -split "`r?`n"
                        foreach ($l in $lines) {
                            if ($l -match "\[デスクトップ字幕\]") {
                                if ($logTextBox -and $logScrollViewer) {
                                    $timeMatch = [regex]::Match($l, "\[(\d{4}-\d{2}-\d{2}\s+)?(\d{2}:\d{2}:\d{2})\]")
                                    $timePrefix = if ($timeMatch.Success) { "[" + $timeMatch.Groups[2].Value + "] " } else { "" }
                                    $cleanMsg = $l -replace "^\[.*?\]\s*\[.*?\]\s*", ""
                                    $logTextBox.AppendText("$timePrefix$cleanMsg`r`n")
                                    Trim-LogTextBox
                                    $logScrollViewer.ScrollToEnd()
                                }
                            }
                        }
                    }
                } elseif ($fi.Length -lt $script:LastLauncherLogPos) {
                    $script:LastLauncherLogPos = 0
                }
            }
        } catch {}
    }
})

# ログファイルの事前初期化
if (-not (Test-Path $ContainerLogFile)) {
    try {
        [System.IO.File]::WriteAllText($ContainerLogFile, "# hayamimi コンテナログ`r`n# コンテナ起動時に自動記録されます。`r`n", [System.Text.Encoding]::UTF8)
    } catch {}
}
if (-not (Test-Path $script:PythonLogFile)) {
    try {
        [System.IO.File]::WriteAllText($script:PythonLogFile, "# hayamimi Pythonログ`r`n# Python venv 起動時に自動記録されます。`r`n", [System.Text.Encoding]::UTF8)
    } catch {}
}

# 初期化処理
$script:IsInitializing = $true

# 1. 実行基盤の初期化 & 復元
$initMode = Get-InitialEngineMode
Set-EngineMode $initMode
if ($initMode -eq "Wslc") {
    if ($rbModeWslc) { $rbModeWslc.IsChecked = $true }
    if ($rbModeVenv) { $rbModeVenv.IsChecked = $false }
} else {
    if ($rbModeVenv) { $rbModeVenv.IsChecked = $true }
    if ($rbModeWslc) { $rbModeWslc.IsChecked = $false }
}

# 2. 推論デバイスの検出 & 復元
Update-GpuDeviceBadge
$initDevMode = Get-SavedDeviceMode
if ($initDevMode -eq "Npu" -and $script:HasNpu) {
    if ($rbDeviceNpu) { $rbDeviceNpu.IsChecked = $true }
    if ($rbDeviceCpu) { $rbDeviceCpu.IsChecked = $false }
    Set-DeviceMode "Npu"
} else {
    if ($rbDeviceCpu) { $rbDeviceCpu.IsChecked = $true }
    if ($rbDeviceNpu) { $rbDeviceNpu.IsChecked = $false }
    Set-DeviceMode "Cpu"
}

$script:IsInitializing = $false
$script:NeedModelRepairOnStartup = $false
if (Test-ModelsReady) {
    Protect-ModelFiles
    Write-AppLog "✔ Core AI モデル実体を確認しました (完全ポータブル保持)" "INFO"
} else {
    Write-AppLog "⚠ Core AI モデルが未配置です。ウィンドウ描画完了後に自動修復を開始します..." "WARN"
    $script:NeedModelRepairOnStartup = $true
}

Write-AppLog "=========================================="
Write-AppLog "hayamimi ランチャー $version を起動しました ($($runtimeBadge.Text))."
Write-AppLog "現在の実行基盤モード: $script:CurrentEngineMode"
Write-AppLog "現在の推論設定モード: $script:CurrentDeviceMode (既定: CPU)"
Write-AppLog "ランチャーログ: $LogFile"
Write-AppLog "Pythonログ:     $script:PythonLogFile"
Write-AppLog "コンテナログ:   $ContainerLogFile"

if ($script:CurrentEngineMode -eq "Venv") {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        Write-AppLog "✔ Python 仮想環境 (.venv) 検出: 準備完了"
    } else {
        Write-AppLog "【案内】Python 仮想環境 (.venv) が未構築です。「📥 Python環境を自動構築」または起動ボタンを押してください。" "WARN"
    }
} else {
    Write-AppLog "WSL Containers (WSL3) および仮想化環境を確認中..."
    $isEnvReady = Update-EngineBannerState
    if ($isEnvReady) {
        Write-AppLog "✔ WSL Containers 検出: $script:WslcCmd"
        Write-AppLog "✔ Hypervisor 仮想化基盤: 正常稼働中"
    } else {
        $diag = Get-VirtualizationDiagnostic
        if (-not $diag.IsReady) {
            if ($diag.Reason -eq "BiosDisabled") {
                Write-AppLog "【警告】CPU ハードウェア仮想化 ($($diag.CpuVendor): VT-x / AMD-V) が BIOS で無効です。" "WARN"
            } elseif ($diag.Reason -eq "HcsServiceUnavailable") {
                Write-AppLog "【警告】仮想化基盤サービス (HCS) が未準備です (エラー: HCS_E_SERVICE_NOT_AVAILABLE)。" "WARN"
            } else {
                Write-AppLog "【警告】Windows 仮想化機能 (VirtualMachinePlatform) が無効、または再起動待ちです。" "WARN"
            }
        } elseif (-not $script:WslcCmd) {
            Write-AppLog "【警告】WSL 3.0 / WSL Containers (wslc.exe) が検出されませんでした。" "WARN"
        }
    }
}

# 稼働中の既存プロセス検出
if (Test-AppRunning) {
    Write-AppLog "既存の hayamimi ($script:CurrentEngineMode) が稼働中です。"
    Set-UIState "稼働中 (8833 / 8766)" $brushGreen $false $true
} else {
    Set-UIState "停止中" $brushGray $true $false
}

# 画面描画完了時のダイアログガイダンス (Wslc モード選択時のみ)
$window.Add_ContentRendered({
    if ($script:CurrentEngineMode -eq "Wslc") {
        $curDiag = Get-VirtualizationDiagnostic
        if (-not $curDiag.IsReady -and $curDiag.Reason -eq "BiosDisabled") {
            Show-BiosVirtualizationGuide -Diag $curDiag
        } elseif (-not $script:WslcCmd -or -not $curDiag.IsReady) {
            $ask = Show-AppConfirm -Message "hayamimi の実行に必要な仮想化機能または WSL 3.0 がセットアップされていません。`r`n`r`n※ Python venv モードを選択すれば、仮想化の設定なしで今すぐ動かせます。`r`n`r`nWSL 3.0 のセットアップを開始しますか？`r`n(管理者権限のインストール画面が開きます)" -Title "環境セットアップ - hayamimi"
            if ($ask -eq [System.Windows.MessageBoxResult]::Yes) {
                Invoke-InstallWslContainers | Out-Null
                Update-EngineBannerState | Out-Null
            }
        }
    }
})

# 起動時アップデート自動確認タイマー (起動1.2秒後に非同期で安全に実行)
# --- SAC (Smart App Control) 判定 & デスクトップショートカット自動最適化 ---
function Optimize-DesktopShortcut {
    try {
        $desktopPath = [System.Environment]::GetFolderPath("Desktop")
        $shortcutPath = Join-Path $desktopPath "hayamimi (早耳).lnk"
        if (-not (Test-Path $shortcutPath)) { return }

        $wsh = New-Object -ComObject WScript.Shell
        $sc = $wsh.CreateShortcut($shortcutPath)
        $curTarget = $sc.TargetPath
        $targetBat = Join-Path $ProjectRoot "start.bat"
        $targetVbs = Join-Path $ProjectRoot "start.vbs"

        # SAC 判定 (1: Enforced, 2: Evaluation)
        $isSac = $false
        try {
            $key = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -Name "VerifiedAndReputablePolicyState" -ErrorAction SilentlyContinue
            if ($key -and ($key.VerifiedAndReputablePolicyState -eq 1 -or $key.VerifiedAndReputablePolicyState -eq 2)) {
                $isSac = $true
            }
        } catch {}

        if ($isSac) {
            # SAC ON なのに start.vbs を指している場合 -> start.bat に自動修復 (SACブロック回避)
            if ($curTarget -like "*start.vbs*") {
                $sc.TargetPath = $targetBat
                $sc.WindowStyle = 7
                $sc.WorkingDirectory = $ProjectRoot
                $sc.Save()
                Write-AppLog "Smart App Control (SAC) 有効環境を検出: デスクトップショートカットを start.bat に自動最適化しました。" "INFO"
            }
        } else {
            # SAC OFF 環境: start.vbs が無ければ自動配備し、VBS起動ショートカットを維持
            if (-not (Test-Path $targetVbs)) {
                $vbsCode = @"
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

psCmd = "powershell"
If fso.FileExists("C:\Program Files\PowerShell\7\pwsh.exe") Then
    psCmd = """C:\Program Files\PowerShell\7\pwsh.exe"""
ElseIf fso.FileExists("C:\Program Files (x86)\PowerShell\7\pwsh.exe") Then
    psCmd = """C:\Program Files (x86)\PowerShell\7\pwsh.exe"""
Else
    Dim pathEnv, paths, p
    pathEnv = WshShell.ExpandEnvironmentStrings("%PATH%")
    paths = Split(pathEnv, ";")
    For Each p In paths
        If p <> "" And fso.FolderExists(p) Then
            If fso.FileExists(fso.BuildPath(p, "pwsh.exe")) Then
                psCmd = "pwsh"
                Exit For
            End If
        End If
    Next
End If

targetScript = scriptDir & "\scripts\launcher_gui.ps1"
If Not fso.FileExists(targetScript) Then
    If fso.FileExists(scriptDir & "\scripts\scripts\launcher_gui.ps1") Then
        targetScript = scriptDir & "\scripts\scripts\launcher_gui.ps1"
    Else
        MsgBox "Cannot find launcher script:" & vbCrLf & _
               scriptDir & "\scripts\launcher_gui.ps1", 16, "hayamimi launcher error"
        WScript.Quit 1
    End If
End If

If InStr(psCmd, "pwsh") > 0 Then
    cmdLine = psCmd & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & targetScript & """"
Else
    cmdLine = psCmd & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command ""$s=[System.IO.File]::ReadAllText('" & Replace(targetScript, "'", "''") & "',[System.Text.Encoding]::UTF8);&([scriptblock]::Create($s))"""
End If

On Error Resume Next
WshShell.Run cmdLine, 0, False
If Err.Number <> 0 Then
    MsgBox "Failed to launch hayamimi (Error " & Err.Number & "):" & vbCrLf & _
           Err.Description & vbCrLf & vbCrLf & _
           "Command: " & cmdLine, 16, "hayamimi launcher error"
    WScript.Quit Err.Number
End If
"@
                [System.IO.File]::WriteAllText($targetVbs, $vbsCode, [System.Text.Encoding]::ASCII)
            }
        }
    } catch {}
}
Optimize-DesktopShortcut

$updateCheckTimer = [System.Windows.Threading.DispatcherTimer]::new()
$updateCheckTimer.Interval = [TimeSpan]::FromMilliseconds(1200)
$updateCheckTimer.Add_Tick({
    $updateCheckTimer.Stop()
    try {
        Test-AppUpdate -PromptUser
    } catch {
        Write-AppLog "起動時アップデート確認中に例外が発生しました: $_" "WARN"
    }
})
$updateCheckTimer.Start()

# 起動時モデル自己修復タイマー (ウィンドウ描画後 400ms に非同期実行)
$modelRepairTimer = [System.Windows.Threading.DispatcherTimer]::new()
$modelRepairTimer.Interval = [TimeSpan]::FromMilliseconds(400)
$modelRepairTimer.Add_Tick({
    $modelRepairTimer.Stop()
    try {
        if ($script:NeedModelRepairOnStartup) {
            $script:NeedModelRepairOnStartup = $false
            Ensure-ModelsIntegrity | Out-Null
        }
    } catch {
        Write-AppLog "起動時モデル自己修復中に例外が発生しました: $_" "WARN"
    }
})
$modelRepairTimer.Start()

# 起動時新機能依存パッケージ自己修復タイマー (起動600ms後にUIスレッドセーフ・SAC安全にチェック)
# ※ PowerShell では ThreadPool.QueueUserWorkItem に ScriptBlock を渡すと Runspace 不在例外で
#    プロセス全体が即死するため、必ず WPF DispatcherTimer でプロセス完了を監視する。
$script:ProcDepInstall = $null
$depInstallWatchTimer = [System.Windows.Threading.DispatcherTimer]::new()
$depInstallWatchTimer.Interval = [TimeSpan]::FromMilliseconds(500)
$depInstallWatchTimer.Add_Tick({
    try {
        if (-not $script:ProcDepInstall) {
            $depInstallWatchTimer.Stop()
            return
        }
        if ($script:ProcDepInstall.HasExited) {
            $depInstallWatchTimer.Stop()
            $exitCode = $script:ProcDepInstall.ExitCode
            $script:ProcDepInstall = $null
            if ($exitCode -eq 0) {
                Unblock-ProjectFiles -RootDir $ProjectRoot
                Write-AppLog "✔ imageio-ffmpeg の自動配備が完了しました (YouTube取得・ffmpeg対応)" "INFO"
            }
        }
    } catch {
        $depInstallWatchTimer.Stop()
        $script:ProcDepInstall = $null
    }
})

$depCheckTimer = [System.Windows.Threading.DispatcherTimer]::new()
$depCheckTimer.Interval = [TimeSpan]::FromMilliseconds(600)
$depCheckTimer.Add_Tick({
    $depCheckTimer.Stop()
    try {
        # 起動直後にスクリプト・実行ファイル群の Zone.Identifier (Mark of the Web) を安全解除
        Unblock-ProjectFiles -RootDir $ProjectRoot

        if ($script:CurrentEngineMode -eq "Venv") {
            $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
            if (Test-Path $venvPython) {
                # imageio_ffmpeg の有無をクイックチェック
                $pinfoCheck = [System.Diagnostics.ProcessStartInfo]::new()
                $pinfoCheck.FileName = $venvPython
                $pinfoCheck.Arguments = "-B -c `"import imageio_ffmpeg`""
                $pinfoCheck.CreateNoWindow = $true
                $pinfoCheck.UseShellExecute = $false
                $procCheck = [System.Diagnostics.Process]::Start($pinfoCheck)
                $procCheck.WaitForExit(3000) | Out-Null
                if ($procCheck.HasExited -and $procCheck.ExitCode -ne 0) {
                    Write-AppLog "未適用の外部依存パッケージ (imageio-ffmpeg) を自動検出しました。仮想環境を同期中..." "INFO"
                    # スマートアプリコントロール (SAC) による未署名 uv.exe ブロック警告を回避するため、
                    # 既に起動確認済みの公式 Python (pip) を直接使用してバックグラウンド導入する
                    $pinfoInstall = [System.Diagnostics.ProcessStartInfo]::new()
                    $pinfoInstall.FileName = $venvPython
                    $pinfoInstall.Arguments = "-B -m pip install imageio-ffmpeg>=0.5.1"
                    $pinfoInstall.WorkingDirectory = $ProjectRoot
                    $pinfoInstall.CreateNoWindow = $true
                    $pinfoInstall.UseShellExecute = $false
                    $pinfoInstall.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
                    $script:ProcDepInstall = [System.Diagnostics.Process]::Start($pinfoInstall)
                    $depInstallWatchTimer.Start()
                }
            }
        }
    } catch {
        Write-AppLog "依存パッケージ自動チェックをスキップしました: $_" "WARN"
    }
})
$depCheckTimer.Start()

$pollTimer.Start()

# ウィンドウ表示
$window.ShowDialog() | Out-Null

$pollTimer.Stop()
$healthTimer.Stop()
if ($updateCheckTimer -and $updateCheckTimer.IsEnabled) {
    $updateCheckTimer.Stop()
}
if ($modelRepairTimer -and $modelRepairTimer.IsEnabled) {
    $modelRepairTimer.Stop()
}
if ($depCheckTimer -and $depCheckTimer.IsEnabled) {
    $depCheckTimer.Stop()
}
if ($depInstallWatchTimer -and $depInstallWatchTimer.IsEnabled) {
    $depInstallWatchTimer.Stop()
}
