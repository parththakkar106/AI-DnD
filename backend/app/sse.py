"""Server-sent events: the wire format, the headers, and the error frame.

Two routers stream: the turn engine in `routers/adventures/turns.py` and the
chat scratchpad in `routers/chat.py`. Both send JSON objects as SSE `data:`
frames, so the format lives here rather than in either one.
"""
import json

from . import analytics

# `no-cache` stops an intermediary from caching the stream. `X-Accel-Buffering`
# makes nginx-style reverse proxies, which hosted deploys use, flush each event
# immediately rather than buffer it.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse(obj: dict) -> str:
    """Returns one SSE frame carrying `obj` as JSON."""
    return f"data: {json.dumps(obj)}\n\n"


async def relay(events, chunks: list[str], reasoning_chunks: list[str]):
    """Yields one SSE frame per `(kind, chunk)` pair from a provider stream.

    The text goes into `chunks` and the reasoning into `reasoning_chunks`, so the
    caller can read the whole reply when the stream ends.
    """
    async for kind, chunk in events:
        if kind == "reasoning":
            reasoning_chunks.append(chunk)
            yield sse({"type": "reasoning", "text": chunk})
        else:
            chunks.append(chunk)
            yield sse({"type": "chunk", "text": chunk})


def turn_error(detail: str, **extra) -> str:
    """Returns an SSE error for a turn that could not be produced, and counts it.

    A failed turn is still an HTTP 200 response, so the middleware's status-code
    tally cannot see it. This metric exists so that a demo whose model refuses
    every request does not report as healthy.
    """
    analytics.record(analytics.M_EVENT, analytics.EV_TURN_ERROR)
    return sse({"type": "error", "detail": detail, **extra})
