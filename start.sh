#!/usr/bin/env bash
# macOS/Linux equivalent of start.ps1: backend (FastAPI, :8000) and frontend
# dev server (Vite, :5173). Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d backend/.venv ]; then
  echo "First-time setup: creating backend/.venv and installing dependencies..."
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -r backend/requirements.txt
fi
if [ ! -d frontend/node_modules ]; then
  echo "First-time setup: npm install..."
  (cd frontend && npm install)
fi

(cd backend && .venv/bin/uvicorn app.main:app --port 8000 --reload) &
BACKEND_PID=$!
trap 'kill "$BACKEND_PID" 2>/dev/null' EXIT

echo "Backend:  http://localhost:8000  (API docs: http://localhost:8000/docs)"
echo "Frontend: http://localhost:5173"
cd frontend && npm run dev
