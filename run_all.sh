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
DO_SETUP=0

for arg in "$@"; do
    case "$arg" in
        --gui|-g) START_GUI=1 ;;
        --reset|-r) DO_RESET=1 ;;
        --setup|-s) DO_SETUP=1 ;;
        --help|-h)
            echo "Usage: $0 [--gui] [--reset] [--setup]"
            echo "  --gui    also launch the Client Order GUI"
            echo "  --reset  clear database and reset clock before starting"
            echo "  --setup  install all dependencies and exit"
            exit 0
            ;;
    esac
done

# --- Dependency Check & Installation ---

ensure_venv() {
    local dir=$1
    local name=$2
    echo "[setup] Ensuring virtual environment for $name..."
    if [ ! -d "$dir/.venv" ]; then
        if ! python3 -m venv "$dir/.venv" 2>/dev/null; then
            echo "[ERROR] Failed to create venv in $dir."
            echo "        On Debian/Ubuntu, try: sudo apt-get install python3-venv"
            exit 1
        fi
    fi
    
    # Install requirements
    if [ -f "$dir/requirements.txt" ]; then
        echo "[setup] Installing requirements for $name..."
        "$dir/.venv/bin/python3" -m pip install --quiet --upgrade pip || true
        "$dir/.venv/bin/python3" -m pip install --quiet -r "$dir/requirements.txt" || {
            echo "[ERROR] Failed to install requirements for $name."
            exit 1
        }
    fi
}

echo "[run_all] Performing dependency check..."

# Check for python3
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found. Please install Python 3."
    exit 1
fi

# Ensure ERP and MES venvs
ensure_venv "$ERP_DIR" "ERP"
ensure_venv "$MES_DIR" "MES"

# Ensure GUI venv if requested or just to be safe
if [ "$START_GUI" -eq 1 ] || [ "$DO_SETUP" -eq 1 ]; then
    GUI_VENV="$ROOT/.venv_gui"
    echo "[setup] Ensuring virtual environment for Client GUI..."
    if [ ! -d "$GUI_VENV" ]; then
        python3 -m venv "$GUI_VENV" || true
    fi
    if [ -d "$GUI_VENV" ]; then
        "$GUI_VENV/bin/python3" -m pip install --quiet --upgrade pip || true
        
        # Check for tkinter (common miss on Linux)
        if ! "$GUI_VENV/bin/python3" -c "import tkinter" &>/dev/null; then
            echo "[WARN] tkinter not found in GUI venv."
            echo "       On Debian/Ubuntu, try: sudo apt-get install python3-tk"
            if [ "$START_GUI" -eq 1 ]; then
                echo "[ERROR] GUI cannot start without tkinter. Aborting."
                exit 1
            fi
        fi
    fi
fi

if [ "$DO_SETUP" -eq 1 ]; then
    echo "[run_all] Setup complete."
    exit 0
fi

# --- Execution ---

if [ "$DO_RESET" -eq 1 ]; then
    echo "[run_all] Resetting database..."
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

# Export SKIP_PIP_UPGRADE to speed up the sub-scripts since we already did it
export SKIP_PIP_UPGRADE=1

echo "[run_all] Launching ERP..."
( cd "$ERP_DIR" && ./run_erp.sh ) &
pids+=($!)

# Small delay so the ERP TCP port (6666) is up before the GUI / MES start
sleep 3

echo "[run_all] Launching MES..."
( cd "$MES_DIR" && ./run_mes.sh ) &
pids+=($!)

echo "[run_all] Launching Dashboard Service..."
if [ -f "erp/.venv/bin/python3" ]; then
    ( cd "$ROOT" && erp/.venv/bin/python3 dashboard_service.py ) &
    pids+=($!)
else
    echo "[run_all] WARN: ERP venv not found, Dashboard Service skipped."
fi

if [ "$START_GUI" -eq 1 ]; then
    sleep 2
    echo "[run_all] Launching Client Order GUI..."
    "$ROOT/.venv_gui/bin/python3" "$ROOT/client_gui.py" &
    pids+=($!)
fi

echo "[run_all] All processes started. Ctrl-C to stop everything."
wait