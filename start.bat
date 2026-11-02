@echo off
setlocal
cd /d "%~dp0"

:: Select PowerShell Runtime: pwsh (PS7) if available, otherwise powershell (PS5.1)
set "PS_CMD=powershell"
where pwsh >nul 2>&1
if %errorlevel% equ 0 set "PS_CMD=pwsh"

:: Remove Mark of the Web (Zone.Identifier) from scripts & tools to prevent Smart App Control / SmartScreen blocks (P-1: .venv除外で起動高速化)
%PS_CMD% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "try { Get-ChildItem -LiteralPath '%~dp0' -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue; foreach ($d in @('%~dp0scripts', '%~dp0tools', '%~dp0.venv\Scripts')) { if (Test-Path $d) { Get-ChildItem -LiteralPath $d -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue } } } catch {}"

:: Smart App Control (SAC) 判定:
:: Windows 11 の SAC 有効時 (0x1) は未署名 VBScript (wscript.exe) がブロックされるため、直接 PowerShell で起動
set "SAC_ENABLED=0"
for /f "tokens=3" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState 2^>nul') do (
    if "%%A"=="0x1" set "SAC_ENABLED=1"
    if "%%A"=="0x2" set "SAC_ENABLED=1"
)

:: SAC がオフ (0) かつ start.vbs が存在する場合のみ wscript で起動してターミナルポップアップを防止
if "%SAC_ENABLED%"=="0" (
    if exist "%~dp0start.vbs" (
        start "" wscript.exe "%~dp0start.vbs"
        endlocal
        exit /b 0
    )
)

:: SAC 有効時、または start.vbs 不在時は安全に PowerShell を直接 Hidden で起動
if "%PS_CMD%"=="pwsh" (
    start "" %PS_CMD% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\launcher_gui.ps1"
) else (
    start "" %PS_CMD% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$s=[System.IO.File]::ReadAllText('%~dp0scripts\launcher_gui.ps1',[System.Text.Encoding]::UTF8);&([scriptblock]::Create($s))"
)

endlocal
exit /b 0
