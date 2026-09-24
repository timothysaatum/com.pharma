#!/bin/bash
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${SECRET_KEY:?SECRET_KEY must be set}"
: "${DATABASE_URL:?DATABASE_URL must be set}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Kill any existing servers
pkill -f "uvicorn.*8001" 2>/dev/null || true
pkill -f "vite.*1420" 2>/dev/null || true

# Start backend on port 8001
cd "$ROOT_DIR/backend.laso"
"$PYTHON_BIN" -m uvicorn main:app --host 0.0.0.0 --port 8001 --workers 1 > /tmp/uvicorn2.log 2>&1 &
BACKEND_PID=$!
sleep 8

# Check backend
curl -s http://127.0.0.1:8001/health || (echo "Backend failed"; cat /tmp/uvicorn2.log; exit 1)

# Start frontend
cd "$ROOT_DIR/ui.laso"
nohup npx vite dev --host 0.0.0.0 --port 1420 > /tmp/vite.log 2>&1 &
FRONTEND_PID=$!
sleep 10

# Check frontend
curl -s http://127.0.0.1:1420/ > /dev/null || (echo "Frontend failed"; cat /tmp/vite.log; exit 1)

# Run Playwright test
./node_modules/.bin/playwright test tests/e2e/test_baseurl.spec.ts --reporter=line
TEST_EXIT=$?

# Cleanup
kill $BACKEND_PID 2>/dev/null || true
kill $FRONTEND_PID 2>/dev/null || true

exit $TEST_EXIT