@echo off
setlocal
cd /d "%~dp0"

:: Select PowerShell Runtime: pwsh (PS7) if available, otherwise powershell (PS5.1)
set "PS_CMD=powershell"
where pwsh >nul 2>&1
if %errorlevel% equ 0 set "PS_CMD=pwsh"

:: Remove Mark of the Web (Zone.Identifier) from scripts & tools to prevent Smart App Control / SmartScreen blocks (P-1: .venv除外で起動高速化)
%PS_CMD% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "try { Get-ChildItem -LiteralPath '%~dp0' -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue; foreach ($d in @('%~dp0scripts', '%~dp0tools', '%~dp0.venv\Scripts')) { if (Test-Path $d) { Get-ChildItem -LiteralPath $d -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue } } } catch {}"

:: If start.vbs exists, run via wscript silently to prevent Windows Terminal popup
if exist "%~dp0start.vbs" (
    start "" wscript.exe "%~dp0start.vbs"
    endlocal
    exit /b 0
)

:: Fallback if start.vbs is missing
start "" %PS_CMD% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\launcher_gui.ps1"

endlocal
exit /b 0
