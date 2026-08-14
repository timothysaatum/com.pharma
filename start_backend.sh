#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend.laso"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

# Free the port — but ONLY if the listener is this project's own server.
#
# The previous version ran `pkill -f "uvicorn.*main:app"`, which matches any
# FastAPI app on the host. On a shared box that silently killed unrelated
# services. Now we identify the listener by PID, compare its working directory
# to this backend, and refuse to touch anything that isn't ours.
stop_own_listener() {
    local pids
    pids="$(ss -lntpH "sport = :$PORT" 2>/dev/null \
        | grep -oP 'pid=\K[0-9]+' | sort -u)" || true
    [[ -z "$pids" ]] && return 0

    local pid owner
    for pid in $pids; do
        owner="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
        if [[ "$owner" == "$BACKEND_DIR" ]]; then
            echo "Stopping previous backend (pid $pid) on port $PORT…"
            kill "$pid" 2>/dev/null || true
        else
            echo "ERROR: port $PORT is held by pid $pid (cwd: ${owner:-unknown})," >&2
            echo "       which is NOT this project. Refusing to kill it." >&2
            echo "       Set PORT=<free-port> to run elsewhere." >&2
            exit 1
        fi
    done
    sleep 2
}
stop_own_listener

# Navigate to backend directory and start the server
cd "$BACKEND_DIR"

# Activate a project virtual environment when present.
if [[ -f "$PROJECT_DIR/.venv/bin/activate" ]]; then
    source "$PROJECT_DIR/.venv/bin/activate"
elif [[ -f "$BACKEND_DIR/.venv/bin/activate" ]]; then
    source "$BACKEND_DIR/.venv/bin/activate"
fi

PYTHON_BIN="$(command -v python || command -v python3)"

# Bring the database schema up to date before accepting requests.
"$PYTHON_BIN" -m alembic upgrade head

# Start backend
"$PYTHON_BIN" -m uvicorn main:app --host "$HOST" --port "$PORT" --reload
