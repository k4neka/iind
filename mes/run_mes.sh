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

echo "[mes] ensuring venv packages (pip upgrade optional)..."
if [ "${SKIP_PIP_UPGRADE:-0}" != "1" ]; then
    echo "[mes] upgrading pip..."
    python3 -m pip install --quiet --upgrade pip || \
        echo "[mes] pip upgrade skipped or failed (platform-managed)"
fi

if [ -f "requirements.txt" ]; then
    echo "[mes] installing requirements..."
    python3 -m pip install --quiet -r requirements.txt || \
        echo "[mes] requirements install skipped or partially failed"
fi

echo "[mes] starting MES (main.py)..."
exec python3 main.py