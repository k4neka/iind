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

echo "[erp] ensuring venv packages (pip upgrade optional)..."
if [ "${SKIP_PIP_UPGRADE:-0}" != "1" ]; then
    echo "[erp] attempting pip upgrade (will continue if blocked)..."
    python3 -m pip install --quiet --upgrade pip || \
        echo "[erp] pip upgrade skipped or failed (platform-managed)"
fi

if [ -f "requirements.txt" ]; then
    echo "[erp] installing requirements (if missing)..."
    python3 -m pip install --quiet -r requirements.txt || \
        echo "[erp] requirements install skipped or partially failed"
fi

echo "[erp] starting ERP (main.py)..."
exec python3 main.py