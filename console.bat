@echo off
rem === wool-hunter desktop console ===
rem
rem Double-click this. It is the only thing you need to run.
rem It works even when the bot is not running:
rem   - fill in .env with a form (no text editor needed)
rem   - health checks (Python, deps, NapCat, DeepSeek key, Weibo cookie)
rem   - start / stop / restart the bot, watch live logs
rem   - start / stop NapCat and scan its QR code, without a black box
rem   - subscriptions, categories, noise filters, feedback, resend
rem
rem The console is its own watchdog: it restarts the bot 3 seconds after a crash.
rem
rem `pyw` / `pythonw` are the windowed Python launchers -- no black console box.
rem Crashes still get written to logs\console_error.log and shown in a dialog.
rem
rem This file must stay pure ASCII: cmd.exe reads it as GBK and mangles UTF-8.

cd /d "%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw console.py
  exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw console.py
  exit /b
)

echo Could not find pyw.exe or pythonw.exe.
echo Install Python 3.10+ from python.org and tick "Add python.exe to PATH".
pause >nul
