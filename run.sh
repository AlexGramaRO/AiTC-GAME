#!/bin/bash
# Run script for ATC RAMP CONTROL (Flask). Optimized for offline / LAN-only use.
# NOTE: If this folder lives on iCloud Desktop, pin it locally before going offline:
#   ./scripts/pin-project-local.sh

echo "🚀 Starting web application..." >&2

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

VENV_PY="$SCRIPT_DIR/venv/bin/python"
SERVER_FILE="$SCRIPT_DIR/app.py"
HOST="${ATC_HOST:-0.0.0.0}"
PORT="${ATC_PORT:-5000}"
FLASK_LOG="/tmp/flask_server_${PORT}.log"
RUN_PY=""
OFFLINE_MODE=0

# ---- Internet detection (fast; must not hang on captive/offline Wi‑Fi) ----

has_internet_access() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --connect-timeout 1 --max-time 2 -o /dev/null \
            "https://captive.apple.com/hotspot-detect.html" 2>/dev/null && return 0
        curl -fsS --connect-timeout 1 --max-time 2 -o /dev/null \
            "https://1.1.1.1" 2>/dev/null && return 0
    fi
    if command -v nc >/dev/null 2>&1; then
        nc -z -G 2 1.1.1.1 443 2>/dev/null && return 0
        nc -z -G 2 8.8.8.8 443 2>/dev/null && return 0
    fi
    return 1
}

# ---- Offline startup: bypass venv launcher (realpath/site can timeout on iCloud / no internet) ----

read_pyvenv_base_executable() {
    local cfg="$SCRIPT_DIR/venv/pyvenv.cfg"
    if [ ! -f "$cfg" ]; then
        return 1
    fi
    grep '^executable = ' "$cfg" 2>/dev/null | sed 's/^executable = //' | head -1
}

read_pyvenv_home() {
    local cfg="$SCRIPT_DIR/venv/pyvenv.cfg"
    if [ ! -f "$cfg" ]; then
        return 1
    fi
    grep '^home = ' "$cfg" 2>/dev/null | sed 's/^home = //' | head -1
}

python_smoke_test() {
    local py="$1"
    [ -n "$py" ] && [ -x "$py" ] || return 1
    "$py" -c "import flask" >/dev/null 2>&1
}

ensure_project_pinned_locally() {
    local helper="$SCRIPT_DIR/scripts/icloud-materialize.sh"
    if [ ! -f "$helper" ]; then
        return 0
    fi
    echo "📥 Offline mode: ensuring project files are local (iCloud pin)..." >&2
    # shellcheck source=/dev/null
    source "$helper"
    icloud_materialize_tree "$SCRIPT_DIR" 2>/dev/null || true
}

resolve_offline_python() {
    local base_py py_ver site_pkgs home_dir

    ensure_project_pinned_locally

    base_py="$(read_pyvenv_base_executable || true)"
    if [ -z "$base_py" ] || [ ! -x "$base_py" ]; then
        home_dir="$(read_pyvenv_home || true)"
        if [ -n "$home_dir" ]; then
            for candidate in \
                "$home_dir/python3.14" \
                "$home_dir/python3" \
                "$home_dir/python"; do
                if [ -x "$candidate" ]; then
                    base_py="$candidate"
                    break
                fi
            done
        fi
    fi

    if [ -z "$base_py" ] || [ ! -x "$base_py" ]; then
        echo -e "${RED}❌ Error: Could not find base Python for offline mode.${NC}"
        echo "   Check venv/pyvenv.cfg (executable / home paths)."
        echo "   Run ./setup.sh once while online, or move the project out of iCloud Desktop."
        return 1
    fi

    py_ver="$("$base_py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)" || {
        echo -e "${RED}❌ Error: Base Python exists but failed to start: $base_py${NC}"
        echo "   If the project is on iCloud Desktop, run ./scripts/pin-project-local.sh while online."
        return 1
    }

    site_pkgs="$SCRIPT_DIR/venv/lib/python${py_ver}/site-packages"
    if [ ! -d "$site_pkgs" ]; then
        echo -e "${RED}❌ Error: venv site-packages not found: $site_pkgs${NC}"
        echo "   Run ./setup.sh while you have internet."
        return 1
    fi

    export VIRTUAL_ENV="$SCRIPT_DIR/venv"
    export PYTHONNOUSERSITE=1
    if [ -n "${PYTHONPATH:-}" ]; then
        export PYTHONPATH="$site_pkgs:$PYTHONPATH"
    else
        export PYTHONPATH="$site_pkgs"
    fi

    RUN_PY="$base_py"

    if ! python_smoke_test "$RUN_PY"; then
        echo -e "${RED}❌ Error: Offline Python cannot import Flask.${NC}"
        echo "   Run ./setup.sh once while online to install dependencies into venv."
        return 1
    fi

    return 0
}

