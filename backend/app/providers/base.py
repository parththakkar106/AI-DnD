from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class PromptParts:
    """Assembled context, provider-agnostic. Providers map this to their wire format."""

    system: str  # narrator prompt + AI instructions + memory
    story: str  # the story text so far (already token-budgeted)


class ProviderError(Exception):
    """User-presentable provider failure (connection refused, bad key, model not found…)."""


class Provider(ABC):
    # The endpoint's own token accounting for the most recent call, when it
    # reported any. It notably carries `prompt_tokens_details.cached_tokens`,
    # which is the only direct measure of whether the prompt prefix is being
    # cached. A caller reads it after the call it made, and one provider is built
    # per request, so nothing races.
    last_usage: dict | None = None

    @abstractmethod
    def generate(
        self,
        parts: PromptParts,
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ("text" | "reasoning", chunk) pairs. Raises ProviderError on failure."""
