@echo off
REM Alpha Engine - double-click this file to start.
REM
REM All the real logic lives in launch.py so there is one implementation of the
REM setup rather than one per platform. This only has to find a Python.

cd /d "%~dp0"

REM The py launcher ships with python.org installs and picks the newest version.
REM Plain `python` on Windows may be the Microsoft Store stub, which is why py
REM is tried first.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 launch.py %*
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python launch.py %*
    goto :done
)

echo.
echo   Python was not found on this computer.
echo.
echo   Install it from https://python.org/downloads
echo   IMPORTANT: tick "Add Python to PATH" in the installer, then
echo   double-click this file again.
echo.
pause
exit /b 1

:done
if %errorlevel% neq 0 pause