prepare_run_python() {
    if has_internet_access; then
        OFFLINE_MODE=0
        RUN_PY="$VENV_PY"
        return 0
    fi

    OFFLINE_MODE=1
    echo -e "${YELLOW}📴 No internet detected — using offline startup (direct base Python, no venv launcher).${NC}"
    resolve_offline_python
}

port_is_listening() {
    local port=$1
    "$RUN_PY" - "$port" <<'PY' 2>/dev/null
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(0.4)
try:
    s.connect(('127.0.0.1', port))
    sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

check_port_free() {
    ! port_is_listening "$1"
}

find_available_port() {
    local start_port=$1
    local i port
    for i in $(seq 0 10); do
        port=$((start_port + i))
        if check_port_free "$port"; then
            echo "$port"
            return 0
        fi
    done
    return 1
}

wait_for_server_ready() {
    local port=$1
    local pid=$2
    local log_file=$3
    local max_attempts=480
    local attempt=0
    local last_progress=-1

    while [ "$attempt" -lt "$max_attempts" ]; do
        if ! ps -p "$pid" > /dev/null 2>&1; then
            echo -e "${RED}❌ Error: Server process exited before it was ready.${NC}"
            if [ -f "$log_file" ]; then
                echo "📋 Server logs:"
                cat "$log_file"
            fi
            if [ "$OFFLINE_MODE" -eq 1 ]; then
                echo ""
                echo -e "${YELLOW}Offline tips:${NC}"
                echo "  • Pin the project locally: ./scripts/pin-project-local.sh (while online)"
                echo "  • Or move it out of iCloud Desktop (e.g. ~/Projects/)"
                echo "  • Ensure ./setup.sh was run once while online"
            fi
            return 1
        fi
        if port_is_listening "$port"; then
            return 0
        fi
        local elapsed=$((attempt / 2))
        if [ "$elapsed" -ne "$last_progress" ] && [ $((elapsed % 5)) -eq 0 ] && [ "$elapsed" -gt 0 ]; then
            echo "   … still starting (${elapsed}s)"
            last_progress=$elapsed
        fi
        sleep 0.5
        attempt=$((attempt + 1))
    done

    echo -e "${RED}❌ Error: Server did not start listening on port $port within 240 seconds.${NC}"
    if [ -f "$log_file" ]; then
        echo "📋 Server logs:"
        cat "$log_file"
    fi
    return 1
}

echo "📁 Working directory: $(pwd)"

if [ ! -x "$VENV_PY" ]; then
    echo -e "${RED}❌ Error: venv not found. Run ./setup.sh first.${NC}"
    echo "   Expected: $VENV_PY"
    exit 1
fi

if [ ! -f "$SERVER_FILE" ]; then
    echo -e "${RED}❌ Error: app.py not found.${NC}"
    exit 1
fi

if ! prepare_run_python; then
    exit 1
fi

if [ "$OFFLINE_MODE" -eq 1 ]; then
    echo "🐍 Offline Python: $RUN_PY"
fi

if ! check_port_free "$PORT"; then
    echo "⚠️  Port $PORT is already in use (possibly AirPlay Receiver on macOS)"
    PORT="$(find_available_port "$PORT")" || {
        echo -e "${RED}❌ Error: Could not find an available port${NC}"
        exit 1
    }
    echo "✅ Using port $PORT"
    FLASK_LOG="/tmp/flask_server_${PORT}.log"
fi

echo "🌐 Starting Flask server on $HOST:$PORT ..."
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export NO_PROXY='*'
export no_proxy='*'
export PYTHONUNBUFFERED=1

"$RUN_PY" -u "$SERVER_FILE" --host="$HOST" --port="$PORT" > "$FLASK_LOG" 2>&1 &
SERVER_PID=$!
echo "📝 Server PID: $SERVER_PID"
echo "📝 Log file: $FLASK_LOG"

echo "⏳ Waiting for server to be ready on port $PORT..."
if ! wait_for_server_ready "$PORT" "$SERVER_PID" "$FLASK_LOG"; then
    kill "$SERVER_PID" 2>/dev/null || true
    exit 1
fi

echo "✅ Server is running and listening on port $PORT"

# Avoid `ifconfig | grep` — it can hang on offline/captive networks.
LOCAL_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)"

echo ""
echo -e "${GREEN}✅ Server is running!${NC}"
if [ "$OFFLINE_MODE" -eq 1 ]; then
    echo -e "${YELLOW}   (offline / LAN-only mode)${NC}"
fi
echo ""
echo "📍 Access your application at:"
echo "   Local:   http://localhost:$PORT"
echo "   Network: http://$LOCAL_IP:$PORT"
echo ""

if [[ "$OSTYPE" == darwin* ]]; then
    open "http://localhost:$PORT" 2>/dev/null &
fi

echo -e "${YELLOW}Press Ctrl+C to stop the server${NC}"
trap 'echo ""; echo "🛑 Stopping server..."; kill '"$SERVER_PID"' 2>/dev/null; exit 0' INT TERM
wait "$SERVER_PID"
