@echo off
REM Двойной клик — сам определяет букву диска флешки по своему расположению,
REM набирать команды в PowerShell не нужно.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_portable.ps1" -Drive %~d0\
pause
