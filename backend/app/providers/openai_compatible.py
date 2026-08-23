import json
from typing import AsyncIterator

import httpx

from .. import debuglog, netguard
from .base import PromptParts, Provider, ProviderError

# Framing appended after the story text in chat mode, so chat-tuned models keep
# continuing prose instead of replying conversationally.
CHAT_CONTINUE_HINT = "\n\n[Continue the story directly. Output only story text.]"

# OpenRouter serves one model from whichever upstream is available, and every
# upstream holds its own prompt cache — so a request that lands somewhere new
# starts cold however stable the prompt is. Naming a preferred upstream makes
# routing deterministic, which is what lets a cache be hit at all.
#
# `allow_fallbacks` is deliberately left at its default of true: this is a
# preference, not a restriction. If the named upstream is down the request
# still goes through somewhere else and merely misses the cache, which is the
# behaviour we had anyway.
#
# A whitelist rather than a derivation from the model slug. The vendor half of
# a slug is *usually* the provider slug ("deepseek/..." -> "deepseek", verified
# against /api/v1/providers) but not reliably: Google's models are served by
# "google-ai-studio" and "google-vertex", and there is no "google". Look a
# vendor up on the model's Providers tab before adding it here — a slug that
# does not exist is a routing preference that silently does nothing at best.
_OPENROUTER_HOST = "openrouter.ai"
_PREFERRED_UPSTREAM = {"deepseek": "deepseek"}


# Completion endpoints have no roles, so a plain chat has to be flattened into
# one labelled transcript that trails off on "Assistant:" for the model to continue.
_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant"}


def flatten_messages(messages: list[dict]) -> str:
    turns = "\n\n".join(
        f"{_ROLE_LABELS.get(m['role'], m['role'])}: {m['content']}" for m in messages
    )
    return f"{turns}\n\nAssistant:"


