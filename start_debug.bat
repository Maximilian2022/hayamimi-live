@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ===================================================
echo   hayamimi - WSL3 Launcher (Debug Console)
echo ===================================================
echo.

where wslc >nul 2>&1
if %errorlevel% equ 0 goto check_runtime

echo [ERROR] wslc not found. WSL 3.0 (wsl --update) is required.
pause
exit /b 1

:check_runtime
set "PWSH_EXE="
for /f "delims=" %%P in ('where pwsh 2^>nul') do (
    if not defined PWSH_EXE set "PWSH_EXE=%%P"
)
if not defined PWSH_EXE if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH_EXE if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PWSH_EXE if exist "%ProgramFiles(x86)%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles(x86)%\PowerShell\7\pwsh.exe"

if not defined PWSH_EXE (
    echo [ERROR] PowerShell 7 ^(pwsh.exe^) is required. Please run setup.bat or start.bat to install it.
    pause
    exit /b 1
)
echo [*] Using PowerShell 7 ^(%PWSH_EXE%^)

:: Check image
wslc images 2>nul | findstr /i "hayamimi" >nul
if %errorlevel% equ 0 goto launch_gui

echo [*] Building hayamimi container image...
wslc build -t hayamimi:latest .
if %errorlevel% equ 0 goto launch_gui

echo [ERROR] Failed to build container image.
pause
exit /b 1

:launch_gui
echo [*] Starting hayamimi GUI Launcher (Console Active)...
"%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher_gui.ps1"

pause
endlocal
exit /b 0
