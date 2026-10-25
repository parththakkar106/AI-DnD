"""The app the old host runs after the move to a new domain.

Every page request is redirected to the same path on `AIDND_REDIRECT_TO`,
through the new host's `/api/auth/handoff`. If the browser holds a session
cookie, the redirect carries a short-lived handoff token for it, so guests and
signed-in players arrive with their adventures. API calls from a tab left open
on the old host get a 410 whose message tells the player to reload.

This app never touches the database. It needs only `AIDND_SECRET_KEY`, which
must match the new host's, because both sides sign with it.

Run it in place of the main app:

    uvicorn app.redirect:app
"""

import os
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from . import security
from .auth import SESSION_COOKIE

TARGET = os.environ.get("AIDND_REDIRECT_TO", "").strip().rstrip("/")
if not TARGET:
    raise RuntimeError("AIDND_REDIRECT_TO must be set to the new host's URL.")


async def redirect(request: Request):
    path = request.url.path
    if path == "/api/health":
        # Render's health check. Redirecting it would mark the service down.
        return JSONResponse({"ok": True})
    if path.startswith("/api/"):
        return JSONResponse(
            {"detail": f"AI D&D has moved to {TARGET}. Reload the page to continue."},
            status_code=410,
        )
    next_path = path + (f"?{request.url.query}" if request.url.query else "")
    params = {"next": next_path}
    user_id = security.verify_session(request.cookies.get(SESSION_COOKIE, ""))
    if user_id is not None:
        params["t"] = security.sign_handoff(user_id)
    # 302, not 301. A browser caches a 301 forever, and the cached copy would
    # replay an expired token.
    return RedirectResponse(
        f"{TARGET}/api/auth/handoff?{urlencode(params)}", status_code=302
    )


app = Starlette(
    routes=[Route("/{path:path}", redirect, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])]
)
