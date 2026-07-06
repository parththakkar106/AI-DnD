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
    @abstractmethod
    def generate(
        self,
        parts: PromptParts,
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ("text" | "reasoning", chunk) pairs. Raises ProviderError on failure."""
