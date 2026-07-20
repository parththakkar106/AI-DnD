import json
from typing import AsyncIterator

import httpx

from .. import debuglog
from .base import PromptParts, Provider, ProviderError

# Framing appended after the story text in chat mode, so chat-tuned models keep
# continuing prose instead of replying conversationally.
CHAT_CONTINUE_HINT = "\n\n[Continue the story directly. Output only story text.]"


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
        # reject unknown fields).
        self.reasoning_max_tokens = reasoning_max_tokens

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _apply_reasoning_budget(self, body: dict) -> None:
        """Give reasoning models their own thinking budget (OpenRouter-style),
        raising max_tokens so the actual output keeps its full budget."""
        if self.reasoning_max_tokens > 0 and self.api_mode == "chat":
            body["reasoning"] = {"max_tokens": self.reasoning_max_tokens}
            body["max_tokens"] += self.reasoning_max_tokens

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
                            debuglog.finish_entry(log, response="".join(received))
                            return
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            continue
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
            debuglog.finish_entry(log, response="".join(received))
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
            text = self._extract_chunk(resp.json())
        except ValueError as exc:
            debuglog.finish_entry(log, error="Invalid JSON response")
            raise ProviderError("AI endpoint returned invalid JSON.") from exc
        debuglog.finish_entry(log, response=text)
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
