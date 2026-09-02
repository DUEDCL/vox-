@echo off
setlocal

rem ---------------------------------------------------------------------------
rem  Vox launcher: console + microphone + wake orb.
rem
rem  ASCII ONLY. cmd.exe reads a batch file using the CURRENT console code page
rem  (cp936 on this machine) line by line as it executes, so a UTF-8 comment is
rem  decoded as mojibake and cmd then tries to RUN the garbage. `chcp 65001`
rem  cannot fix it: the earlier lines were already read. All Chinese text lives
rem  in Python, which prints UTF-8 that the console can render once chcp ran.
rem
rem  Three things this file does that the code must not:
rem    chcp 65001  - Python writes UTF-8 bytes; the console default is cp936.
rem    pushd %~dp0.. - config/models/.env resolve from the repo root, and the
rem                    current directory of a double click is the Desktop.
rem    pause on failure - a double-clicked window closes instantly on exit.
rem ---------------------------------------------------------------------------

chcp 65001 >nul
pushd "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [Vox] .venv\Scripts\python.exe not found -- the isolated environment is missing.
    echo.
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements-voice.txt
    echo.
    popd
    pause
    exit /b 1
)

rem No arguments = the everyday launch. Arguments are passed straight through,
rem which is what makes `vox.cmd --help` a real smoke test of this file.
set VOXARGS=%*
if "%VOXARGS%"=="" set VOXARGS=--voice

.venv\Scripts\python.exe scripts\run_console.py %VOXARGS%
set EXITCODE=%ERRORLEVEL%

popd

if not "%EXITCODE%"=="0" (
    echo.
    echo [Vox] exited with code %EXITCODE%
    echo.
    echo   * port 8899 already in use   -- another Vox is still running
    echo   * microphone busy or blocked -- the console still opens and says so
    echo   * missing API key            -- fix it on the console's key page
    echo.
    pause
)

endlocal
