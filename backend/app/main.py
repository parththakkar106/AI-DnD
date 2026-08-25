import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import analytics, cleanup
from .auth import MULTI_USER
from .database import engine
from .limits import BodySizeLimitMiddleware
from .migrations import bootstrap
from .routers import (
    adventures, analytics as analytics_router, auth, chat, debug, scenarios, scripts,
    settings, story_cards,
)
from .seed import seed_public_scenarios

bootstrap(engine)
seed_public_scenarios(engine)

# Production serves the SPA same-origin, so CORS only matters for the Vite dev
# server; AIDND_CORS_ORIGINS overrides for any other cross-origin setup.
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("AIDND_CORS_ORIGINS", "").split(",")
    if o.strip()
] or ["http://localhost:5173", "http://127.0.0.1:5173"]

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Sweeps once on boot, then on an interval. Booting is the reliable
    # trigger on Render's free tier, where the service sleeps after ~15
    # minutes and a long-running timer rarely gets to fire.
    sweeper = cleanup.start_sweeper()
    # Visit counters are buffered in memory and written in batches; this is
    # what turns them into rows, and stop_flusher writes out the last batch so
    # a deploy doesn't drop it.
    flusher = analytics.start_flusher()
    try:
        yield
    finally:
        await cleanup.stop_sweeper(sweeper)
        await analytics.stop_flusher(flusher)


# The interactive API docs stay local-only: in multi-user mode they just hand
# strangers a map of the API surface.
app = FastAPI(
    title="AI D&D",
    docs_url=None if MULTI_USER else "/docs",
    redoc_url=None,
    openapi_url=None if MULTI_USER else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(BodySizeLimitMiddleware)


class SecurityHeadersMiddleware:
    """Standard hardening headers on every response. Pure ASGI (wraps `send`)
    so SSE streams pass through unbuffered. The CSP allows exactly what the
    SPA uses: same-origin everything, inline styles (React), Google Fonts."""

    _HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"x-frame-options", b"DENY"),
        (
            b"content-security-policy",
            b"default-src 'self'; "
            b"script-src 'self'; "
            b"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            b"font-src https://fonts.gstatic.com; "
            b"img-src 'self' data:; "
            b"connect-src 'self'; "
            b"frame-ancestors 'none'",
        ),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + self._HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ApiErrorMiddleware:
    """Counts failed API responses for the analytics dashboard.

    This is middleware rather than an exception handler, because it observes
    what the client received. A 429 from a rate limiter, a 404 from routing, and
    a 500 from a handler that never returned all reach it the same way. It is
    pure ASGI for the same reason as the headers above: an SSE turn must not be
    buffered on its way out. It watches `/api` only, because a 404 on the SPA
    mount is a page load rather than a fault.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api"):
            return await self.app(scope, receive, send)

        async def send_counting(message):
            if message["type"] == "http.response.start" and message["status"] >= 400:
                # The router has already put the matched route on the scope by
                # the time a response starts, so the label can name the
                # endpoint rather than the caller's path.
                analytics.record(
                    analytics.M_ERROR,
                    analytics.api_route_label(scope, message["status"]),
                )
            await send(message)

        await self.app(scope, receive, send_counting)


app.add_middleware(ApiErrorMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(scenarios.router)
app.include_router(adventures.router)
app.include_router(story_cards.router)
app.include_router(scripts.router)
app.include_router(settings.router)
app.include_router(chat.router)
app.include_router(debug.router)
app.include_router(analytics_router.router)


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
