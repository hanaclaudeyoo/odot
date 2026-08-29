#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x ".venv/bin/uvicorn" ]; then
  echo "Missing backend dependencies. Run:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r backend/requirements.txt"
  exit 1
fi

if [ ! -d "frontend/node_modules" ]; then
  echo "Missing frontend dependencies. Run:"
  echo "  npm install --prefix frontend"
  exit 1
fi

cleanup() {
  kill "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "Starting backend API on http://127.0.0.1:8000"
npm run backend:dev &
BACKEND_PID=$!

echo "Starting frontend app"
npm run frontend:dev &
FRONTEND_PID=$!

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done

wait "$BACKEND_PID" "$FRONTEND_PID"
