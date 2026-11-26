@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

:: 1. Resolve PowerShell 7 (pwsh.exe) - auto-install via winget on first run if missing
set "PWSH_EXE="
for /f "delims=" %%P in ('where pwsh 2^>nul') do (
    if not defined PWSH_EXE set "PWSH_EXE=%%P"
)
if not defined PWSH_EXE if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH_EXE if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PWSH_EXE if exist "%ProgramFiles(x86)%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles(x86)%\PowerShell\7\pwsh.exe"

if not defined PWSH_EXE (
    echo [*] PowerShell 7 ^(pwsh^) not found. Installing via winget...
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
    pause
    exit /b 1
)

:: 2. Smart App Control (SAC) check:
:: When SAC is Enforced (0x1) or Evaluation (0x2), unsigned VBScript (wscript.exe) is blocked.
set "SAC_ENABLED=0"
for /f "tokens=3" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState 2^>nul') do (
    if "%%A"=="0x1" set "SAC_ENABLED=1"
    if "%%A"=="0x2" set "SAC_ENABLED=1"
)

:: Use start.vbs for zero-flicker launch only when SAC is OFF (0); full Unblock runs asynchronously inside GUI
if "%SAC_ENABLED%"=="0" (
    if exist "%~dp0start.vbs" (
        start "" wscript.exe "%~dp0start.vbs"
        endlocal
        exit /b 0
    )
)

:: Launch GUI directly via PowerShell 7 (-File)
start "" "%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\launcher_gui.ps1"

endlocal
exit /b 0
