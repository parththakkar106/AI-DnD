# AI D&D — local AI Dungeon-style storytelling app

Python (FastAPI + SQLite) backend, React (Vite) frontend, connectable to any
OpenAI-compatible AI endpoint (Ollama, LM Studio, OpenAI, OpenRouter, …).

Plan: see [`plan/00-OVERVIEW.md`](plan/00-OVERVIEW.md). Currently completed: **Phase 4** (AI Dungeon-compatible scripting).

## Run

```powershell
.\start.ps1
```

Then open http://localhost:5173. Backend API docs at http://localhost:8000/docs.

Or manually, in two terminals:

```powershell
# Terminal 1 — backend
cd backend
.\.venv\Scripts\uvicorn.exe app.main:app --port 8000 --reload

# Terminal 2 — frontend
cd frontend
npm run dev
```

## First-time setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt

cd ..\frontend
npm install
```

## Production-ish serving (single port)

Build the frontend, then the backend serves it statically:

```powershell
cd frontend
npm run build
# then run the backend and open http://localhost:8000
```

## Layout

- `backend/app/` — FastAPI app: `models.py` (SQLAlchemy), `schemas.py` (Pydantic), `routers/`
- `backend/data.db` — SQLite database (created on first run)
- `frontend/src/` — React SPA: `pages/`, `api.js`
- `plan/` — phased implementation plan
