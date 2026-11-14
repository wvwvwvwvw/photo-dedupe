@echo off
rem Moving to another PC? See AGENT_SETUP.md (setup steps for humans and AI).
rem Photo Dedupe launcher.
rem The "runtime" folder next to this file is a complete Python - the app needs
rem nothing else installed on the machine.
setlocal
cd /d "%~dp0"
title Photo Dedupe

set "PYEXE="

call :check_exe "%~dp0runtime\python.exe"
if not defined PYEXE call :check_exe "%~dp0.venv\Scripts\python.exe"
if not defined PYEXE call :check_cmd python
if not defined PYEXE call :check_cmd py -3

if not defined PYEXE goto nodeps

%PYEXE% "%~dp0app.py" %*
if errorlevel 1 pause
goto end

:check_exe
if not exist "%~1" goto :eof
"%~1" -c "import PIL, numpy" >nul 2>&1
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof

:check_cmd
where %1 >nul 2>&1
if errorlevel 1 goto :eof
%* -c "import PIL, numpy" >nul 2>&1
if errorlevel 1 goto :eof
set "PYEXE=%*"
goto :eof

:nodeps
echo.
echo   [!] The bundled runtime folder is missing or broken.
echo.
echo   Fix one of these:
echo     1) Put the "runtime" folder back next to this file (it ships with
echo        the app, about 127 MB).
echo     2) Or double-click the setup bat to build a local .venv.
echo.
echo   Run the check bat to see details.
echo.
pause
goto end

:end
endlocal