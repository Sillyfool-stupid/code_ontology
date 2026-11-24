#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python scripts/intranet.py "$@"
elif [ -f .venv/Scripts/python.exe ]; then
  exec .venv/Scripts/python.exe scripts/intranet.py "$@"
else
  echo 'Project .venv missing; create it and install requirements first.' >&2
  exit 1
fi
