#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend.laso"

# Kill any existing backend processes
pkill -f "uvicorn.*main:app" || true
pkill -f "python.*main\.py" || true

# Wait for ports to free up
sleep 2

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
"$PYTHON_BIN" -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
