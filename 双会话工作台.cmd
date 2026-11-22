@echo off
setlocal
where py.exe >nul 2>&1
if errorlevel 1 goto use_python
py.exe -3 "%~dp0daily\launcher.py" %*
goto finished
:use_python
python.exe "%~dp0daily\launcher.py" %*
:finished
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%
