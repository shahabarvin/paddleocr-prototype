@echo off
REM Launch the multi-worker OCR service. Path-independent (uses its own folder),
REM so it works after re-cloning anywhere. Used by the OCR-Server scheduled task.
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" serve.py
