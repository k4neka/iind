#!/usr/bin/env bash
# Launches the whole flexible-production-line stack: ERP, MES and the cloud
# dashboard service, with the optional Client Order GUI. Each component gets
# its own venv (pip guaranteed via ensurepip / get-pip fallback) and its
# requirements installed. The dashboard connection info (host(s) + WS port) is
# printed once everything is up.
#
# Usage:
#   ./run_all.sh             # ERP + MES + dashboard service
#   ./run_all.sh --gui       # ... plus the Client Order GUI
#   ./run_all.sh -g          # same as --gui
#
# NOTE: the browser dashboard talks MQTT-over-WebSocket to the broker on :9001.
# Start the broker with the bundled config so that listener exists:
#   mosquitto -c dashboards/mosquitto_ws.conf

ROOT="$(cd "$(dirname "$0")" && pwd)"
ERP_DIR="$ROOT/erp"
MES_DIR="$ROOT/mes"
DASH_DIR="$ROOT/dashboards"
DASH_VENV="$ROOT/.venv_dashboard"
GUI_VENV="$ROOT/.venv_gui"

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

# --------------------------------------------------------------------------
# Robust venv + pip bootstrap. python3 -m venv usually ships pip, but on some
# stripped distros it doesn't; fall back to ensurepip, then to get-pip.py.
# --------------------------------------------------------------------------
ensure_pip() {
    local py="$1"
    if "$py" -m pip --version >/dev/null 2>&1; then return 0; fi
    echo "[run_all] pip not found in venv; trying ensurepip..."
    if "$py" -m ensurepip --upgrade >/dev/null 2>&1; then return 0; fi
    echo "[run_all] ensurepip unavailable; downloading get-pip.py..."
    local tmp; tmp="$(mktemp /tmp/get-pip-XXXXXX.py)"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$tmp"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$tmp" https://bootstrap.pypa.io/get-pip.py
    else
        echo "[run_all] ERROR: neither curl nor wget available to fetch pip."
        rm -f "$tmp"; return 1
    fi
    "$py" "$tmp"; local rc=$?; rm -f "$tmp"; return $rc
}

bootstrap_venv() {
    # $1 = venv dir, $2 = requirements file ("" for none), $3 = label
    # Fast path: when the venv already exists and requirements.txt is unchanged
    # (matched against a stored hash), skip pip entirely -- pip install over the
    # /mnt/c filesystem is the slow part of startup, so this makes repeat
    # launches near-instant.
    local dir="$1" req="$2" label="$3"
    local py="$dir/bin/python"
    local created=0
    if [ ! -x "$py" ]; then
        echo "[run_all] [$label] creating venv at $dir"
        python3 -m venv "$dir" 2>/dev/null || python3 -m venv --without-pip "$dir"
        created=1
    fi
    ensure_pip "$py" || { echo "[run_all] [$label] could not provision pip"; return 1; }
    # Only upgrade pip when the venv was just created (avoids a network call
    # on every launch).
    if [ "$created" -eq 1 ]; then
        "$py" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    fi
    if [ -n "$req" ] && [ -f "$req" ]; then
        local stamp="$dir/.req_hash"
        local newhash; newhash="$(md5sum "$req" 2>/dev/null | awk '{print $1}')"
        if [ "$created" -eq 1 ] || [ ! -f "$stamp" ] \
                || [ "$(cat "$stamp" 2>/dev/null)" != "$newhash" ]; then
            echo "[run_all] [$label] installing $req"
            if "$py" -m pip install --quiet -r "$req"; then
                echo "$newhash" >"$stamp"
            else
                echo "[run_all] [$label] WARN: requirements install failed"
            fi
        else
            echo "[run_all] [$label] deps up to date (skipping pip)"
        fi
    fi
}

wait_for_port() {
    # $1 = port, $2 = max tries (0.5s each, default 60 -> 30s)
    local port="$1" tries="${2:-60}" i
    for ((i = 0; i < tries; i++)); do
        if (echo >"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1; then return 0; fi
        sleep 0.5
    done
    return 1
}

# --------------------------------------------------------------------------
# Provision venvs.
# --------------------------------------------------------------------------
echo "[run_all] Provisioning virtual environments..."
bootstrap_venv "$ERP_DIR/.venv"  "$ERP_DIR/requirements.txt"  erp
bootstrap_venv "$MES_DIR/.venv"  "$MES_DIR/requirements.txt"  mes
bootstrap_venv "$DASH_VENV"      "$DASH_DIR/requirements.txt" dashboard

# --------------------------------------------------------------------------
# ERP -> wait :6666 -> MES -> dashboard service.
# --------------------------------------------------------------------------
echo "[run_all] Launching ERP..."
( cd "$ERP_DIR" && exec "$ERP_DIR/.venv/bin/python" main.py ) &
pids+=($!)

echo "[run_all] Waiting for ERP TCP port 6666..."
if wait_for_port 6666; then
    echo "[run_all] ERP is accepting client orders on :6666"
else
    echo "[run_all] WARN: ERP :6666 not up yet; continuing anyway"
fi

echo "[run_all] Launching MES..."
( cd "$MES_DIR" && exec "$MES_DIR/.venv/bin/python" main.py ) &
pids+=($!)

echo "[run_all] Launching dashboard service..."
( cd "$ROOT" && exec "$DASH_VENV/bin/python" dashboards/dashboard_service.py ) &
pids+=($!)

# Give the dashboard service a moment to print + write its connection info.
sleep 2
echo
echo "===================== DASHBOARD CONNECTION ====================="
if [ -f "$DASH_DIR/.dashboard_conn" ]; then
    cat "$DASH_DIR/.dashboard_conn"
else
    echo "Open dashboards/dashboard.html and connect MQTT-WS to <HOST>:9001"
fi
# Is the broker's WebSocket listener actually up?
if wait_for_port 9001 2; then
    echo "MQTT WebSocket listener on :9001 is UP."
else
    echo "NOTE: no MQTT WebSocket listener on :9001 -> the browser cannot connect."
    echo "      Start the broker with: mosquitto -c dashboards/mosquitto_ws.conf"
fi
echo "==============================================================="
echo

# --------------------------------------------------------------------------
# Optional Client Order GUI (stdlib + tkinter only).
# --------------------------------------------------------------------------
if [ "$START_GUI" -eq 1 ]; then
    echo "[run_all] Launching Client Order GUI..."
    bootstrap_venv "$GUI_VENV" "" gui
    # Tkinter ships with CPython on most distros; on Debian/Ubuntu you may need
    # 'sudo apt-get install python3-tk' if the GUI fails to import tkinter.
    ( cd "$ROOT" && exec "$GUI_VENV/bin/python" client_gui.py ) &
    pids+=($!)
fi

echo "[run_all] All processes started. Ctrl-C to stop everything."
wait
