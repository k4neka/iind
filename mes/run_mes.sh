#!/usr/bin/env bash
# Bootstrap + run the MES. Run from inside the mes/ directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PY="${PYTHON:-python3}"

if [ ! -d "$VENV_DIR" ]; then
    echo "[mes] creating virtual environment at $VENV_DIR"
    "$PY" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

echo "[mes] upgrading pip..."
pip install --quiet --upgrade pip

if [ -f "requirements.txt" ]; then
    echo "[mes] installing requirements..."
    pip install --quiet -r requirements.txt
fi

echo "[mes] starting MES (main.py)..."
exec python main.py