"""Gives every new guest a short adventure that is already played.

A guest arriving on an empty account has nothing to look at, and the demo turns
are limited, so learning what the app does used to cost one of them. This copies
a small pre-played adventure into the new account instead. It opens on a story
with real turns in it, and the turn summaries show what the world-state engine
records: the changes it applied, and the changes it refused.

The file in `starter_data/` is an ordinary export bundle, produced by
`GET /api/adventures/{id}/export` and trimmed to its first two exchanges. To
replace it, play a new adventure, export it, and overwrite the file. Nothing
else here knows what the story contains.

The copy is the guest's own from the first moment: they can edit it, branch it,
delete it, or export it, and nothing links it back to the file. A returning
guest is not given a second one, because this runs only where a guest row is
created.
"""

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from . import bundle, models

logger = logging.getLogger(__name__)

STARTER_FILE = Path(__file__).resolve().parent / "starter_data" / "pokemon-league.json"


def _load() -> dict | None:
    """Returns the starter bundle, or None when the file is missing or invalid.

    The file ships with the server, so a failure here is a packaging mistake
    rather than bad input. It is still tolerated: a guest with no starter
    adventure can play, and a guest who cannot be created cannot.
    """
    try:
        return json.loads(STARTER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Starter adventure is missing or unreadable; skipping it.")
        return None


def give(db: Session, user: models.User) -> models.Adventure | None:
    """Copies the starter adventure into `user`, and returns it.

    The caller commits. This writes rows and does not commit them, so a guest
    and their first adventure land in one transaction: an account is never left
    half-populated.

    Failures are logged and swallowed. The starter is a convenience, and losing
    it must not cost the visitor their session.
    """
    payload = _load()
    if payload is None:
        return None
    try:
        # A savepoint, so a failure halfway through writing the tree discards
        # only the starter's rows. Without it the caller's next commit would
        # flush whatever part of the adventure the session still held.
        with db.begin_nested():
            story = bundle.plan(payload, bundle.check_format(payload))
            return bundle.materialize(db, payload, story, user.id)
    except Exception:
        logger.exception("Could not give user %s the starter adventure.", user.id)
        return None
