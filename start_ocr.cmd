@echo off
REM Launch the multi-worker OCR service (used by the OCR-Server scheduled task).
cd /d "D:\projects\paddleocr-prototype"
"D:\projects\paddleocr-prototype\.venv\Scripts\python.exe" serve.py
