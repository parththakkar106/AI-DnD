from fastapi import APIRouter

from .. import debuglog

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/requests")
def recent_requests():
    """Most-recent-first log of provider requests/responses (no API keys)."""
    return debuglog.recent()
