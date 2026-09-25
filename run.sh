#!/usr/bin/env bash
# Start Grande on macOS or Linux.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed, and was not found."
  echo "Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

# Install when Grande is missing *or* is installed from another folder: after a
# newer copy is extracted elsewhere, the old install would otherwise keep running.
if ! "$PY" -c "import grande, pathlib, sys; sys.exit(0 if pathlib.Path(grande.__file__).resolve().is_relative_to(pathlib.Path('src').resolve()) else 1)" >/dev/null 2>&1; then
  echo "Installing this copy of Grande and its dependencies. This happens once per copy."
  "$PY" -m pip install --quiet -e .
fi

exec "$PY" -m grande "$@"
