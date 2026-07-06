from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .database import engine
from .migrations import bootstrap
from .routers import adventures, debug, scenarios, scripts, settings, story_cards

bootstrap(engine)

app = FastAPI(title="AI D&D")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scenarios.router)
app.include_router(adventures.router)
app.include_router(story_cards.router)
app.include_router(scripts.router)
app.include_router(settings.router)
app.include_router(debug.router)


@app.get("/api/health")
def health():
    return {"ok": True}


# In production, serve the built frontend (frontend/dist) as static files.
class SPAStaticFiles(StaticFiles):
    """Serve index.html for unknown paths so client-side routes (/play/3)
    survive a page reload. API routes are matched before this mount."""

    async def get_response(self, path, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", SPAStaticFiles(directory=frontend_dist, html=True), name="frontend")
