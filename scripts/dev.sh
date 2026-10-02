#!/usr/bin/env bash
# Start / stop / status for the Laso dev stack (backend + frontend) in WSL.
#
# Usage:
#   ./scripts/dev.sh start     # start both
#   ./scripts/dev.sh stop      # stop both
#   ./scripts/dev.sh restart   # stop then start
#   ./scripts/dev.sh status    # listeners, health, logs
#   ./scripts/dev.sh logs      # tail both logs
#
# Ports
#   backend  8100  - NOT 8000. Windows already binds 127.0.0.1:8000 with
#                    KratosElectionServer, and a native listener wins over WSL
#                    localhost forwarding, so the API would be unreachable
#                    from a Windows browser on 8000. Override with PORT=xxxx.
#   frontend 1420  - pinned by ui.laso/vite.config.ts (strictPort: true).
#
# Toolchain (WSL, no sudo required)
#   python  /home/ubuntu/lasoenv              CPython 3.12 via uv
#   node    ~/.local/node                     Node 22 LTS tarball
#   pnpm    corepack, pinned to the version in ui.laso/package.json
#
# Python 3.12 is required, not optional: requirements.txt pins numpy==1.26.4,
# pandas==2.1.4 and psycopg2-binary==2.9.9, which publish no cp314 wheels, and
# this image's system Python is 3.14. uvloop is Linux/macOS only, which is why
# the backend runs in WSL rather than on the Windows host.
set -uo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend.laso"
UI_DIR="$PROJECT_DIR/ui.laso"

PORT="${PORT:-8100}"
FRONTEND_PORT="${FRONTEND_PORT:-1420}"

export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"
PY="$HOME/lasoenv/bin/python"
BACKEND_LOG=/tmp/laso-backend.log
FRONTEND_LOG=/tmp/laso-frontend.log

# /proc/<pid>/cwd reports a fully resolved Linux path, so compare against the
# physical path. Do NOT route this through `wslpath -m`: that translates to
# Windows form (C:/Users/...) while /proc reports /mnt/c/Users/..., so the
# ownership check would reject the project's own server.
wsl_cwd() {
    (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"
}

# Stop the listener on a port only when its cwd belongs to this project.
# Deliberately never pattern-matches on the process name: a bare
# `pkill -f uvicorn` or `pkill -f vite` on a shared box kills unrelated work.
stop_own_listener() {
    local port="$1" want_cwd="$2" label="$3"
    local pids pid owner
    pids=$(ss -lntpH "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u) || true
    [ -z "$pids" ] && return 0
    for pid in $pids; do
        owner=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
        case "$owner" in
            "$want_cwd"|"$want_cwd"/*)
                echo "  stopping $label (pid $pid)"
                kill "$pid" 2>/dev/null || true
                ;;
            *)
                echo "  ERROR: $label port $port held by pid $pid (cwd: ${owner:-unknown}), not ours." >&2
                echo "         Refusing to kill it. Stop it yourself, or set ${4:-PORT}=<free port>." >&2
                return 1
                ;;
        esac
    done
    sleep 2
}

wait_for_http() {
    local url="$1" tries="${2:-90}" i code
    for i in $(seq 1 "$tries"); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)
        [ "$code" = "200" ] && { echo "  up after ${i}s"; return 0; }
        sleep 1
    done
    echo "  FAILED to answer: $url" >&2
    return 1
}

start_backend() {
    echo "backend -> :$PORT"
    cd "$BACKEND_DIR" || return 1
    # setsid detaches into a new session; without it the server dies with the
    # shell that launched it. The python entry point is invoked directly so
    # there is no wrapper process to reap.
    setsid nohup "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" \
        </dev/null > "$BACKEND_LOG" 2>&1 &
    disown 2>/dev/null || true
    wait_for_http "http://127.0.0.1:$PORT/health"
}

start_frontend() {
    echo "frontend -> :$FRONTEND_PORT"
    cd "$UI_DIR" || return 1
    [ -d node_modules ] || { echo "  ERROR: ui.laso/node_modules missing. Run: pnpm install" >&2; return 1; }
    setsid nohup "$HOME/.local/node/bin/node" ./node_modules/vite/bin/vite.js \
        --host 0.0.0.0 --port "$FRONTEND_PORT" \
        </dev/null > "$FRONTEND_LOG" 2>&1 &
    disown 2>/dev/null || true
    wait_for_http "http://127.0.0.1:$FRONTEND_PORT/"
}

do_start() {
    stop_own_listener "$PORT" "$(wsl_cwd "$BACKEND_DIR")" "backend" "PORT" || return 1
    stop_own_listener "$FRONTEND_PORT" "$(wsl_cwd "$UI_DIR")" "frontend" "FRONTEND_PORT" || return 1
    start_backend || return 1
    start_frontend || return 1
    echo
    do_status
}

do_stop() {
    local rc=0
    stop_own_listener "$PORT" "$(wsl_cwd "$BACKEND_DIR")" "backend" "PORT" || rc=1
    stop_own_listener "$FRONTEND_PORT" "$(wsl_cwd "$UI_DIR")" "frontend" "FRONTEND_PORT" || rc=1
    if [ "$rc" -eq 0 ]; then
        echo "  stopped"
    else
        echo "  stopped what was ours; refused to touch the rest (see errors above)" >&2
    fi
    return "$rc"
}

do_status() {
    echo "listeners"
    ss -lntp 2>/dev/null | grep -E ":($PORT|$FRONTEND_PORT)\b" || echo "  none"
    echo
    echo "backend health"
    curl -s -m 10 "http://127.0.0.1:$PORT/health" && echo || echo "  (no answer)"
    echo
    echo "frontend http"
    curl -s -o /dev/null -m 10 -w '  HTTP %{http_code}\n' "http://127.0.0.1:$FRONTEND_PORT/"
    echo
    echo "WSL IP (reachable from the Windows host)"
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "  $IP  backend http://$IP:$PORT/health   frontend http://$IP:$FRONTEND_PORT/"
    echo
    echo "logs: $BACKEND_LOG  $FRONTEND_LOG"
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    logs)    tail -n 40 -F "$BACKEND_LOG" "$FRONTEND_LOG" ;;
    *)
        echo "usage: $0 {start|stop|restart|status|logs}" >&2
        exit 2
        ;;
esac
