@echo off
rem Starts the Pulse demo: the panel in the browser, with WinUAE already booting.
rem   start_demo.cmd [path\to\winuae64.exe]
setlocal

rem ===== EDIT HERE: full path to your winuae64.exe with LinkUAE (from Releases or your build) =====
set "WINUAE_EXE="
rem e.g. set "WINUAE_EXE=C:\Emulators\LinkUAE\winuae64.exe"
rem ==============================================================================================

rem Where to look, in order: the argument, WINUAE_EXE above, the WINUAE variable, linkuae\build\out.
rem A relative argument is relative to where you ran this, so resolve it before changing folder.
set "ARG=%~f1"
cd /d "%~dp0"
set "EXE="
if not "%~1"=="" set "EXE=%ARG%"
if not defined EXE if defined WINUAE_EXE set "EXE=%WINUAE_EXE%"
if not defined EXE if defined WINUAE set "EXE=%WINUAE%"
if not defined EXE if exist "%~dp0..\..\build\out\winuae64.exe" set "EXE=%~dp0..\..\build\out\winuae64.exe"
if not defined EXE (
  echo WinUAE with LinkUAE not found. Write its path in WINUAE_EXE at the top of
  echo start_demo.cmd, or pass it: start_demo.cmd path\to\winuae64.exe
  goto fail
)
if not exist "%EXE%" (
  echo Not found: "%EXE%"
  goto fail
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is not on PATH.
  goto fail
)

echo WinUAE: %EXE%
python panel.py "%EXE%" --start
if errorlevel 1 goto fail
exit /b 0

:fail
pause
exit /b 1
