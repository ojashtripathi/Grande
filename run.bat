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

rem --- install once per copy ------------------------------------------------
rem Note: "%~dp0" would end in a backslash, and \" escapes the closing quote,
rem so pip would receive a path with a stray quote on the end. We have already
rem changed into this folder, so "." is both correct and safe.
rem "import grande" alone is not enough: once a newer zip is extracted to a
rem new folder, the copy installed from the old folder still imports, and this
rem launcher would quietly go on running the old version. So check that the
rem Grande that imports is the one in this folder.
%PY% -c "import grande, pathlib, sys; here = pathlib.Path('src').resolve(); sys.exit(0 if pathlib.Path(grande.__file__).resolve().is_relative_to(here) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Installing this copy of Grande and its dependencies. This happens once
  echo per copy, and needs an internet connection. It may take a minute.
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
