@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
set PYTHONIOENCODING=utf-8

if "%~1"=="" (
  python backup.py backup
) else (
  python backup.py %*
)
endlocal
