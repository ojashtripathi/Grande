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

if ! "$PY" -c "import grande" >/dev/null 2>&1; then
  echo "Installing Grande and its dependencies. This happens once."
  "$PY" -m pip install --quiet -e .
fi

exec "$PY" -m grande "$@"
