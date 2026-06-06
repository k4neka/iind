#!/usr/bin/env bash
# Launches ERP and MES, with the optional Client Order GUI.
#
# Usage:
#   ./run_all.sh             # ERP + MES
#   ./run_all.sh --gui       # ERP + MES + Client GUI
#   ./run_all.sh -g          # same as --gui
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
ERP_DIR="$ROOT/erp"
MES_DIR="$ROOT/mes"

START_GUI=0
DO_RESET=0
for arg in "$@"; do
    case "$arg" in
        --gui|-g) START_GUI=1 ;;
        --reset|-r) DO_RESET=1 ;;
        --help|-h)
            echo "Usage: $0 [--gui] [--reset]"
            echo "  --gui    also launch the Client Order GUI"
            echo "  --reset  clear database and reset clock before starting"
            exit 0
            ;;
    esac
done

if [ "$DO_RESET" -eq 1 ]; then
    echo "[run_all] Resetting database..."
    # Ensure ERP venv is ready so we can run reset_db.py
    if [ ! -d "$ERP_DIR/.venv" ]; then
        ( cd "$ERP_DIR" && ./run_erp.sh ) & 
        ERP_BOOT_PID=$!
        echo "[run_all] Waiting for ERP venv to initialize..."
        while [ ! -f "$ERP_DIR/.venv/bin/python3" ]; do sleep 1; done
        kill $ERP_BOOT_PID 2>/dev/null || true
        wait $ERP_BOOT_PID 2>/dev/null || true
    fi
    "$ERP_DIR/.venv/bin/python3" "$ROOT/reset_db.py"
fi

# Ensure helper scripts are executable
chmod +x "$ERP_DIR/run_erp.sh" "$MES_DIR/run_mes.sh" 2>/dev/null || true

pids=()

cleanup() {
    echo
    echo "[run_all] Stopping all subsystems..."
    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM

echo "[run_all] Launching ERP..."
( cd "$ERP_DIR" && ./run_erp.sh ) &
pids+=($!)

# Small delay so the ERP TCP port (6666) is up before the GUI / MES start
sleep 3

echo "[run_all] Launching MES..."
( cd "$MES_DIR" && ./run_mes.sh ) &
pids+=($!)

echo "[run_all] Launching Dashboard Service..."
# Wait up to 30s for ERP venv to be ready
timeout=30
while [ ! -f "erp/.venv/bin/python3" ] && [ $timeout -gt 0 ]; do
    sleep 1
    timeout=$((timeout-1))
done
if [ -f "erp/.venv/bin/python3" ]; then
    ( cd "$ROOT" && erp/.venv/bin/python3 dashboard_service.py ) &
    pids+=($!)
else
    echo "[run_all] WARN: ERP venv not found, Dashboard Service skipped."
fi

if [ "$START_GUI" -eq 1 ]; then
    sleep 2
    echo "[run_all] Launching Client Order GUI..."

    # Use a dedicated venv for the GUI so we don't pollute ERP/MES envs
    GUI_VENV="$ROOT/.venv_gui"
    if [ ! -d "$GUI_VENV" ]; then
        python3 -m venv "$GUI_VENV"
    fi
    
    # Use the venv's python directly to avoid PEP 668 externally-managed errors
    VENV_PYTHON="$GUI_VENV/bin/python3"
    "$VENV_PYTHON" -m pip install --quiet --upgrade pip || true
    # Tkinter is shipped with CPython on most distros; nothing to pip install.
    # (If on Debian/Ubuntu you may need: sudo apt-get install python3-tk)

    "$VENV_PYTHON" "$ROOT/client_gui.py" &
    pids+=($!)
fi

echo "[run_all] All processes started. Ctrl-C to stop everything."
wait