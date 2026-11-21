@echo off
REM Launch the multi-worker OCR service from the repo root (one level up).
REM Path-independent, so it works after re-cloning anywhere. Run by the
REM OCR-Server scheduled task (registered by setup\install_autostart.ps1).
cd /d "%~dp0.."
REM Machine-local overrides (git-ignored), e.g. "set OCR_WORKERS=1".
REM Written by manage\Set-Workers.cmd; absent = built-in defaults.
if exist "data\settings.local.cmd" call "data\settings.local.cmd"
".venv\Scripts\python.exe" src\serve.py
