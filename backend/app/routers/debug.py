from fastapi import APIRouter, HTTPException

from .. import auth, debuglog

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/requests")
def recent_requests():
    """Most-recent-first log of provider requests/responses (no API keys).

    The log is a single process-wide ring buffer with no per-user
    attribution, so in multi-user (hosted) mode it would leak other players'
    prompts — disabled there, available on local installs."""
    if auth.MULTI_USER:
        raise HTTPException(403, "The debug log is only available on local installs.")
    return debuglog.recent()
