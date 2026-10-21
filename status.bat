@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
set PYTHONIOENCODING=utf-8
python backup.py status
endlocal
