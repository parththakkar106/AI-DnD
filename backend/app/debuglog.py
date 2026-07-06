"""In-memory ring buffer of recent provider requests/responses for the debug page.

API keys never enter the log: only the request body (which carries no
credentials) and response text are recorded, both truncated.
"""

import itertools
from collections import deque
from datetime import datetime, timezone

MAX_ENTRIES = 30
MAX_TEXT = 6000

_entries: deque[dict] = deque(maxlen=MAX_ENTRIES)
_ids = itertools.count(1)


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"\n… [{len(text) - MAX_TEXT} more chars truncated]"


def _clip_obj(obj):
    if isinstance(obj, str):
        return _clip(obj)
    if isinstance(obj, dict):
        return {k: _clip_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clip_obj(v) for v in obj]
    return obj


def start_entry(url: str, model: str, body: dict) -> dict:
    entry = {
        "id": next(_ids),
        "time": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "model": model,
        "request": _clip_obj(body),
        "status": "pending",
        "response": "",
        "error": None,
    }
    _entries.appendleft(entry)
    return entry


def finish_entry(entry: dict, *, response: str = "", error: str | None = None) -> None:
    entry["response"] = _clip(response)
    entry["error"] = error
    entry["status"] = "error" if error else "ok"


def recent() -> list[dict]:
    return list(_entries)
