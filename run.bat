@echo off
setlocal
title Grande
cd /d "%~dp0"

echo Starting Grande...
echo.

rem --- find a Python 3 ------------------------------------------------------
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  py -3 --version >nul 2>&1 && set "PY=py -3"
)

if not defined PY (
  echo Python 3.10 or newer is needed, and was not found.
  echo.
  echo Install it from https://www.python.org/downloads/ and tick
  echo "Add Python to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

rem --- install once ---------------------------------------------------------
rem Note: "%~dp0" would end in a backslash, and \" escapes the closing quote,
rem so pip would receive a path with a stray quote on the end. We have already
rem changed into this folder, so "." is both correct and safe.
%PY% -c "import grande" >nul 2>&1
if errorlevel 1 (
  echo Installing Grande and its dependencies. This happens once,
  echo and needs an internet connection. It may take a minute.
  echo.
  %PY% -m pip install --disable-pip-version-check --quiet -e .
  if errorlevel 1 (
    echo.
    echo The install did not finish. The messages above say why.
    echo.
    echo If it mentions a network or proxy problem, connect to the internet
    echo and try again. Everything after this first install works offline.
    echo.
    pause
    exit /b 1
  )
  echo Done.
  echo.
)

rem --- run ------------------------------------------------------------------
%PY% -m grande %*
if errorlevel 1 (
  echo.
  echo Grande stopped with an error. The messages above say why.
  pause
)
