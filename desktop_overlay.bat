@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PWSH_EXE="
for /f "delims=" %%P in ('where pwsh 2^>nul') do (
    if not defined PWSH_EXE set "PWSH_EXE=%%P"
)
if not defined PWSH_EXE if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH_EXE if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PWSH_EXE if exist "%ProgramFiles(x86)%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles(x86)%\PowerShell\7\pwsh.exe"

if not defined PWSH_EXE (
    echo [ERROR] PowerShell 7 ^(pwsh.exe^) was not found. Please run start.bat or setup.bat first.
    pause
    exit /b 1
)

start "" "%PWSH_EXE%" -STA -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\desktop_overlay.ps1" %*
endlocal
exit /b 0
