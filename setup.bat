@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ===================================================
echo   Hayamimi Live (Early Hearing Live) - Setup Launcher
echo ===================================================
echo.

:: 1. Check PowerShell Runtime (prefer pwsh if available, fallback to powershell)
set "PS_CMD=powershell"
where pwsh >nul 2>&1
if %errorlevel% equ 0 set "PS_CMD=pwsh"

:: 2. Check local setup.ps1 or download from GitHub
set "SETUP_SCRIPT=%~dp0setup.ps1"
if exist "%SETUP_SCRIPT%" goto run_setup

set "SETUP_SCRIPT=%TEMP%\hayamimi_setup.ps1"
set "GITHUB_RAW_URL=https://raw.githubusercontent.com/Maximilian2022/hayamimi-live/main/setup.ps1"

echo [*] Downloading setup script from GitHub...
echo     URL: %GITHUB_RAW_URL%
echo.

%PS_CMD% -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile($env:GITHUB_RAW_URL, $env:SETUP_SCRIPT)"

if not exist "%SETUP_SCRIPT%" (
    echo [ERROR] Failed to download setup.ps1 from GitHub.
    echo Please check your internet connection or download manually:
    echo %GITHUB_RAW_URL%
    echo.
    pause
    exit /b 1
)

:run_setup
:: Unblock all local files (remove Mark of the Web / Zone.Identifier for Smart App Control & SmartScreen)
:: and ensure UTF-8 with BOM on .ps1 files so Windows PowerShell 5.1 parses Japanese characters cleanly
%PS_CMD% -NoProfile -ExecutionPolicy Bypass -Command "try { Get-ChildItem -LiteralPath '%~dp0' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue } catch {}; if (Test-Path $env:SETUP_SCRIPT) { try { Unblock-File -LiteralPath $env:SETUP_SCRIPT -ErrorAction SilentlyContinue } catch {}; $b=[System.IO.File]::ReadAllBytes($env:SETUP_SCRIPT); if ($b.Length -lt 3 -or $b[0] -ne 239 -or $b[1] -ne 187 -or $b[2] -ne 191) { [System.IO.File]::WriteAllBytes($env:SETUP_SCRIPT, [byte[]]@(239, 187, 191) + $b) } }"

echo [*] Launching hayamimi setup GUI...
%PS_CMD% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%SETUP_SCRIPT%"

:: Clean up temp file if downloaded
if exist "%TEMP%\hayamimi_setup.ps1" (
    del /f /q "%TEMP%\hayamimi_setup.ps1" >nul 2>&1
)

endlocal
exit /b 0