class OpenAICompatibleProvider(Provider):
    """Adapter for any /v1-style endpoint: Ollama, LM Studio, OpenAI, OpenRouter, vLLM, Groq…"""

    def __init__(
        self,
        endpoint_url: str,
        api_key: str,
        model: str,
        api_mode: str = "chat",
        reasoning_max_tokens: int = 0,
    ):
        self.base_url = endpoint_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.api_mode = api_mode  # "chat" | "completion"
        # Thinking budget for reasoning models, on top of max_tokens. 0 = the
        # `reasoning` param is not sent (endpoints that don't know it may
        # reject unknown fields); negative = explicitly ask the endpoint to
        # turn reasoning off.
        self.reasoning_max_tokens = reasoning_max_tokens
        # Token accounting from the last call, when the endpoint reported any:
        # prompt/completion counts plus, on OpenRouter, `prompt_tokens_details.
        # cached_tokens` — the number of prompt tokens read from cache instead
        # of billed in full. Written by every request method, so a caller reads
        # it after the call it made; one provider is built per request.
        self.last_usage: dict | None = None

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _apply_reasoning_budget(self, body: dict) -> None:
        """Give reasoning models their own thinking budget (OpenRouter-style),
        raising max_tokens so the actual output keeps its full budget.

        A negative budget means the opposite: send `effort: "none"` to switch
        reasoning off on models that do it by default (DeepSeek V4 Flash, say).
        That's distinct from `exclude: true`, which still thinks — and bills —
        but hides the trace. Zero stays "send nothing at all" so endpoints that
        reject unknown fields (Ollama) keep working."""
        if self.api_mode != "chat":
            return
        if self.reasoning_max_tokens < 0:
            body["reasoning"] = {"effort": "none"}
        elif self.reasoning_max_tokens > 0:
            body["reasoning"] = {"max_tokens": self.reasoning_max_tokens}
            body["max_tokens"] += self.reasoning_max_tokens

    def _apply_provider_routing(self, body: dict) -> None:
        """Prefer one upstream on OpenRouter, so the prompt cache is warm.

        Silent no-op everywhere else: `provider` is an OpenRouter extension and
        Ollama and friends reject fields they do not know — the same trap the
        `reasoning` param above is written around."""
        if _OPENROUTER_HOST not in self.base_url:
            return
        upstream = _PREFERRED_UPSTREAM.get(self.model.split("/", 1)[0].lower())
        if upstream:
            body["provider"] = {"order": [upstream]}

    def _record_usage(self, payload: dict) -> None:
        """Record the endpoint's own token accounting, if it reported any.

        OpenRouter always reports usage now (`usage: {include: true}` and
        `stream_options` are deprecated no-ops), and in a stream it rides on a
        final chunk that carries no choices — which is why this is read
        separately from the text extraction rather than beside it."""
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage:
            self.last_usage = usage

    def _request(self, parts: PromptParts, temperature: float, max_tokens: int) -> tuple[str, dict]:
        if self.api_mode == "completion":
            url = f"{self.base_url}/completions"
            body = {
                "model": self.model,
                "prompt": f"{parts.system}\n\n{parts.story}",
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
        else:
            url = f"{self.base_url}/chat/completions"
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": parts.system},
                    {"role": "user", "content": parts.story + CHAT_CONTINUE_HINT},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
        self._apply_reasoning_budget(body)
        self._apply_provider_routing(body)
        return url, body

    @staticmethod
    def _extract_chunk(payload: dict) -> str:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        choice = choices[0]
        # chat stream → delta.content; completion stream → text;
        # non-stream fallbacks → message.content / text
        delta = choice.get("delta") or {}
        return (
            delta.get("content")
            or choice.get("text")
            or (choice.get("message") or {}).get("content")
            or ""
        )

    @staticmethod
    def _extract_reasoning(payload: dict) -> str:
        """Reasoning-model thinking: OpenRouter normalizes to `reasoning`;
        DeepSeek-style servers use `reasoning_content`."""
        choices = payload.get("choices") or []
        if not choices:
            return ""
        choice = choices[0]
        delta = choice.get("delta") or {}
        message = choice.get("message") or {}
        return (
            delta.get("reasoning")
            or delta.get("reasoning_content")
            or message.get("reasoning")
            or message.get("reasoning_content")
            or ""
        )

    async def generate(
        self, parts: PromptParts, *, temperature: float, max_tokens: int
    ) -> AsyncIterator[tuple[str, str]]:
        """Yields ("text" | "reasoning", chunk) pairs."""
        if not self.model:
            raise ProviderError("No model configured — set one in Settings.")
        url, body = self._request(parts, temperature, max_tokens)
        async for event in self._stream(url, body):
            yield event

    async def chat(
        self, messages: list[dict], *, temperature: float, max_tokens: int
    ) -> AsyncIterator[tuple[str, str]]:
        """Plain multi-turn chat — no story framing, no context assembly. Takes
        [{"role", "content"}, ...] straight to the endpoint. Used by the AI Chat
        scratchpad; the turn engine uses generate()."""
        if not self.model:
            raise ProviderError("No model configured — set one in Settings.")
        if self.api_mode == "completion":
            url = f"{self.base_url}/completions"
            body = {
                "model": self.model,
                "prompt": flatten_messages(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
        else:
            url = f"{self.base_url}/chat/completions"
            body = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
        self._apply_reasoning_budget(body)
        self._apply_provider_routing(body)
        async for event in self._stream(url, body):
            yield event

    async def _stream(self, url: str, body: dict) -> AsyncIterator[tuple[str, str]]:
        """Shared SSE plumbing for generate()/chat(): POST a streaming request
        and yield ("text" | "reasoning", chunk) pairs, logging the exchange."""
        # SSRF guard (hosted mode): a user-supplied endpoint_url must not point
        # at an internal/metadata address. No-op for local installs.
        reason = netguard.endpoint_block_reason(url)
        if reason:
            raise ProviderError(f"This endpoint can't be used — {reason}.")
        log = debuglog.start_entry(url, self.model, body)
        received: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                async with client.stream("POST", url, json=body, headers=self._headers()) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread()).decode(errors="replace")[:500]
                        raise ProviderError(self._friendly_http_error(resp.status_code, detail))
                    # Some servers ignore stream=true and return one plain JSON
                    # body; buffer non-SSE lines so we can fall back to it.
                    saw_sse = False
                    raw_lines: list[str] = []
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            if not saw_sse:
                                raw_lines.append(line)
                            continue
                        saw_sse = True
                        data = line[5:].strip()
                        if data == "[DONE]":
                            debuglog.finish_entry(
                                log, response="".join(received), usage=self.last_usage
                            )
                            return
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            continue
                        self._record_usage(payload)
                        reasoning = self._extract_reasoning(payload)
                        if reasoning:
                            yield "reasoning", reasoning
                        chunk = self._extract_chunk(payload)
                        if chunk:
                            received.append(chunk)
                            yield "text", chunk
                    if not saw_sse:
                        body_text = "\n".join(raw_lines).strip()
                        try:
                            payload = json.loads(body_text)
                        except ValueError:
                            raise ProviderError(
                                "AI endpoint returned neither an SSE stream nor JSON: "
                                + body_text[:200]
                            )
                        self._record_usage(payload)
                        reasoning = self._extract_reasoning(payload)
                        if reasoning:
                            yield "reasoning", reasoning
                        chunk = self._extract_chunk(payload)
                        if chunk:
                            received.append(chunk)
                            yield "text", chunk
                        if not received:
                            raise ProviderError(
                                "AI endpoint returned a response with no text: "
                                + body_text[:200]
                            )
            debuglog.finish_entry(log, response="".join(received), usage=self.last_usage)
        except httpx.ConnectError as exc:
            error = f"Could not connect to {self.base_url} — is the AI server running?"
            debuglog.finish_entry(log, response="".join(received), error=error)
            raise ProviderError(error) from exc
        except httpx.TimeoutException as exc:
            debuglog.finish_entry(log, response="".join(received), error="Timed out")
            raise ProviderError("The AI endpoint timed out.") from exc
        except httpx.HTTPError as exc:
            debuglog.finish_entry(log, response="".join(received), error=str(exc))
            raise ProviderError(f"Request to AI endpoint failed: {exc}") from exc
        except (ProviderError, GeneratorExit, BaseException) as exc:
            status = "cancelled" if isinstance(exc, GeneratorExit) else str(exc)
            debuglog.finish_entry(log, response="".join(received), error=status)
            raise

    async def complete(
        self, system: str, user: str, *, temperature: float = 0.3, max_tokens: int = 400
    ) -> str:
        """Single non-streaming completion for background calls (summarization).
        Unlike generate(), no story-continuation framing is added."""
        if not self.model:
            raise ProviderError("No model configured — set one in Settings.")
        if self.api_mode == "completion":
            url = f"{self.base_url}/completions"
            body = {
                "model": self.model,
                "prompt": f"{system}\n\n{user}",
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
        else:
            url = f"{self.base_url}/chat/completions"
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
        self._apply_reasoning_budget(body)
        self._apply_provider_routing(body)

        log = debuglog.start_entry(url, self.model, body)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                resp = await client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            debuglog.finish_entry(log, error=str(exc))
            raise ProviderError(f"Request to AI endpoint failed: {exc}") from exc
        if resp.status_code != 200:
            error = self._friendly_http_error(resp.status_code, resp.text[:500])
            debuglog.finish_entry(log, error=error)
            raise ProviderError(error)
        try:
            payload = resp.json()
        except ValueError as exc:
            debuglog.finish_entry(log, error="Invalid JSON response")
            raise ProviderError("AI endpoint returned invalid JSON.") from exc
        self._record_usage(payload)
        text = self._extract_chunk(payload)
        debuglog.finish_entry(log, response=text, usage=self.last_usage)
        return text.strip()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """POST /v1/embeddings; self.model is the embedding model here."""
        if not self.model:
            raise ProviderError("No embedding model configured — set one in Settings.")
        url = f"{self.base_url}/embeddings"
        body = {"model": self.model, "input": texts}
        log = debuglog.start_entry(url, self.model, body)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
                resp = await client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            debuglog.finish_entry(log, error=str(exc))
            raise ProviderError(f"Embedding request failed: {exc}") from exc
        if resp.status_code != 200:
            error = self._friendly_http_error(resp.status_code, resp.text[:500])
            debuglog.finish_entry(log, error=error)
            raise ProviderError(error)
        try:
            data = resp.json().get("data", [])
            vectors = [item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0))]
        except (ValueError, KeyError, TypeError) as exc:
            debuglog.finish_entry(log, error="Malformed embeddings response")
            raise ProviderError("AI endpoint returned malformed embeddings.") from exc
        if len(vectors) != len(texts):
            debuglog.finish_entry(log, error="Embedding count mismatch")
            raise ProviderError("AI endpoint returned the wrong number of embeddings.")
        debuglog.finish_entry(log, response=f"{len(vectors)} vectors × {len(vectors[0]) if vectors else 0} dims")
        return vectors

    def _friendly_http_error(self, status: int, detail: str) -> str:
        if status == 401:
            return "Authentication failed — check your API key in Settings."
        if status == 404:
            return (
                f"Endpoint or model not found (HTTP 404). Check the endpoint URL and that "
                f"model '{self.model}' exists. {detail}"
            )
        if status == 429:
            # OpenRouter's shared free tier has a per-day cap; distinguish it
            # from a short-term burst limit so the message is actionable.
            if "free-models-per-day" in detail:
                return (
                    "The free demo has hit its daily request limit (resets at "
                    "00:00 UTC). Please try again later."
                )
            return "The AI is getting too many requests right now — wait a moment and try again."
        return f"AI endpoint returned HTTP {status}: {detail}"
