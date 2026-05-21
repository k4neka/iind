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
for arg in "$@"; do
    case "$arg" in
        --gui|-g) START_GUI=1 ;;
        --help|-h)
            echo "Usage: $0 [--gui]"
            echo "  --gui    also launch the Client Order GUI"
            exit 0
            ;;
    esac
done

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

if [ "$START_GUI" -eq 1 ]; then
    sleep 2
    echo "[run_all] Launching Client Order GUI..."

    # Use a dedicated venv for the GUI so we don't pollute ERP/MES envs
    GUI_VENV="$ROOT/.venv_gui"
    if [ ! -d "$GUI_VENV" ]; then
        python3 -m venv "$GUI_VENV"
    fi
    # shellcheck disable=SC1091
    source "$GUI_VENV/bin/activate"
    pip install --quiet --upgrade pip
    # Tkinter is shipped with CPython on most distros; nothing to pip install.
    # (If on Debian/Ubuntu you may need: sudo apt-get install python3-tk)

    python "$ROOT/client_gui.py" &
    pids+=($!)
fi

echo "[run_all] All processes started. Ctrl-C to stop everything."
wait