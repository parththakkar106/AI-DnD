"""An OpenAI-compatible endpoint backed by the local `claude` CLI.

Point AI-DnD's Settings at `http://127.0.0.1:8787/v1` and the turn engine talks
to Claude through your Claude Code subscription instead of an API key. Use it to
play the demos against a real model when you do not want to spend API credit,
and to check prompt changes against the model the deployed app actually meets.
The shim speaks the small part of the OpenAI protocol the app uses: a model
listing, and `chat/completions` in both streaming and non-streaming form.

Each request spawns one `claude --print` process. The CLI holds no conversation
state here, which suits AI-DnD, because the app assembles the whole prompt every
turn and expects a stateless endpoint.

Embeddings are not served. Leave the embedding model blank in Settings, or point
the memory bank at a real endpoint.

To use it:

1.  Start the shim from the backend virtualenv, which already has FastAPI and
    uvicorn:

        backend/.venv/Scripts/python.exe tools/claude_shim.py

2.  In Settings, set the provider to OpenAI-compatible, the base URL to
    `http://127.0.0.1:8787/v1`, and the API key to any non-empty string. The
    shim ignores the key. It authenticates as you, through the CLI.

3.  Pick `sonnet` as the model, then run the connection test.

Set the reasoning budget to `0` or `-1`. A positive budget sends a
`reasoning.max_tokens` field that Claude 5 models reject with a 400.

Options:

    --port      Port to listen on. Defaults to 8787.
    --claude    Path to the `claude` executable. Defaults to `$AIDND_CLAUDE_BIN`,
                then to whatever is on `PATH`, then to the per-user install
                under `~/.local/bin`.

Run this only against a local backend. The endpoint has no authentication, and
anything that reaches it spends your Claude subscription quota. The SSRF guard
in `app/netguard.py` blocks localhost endpoints when `AIDND_MULTI_USER` is set,
so a deployed instance cannot be pointed here.
"""
import argparse
import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def find_claude() -> str:
    """Returns the path to the `claude` executable.

    The per-user install is the last resort, because `PATH` is what a developer
    controls. Windows needs the `.exe` spelled out, since `create_subprocess_exec`
    does no extension search.
    """
    override = os.environ.get("AIDND_CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude.exe", "claude"):
        candidate = os.path.expanduser(os.path.join("~", ".local", "bin", name))
        if os.path.exists(candidate):
            return candidate
    return "claude"


CLAUDE = find_claude()
DEFAULT_PORT = 8787

# The listing the Settings page shows. The CLI accepts both the aliases and the
# full ids, so both are offered.
MODELS = [
    "opus",
    "sonnet",
    "haiku",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
]

# Flags that strip the coding agent down to a text generator. `--system-prompt-file`
# replaces Claude Code's own system prompt, `--restricted` removes the tools that
# run commands, and the empty MCP config keeps the connected servers out of the
# tool list. An empty `--mcp-config` value is rejected: the key has to be present.
BASE_FLAGS = [
    "--print",
    "--restricted",
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers": {}}',
    "--disallowed-tools",
    "Read Write Edit Glob Grep WebSearch WebFetch Task Skill ToolSearch",
]

app = FastAPI()


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]}


@app.post("/v1/embeddings")
async def embeddings():
    return JSONResponse(
        {"error": {"message": "This shim does not serve embeddings. Leave the "
                              "embedding model blank, or point the memory bank "
                              "at a real endpoint."}},
        status_code=501,
    )


def split_prompt(messages: list[dict]) -> tuple[str, str]:
    """Splits an OpenAI message list into a system prompt and a user prompt.

    The system messages become the CLI's `--system-prompt-file`. Everything else
    is joined into one prompt sent on stdin. AI-DnD sends exactly one system and
    one user message per turn, so the labeling only matters for the AI Chat page.
    """
    system_parts, turn_parts = [], []
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):  # Content-block form, if a caller uses it.
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        if m.get("role") == "system":
            system_parts.append(content)
        elif len(messages) <= 2:
            turn_parts.append(content)
        else:
            turn_parts.append(f"{m.get('role', 'user').title()}: {content}")
    return "\n\n".join(system_parts), "\n\n".join(turn_parts)


async def run_claude(system: str, prompt: str, model: str):
    """Yields `(kind, text)` pairs from one `claude --print` run.

    `kind` is `"text"` for narration, `"usage"` for the final token accounting,
    and `"error"` for a failure the CLI reported in its result record.
    """
    fd, sys_path = tempfile.mkstemp(suffix=".txt", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(system or "You are a helpful assistant.")
    try:
        args = [
            CLAUDE, *BASE_FLAGS,
            "--system-prompt-file", sys_path,
            "--model", model,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(),
        )
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "stream_event":
                event = rec.get("event") or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield "text", delta.get("text", "")
            elif rec.get("type") == "result":
                if rec.get("is_error"):
                    yield "error", str(rec.get("result") or "claude failed")
                usage = rec.get("usage") or {}
                yield "usage", {
                    "prompt_tokens": (usage.get("input_tokens", 0)
                                      + usage.get("cache_read_input_tokens", 0)
                                      + usage.get("cache_creation_input_tokens", 0)),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": 0,
                    "prompt_tokens_details": {
                        "cached_tokens": usage.get("cache_read_input_tokens", 0),
                    },
                    "cost_usd": rec.get("total_cost_usd"),
                }
        await proc.wait()
        if proc.returncode != 0:
            detail = (await proc.stderr.read()).decode("utf-8", errors="replace")
            yield "error", f"claude exited {proc.returncode}: {detail[:500]}"
    finally:
        os.unlink(sys_path)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model") or "sonnet"
    system, prompt = split_prompt(body.get("messages") or [])
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if not body.get("stream"):
        text, usage, error = "", {}, None
        async for kind, value in run_claude(system, prompt, model):
            if kind == "text":
                text += value
            elif kind == "usage":
                usage = value
            else:
                error = value
        if error and not text:
            return JSONResponse({"error": {"message": error}}, status_code=502)
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage,
        }

    async def sse():
        def chunk(delta: dict, finish=None, usage=None) -> str:
            payload = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage:
                payload["usage"] = usage
            return f"data: {json.dumps(payload)}\n\n"

        yield chunk({"role": "assistant", "content": ""})
        sent = False
        async for kind, value in run_claude(system, prompt, model):
            if kind == "text" and value:
                sent = True
                yield chunk({"content": value})
            elif kind == "usage":
                yield chunk({}, finish="stop", usage=value)
            elif kind == "error" and not sent:
                yield chunk({"content": f"[shim error] {value}"}, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--claude", default=CLAUDE, help="Path to the claude executable.")
    args = parser.parse_args()
    CLAUDE = args.claude
    print(f"claude binary: {CLAUDE}")
    print(f"Point AI-DnD Settings at http://127.0.0.1:{args.port}/v1")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
