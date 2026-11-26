@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ===================================================
echo   Hayamimi Live (Early Hearing Live) - Setup Launcher
echo ===================================================
echo.

:: 1. Resolve PowerShell 7 (pwsh.exe) - install automatically via winget if missing
set "PWSH_EXE="
for /f "delims=" %%P in ('where pwsh 2^>nul') do (
    if not defined PWSH_EXE set "PWSH_EXE=%%P"
)
if not defined PWSH_EXE if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH_EXE if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PWSH_EXE if exist "%ProgramFiles(x86)%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles(x86)%\PowerShell\7\pwsh.exe"

if not defined PWSH_EXE (
    echo [*] PowerShell 7 ^(pwsh^) was not found. Installing via winget...
    set "WINGET_EXE=winget"
    where winget >nul 2>&1
    if errorlevel 1 (
        if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe" (
            set "WINGET_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe"
        ) else (
            set "WINGET_EXE="
        )
    )
    if defined WINGET_EXE (
        "%WINGET_EXE%" install --id Microsoft.PowerShell -e --source winget --accept-package-agreements --accept-source-agreements
        for /f "delims=" %%P in ('where pwsh 2^>nul') do (
            if not defined PWSH_EXE set "PWSH_EXE=%%P"
        )
        if not defined PWSH_EXE if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
        if not defined PWSH_EXE if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
        if not defined PWSH_EXE if exist "%ProgramFiles(x86)%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles(x86)%\PowerShell\7\pwsh.exe"
    )
)

if not defined PWSH_EXE (
    echo [ERROR] PowerShell 7 ^(pwsh.exe^) is required and could not be installed automatically.
    echo Please install PowerShell 7 from Microsoft Store or https://aka.ms/powershell-release?tag=stable
    echo.
    pause
    exit /b 1
)

:: 2. Check local setup.ps1 or download from GitHub
set "SETUP_SCRIPT=%~dp0setup.ps1"
if exist "%SETUP_SCRIPT%" goto run_setup

set "SETUP_SCRIPT=%TEMP%\hayamimi_setup.ps1"
set "GITHUB_RAW_URL=https://raw.githubusercontent.com/Maximilian2022/hayamimi-live/main/setup.ps1"

echo [*] Downloading setup script from GitHub...
echo     URL: %GITHUB_RAW_URL%
echo.

"%PWSH_EXE%" -NoProfile -Command "Invoke-WebRequest -Uri $env:GITHUB_RAW_URL -OutFile $env:SETUP_SCRIPT"

if not exist "%SETUP_SCRIPT%" (
    echo [ERROR] Failed to download setup.ps1 from GitHub.
    echo Please check your internet connection or download manually:
    echo %GITHUB_RAW_URL%
    echo.
    pause
    exit /b 1
)

:run_setup
echo [*] Launching hayamimi setup GUI ^(PowerShell 7^)...
"%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "try { Unblock-File -LiteralPath $env:SETUP_SCRIPT -ErrorAction SilentlyContinue } catch {}"
"%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%SETUP_SCRIPT%"

:: Clean up temp file if downloaded
if exist "%TEMP%\hayamimi_setup.ps1" (
    del /f /q "%TEMP%\hayamimi_setup.ps1" >nul 2>&1
)

endlocal
exit /b 0
