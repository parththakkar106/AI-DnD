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
    # reported any — notably `prompt_tokens_details.cached_tokens`, which is
    # the only direct read on whether the prompt prefix is being cached.
    # Callers read it after the call they made; one provider is built per
    # request, so there is nothing to race.
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
