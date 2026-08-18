"""Runs an adventure's enabled scripts through a turn's hook points, applying
state and story-card mutations back to the database after each hook."""

from sqlalchemy.orm import Session

from .. import models
from ..context import history as context_history
from .engine import run_hook

MAX_STORY_CARDS = 5000  # AI Dungeon's per-adventure sanity cap


class ScriptPipeline:
    def __init__(self, adventure: models.Adventure, db: Session):
        self.adventure = adventure
        self.db = db
        self.logs: list[str] = []
        self.errors: list[str] = []

    @property
    def message(self) -> str | None:
        state = self.adventure.script_state
        msg = state.get("message") if isinstance(state, dict) else None
        return msg if isinstance(msg, str) and msg.strip() else None

    def _history(self) -> list[dict]:
        # The path, not `adventure.actions` — that collection is every branch's
        # actions, and this is the documented history API a user script reads.
        # Handing a script the siblings of the turn it is running on would be
        # the same bug as building a prompt from them, only user-visible.
        #
        # `story_actions` also drops blank-text rows, which `adventure.actions`
        # kept, so this array is shorter than it used to be for an adventure
        # that has any — and `info.actionCount` counts the same way. That is
        # deliberate: a row with no text is this app's bookkeeping, it has no
        # counterpart in the AI Dungeon history a ported script was written
        # against, and the prompt has never included one. A script keyed on
        # "every N actions" will land on different turns than it did before
        # phase 14; there is no reading of this that is compatible with both.
        return [
            {"text": a.text, "rawText": a.text, "type": a.type}
            for a in context_history.story_actions(self.adventure)
        ]

    def _cards(self) -> list[dict]:
        return [
            {"id": c.id, "keys": c.keys, "entry": c.entry, "type": c.type}
            for c in self.adventure.story_cards
        ]

    def _info(self) -> dict:
        return {
            "actionCount": context_history.count(self.adventure),
            "characterNames": [],
            "memoryLength": len(self.adventure.memory),
            "maxChars": 0,
        }

    def _apply_cards(self, returned: list) -> None:
        existing = {c.id: c for c in self.adventure.story_cards}
        seen_ids = set()
        added = 0
        for item in returned:
            if not isinstance(item, dict):
                continue
            card_id = item.get("id")
            keys = str(item.get("keys") or "")
            entry = str(item.get("entry") or "")
            card_type = str(item.get("type") or "")
            if card_id in existing:
                seen_ids.add(card_id)
                card = existing[card_id]
                card.keys, card.entry, card.type = keys, entry, card_type
            elif len(existing) + added < MAX_STORY_CARDS:
                self.db.add(
                    models.StoryCard(
                        adventure_id=self.adventure.id,
                        keys=keys, entry=entry, type=card_type,
                    )
                )
                added += 1
        for card_id, card in existing.items():
            if card_id not in seen_ids:
                self.db.delete(card)

    def run(self, hook: str, text: str) -> tuple[str, bool]:
        """Chain `hook` across all enabled scripts. Returns (text, stop)."""
        state = self.adventure.script_state if isinstance(self.adventure.script_state, dict) else {}
        for script in self.adventure.scripts:
            hook_js = getattr(script, f"{hook}_js")
            if not script.enabled or not hook_js.strip():
                continue
            result = run_hook(
                script.library_js, hook_js, text, state,
                self._history(), self._cards(), self._info(),
            )
            if result.error:
                self.errors.append(f"{script.name} ({hook}): {result.error}")
                continue  # a broken script never breaks the turn
            self.logs.extend(f"[{script.name}/{hook}] {line}" for line in result.logs)
            self._apply_cards(result.story_cards)
            state = result.state
            self.adventure.script_state = state
            self.db.commit()
            text = result.text
            if result.stop:
                return text, True
        return text, False

    def report(self) -> dict:
        return {"logs": self.logs, "errors": self.errors, "message": self.message}
