@echo off
setlocal
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
set "PS_CMD=powershell"
where pwsh >nul 2>&1
if %errorlevel% equ 0 (
    set "PS_CMD=pwsh"
    echo [*] Using PowerShell 7 (pwsh)
) else (
    echo [*] Using Windows PowerShell 5.1
)

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
if "%PS_CMD%"=="pwsh" (
    %PS_CMD% -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher_gui.ps1"
) else (
    %PS_CMD% -NoProfile -ExecutionPolicy Bypass -Command "$s=[System.IO.File]::ReadAllText('%~dp0scripts\launcher_gui.ps1',[System.Text.Encoding]::UTF8);&([scriptblock]::Create($s))"
)

pause
endlocal
exit /b 0