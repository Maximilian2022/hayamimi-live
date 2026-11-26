@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ===================================================
echo   hayamimi - Debug Launcher (Console Active)
echo ===================================================
echo.

:: 1. Resolve PowerShell 7 (pwsh.exe) - auto-install via winget if missing
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
echo [*] Using PowerShell 7 ^(%PWSH_EXE%^)

:: 2. Show quick environment summary (supports both Python venv and WSL3 modes)
if exist "%~dp0.venv\Scripts\python.exe" (
    echo [*] .venv Python detected: "%~dp0.venv\Scripts\python.exe"
) else (
    echo [*] .venv not yet initialized ^(GUI launcher will set it up in Venv mode^).
)
if exist "%~dp0.venv_gpu\Scripts\python.exe" (
    echo [*] .venv_gpu Python detected: "%~dp0.venv_gpu\Scripts\python.exe"
)
where wslc >nul 2>&1
if %errorlevel% equ 0 (
    echo [*] wslc CLI detected ^(WSL3 mode available^).
)

echo.
echo [*] Starting hayamimi GUI Launcher ^(Console Active^)...
"%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher_gui.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [*] Launcher exited with code %EXIT_CODE%.
echo [*] Log file: "%~dp0logs\launcher.log"
pause
endlocal
exit /b %EXIT_CODE%
