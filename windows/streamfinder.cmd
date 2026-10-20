@echo off
rem streamfinder start ^| stop ^| restart ^| status ^| update ^| logs  (this folder is on PATH after installing)
rem Python: the clone's .venv (windows-install.ps1) or the one bundled with the setup .exe.
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\..\python\python.exe"
"%PY%" "%~dp0control.py" %*
