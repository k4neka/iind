#!/usr/bin/env bash
# Bootstrap + run the ERP. Run from inside the erp/ directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PY="${PYTHON:-python3}"

if [ ! -d "$VENV_DIR" ]; then
    echo "[erp] creating virtual environment at $VENV_DIR"
    "$PY" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

echo "[erp] upgrading pip..."
pip install --quiet --upgrade pip

if [ -f "requirements.txt" ]; then
    echo "[erp] installing requirements..."
    pip install --quiet -r requirements.txt
fi

echo "[erp] starting ERP (main.py)..."
exec python main.py