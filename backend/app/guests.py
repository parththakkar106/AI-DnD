"""The one place a guest account is created.

Every visitor used to get a `users` row the moment they loaded the page, because
`GET /api/auth/me` created one for anybody who arrived without a cookie. That
made arriving indistinguishable from playing. A crawler walking the API, a
preview fetcher, a browser opening two tabs at once — each left behind an
account and a copy of the starter adventure that nobody would ever open, and the
retention sweep was what eventually cleaned up after them.

So the account is written down later. `GET /api/auth/me` now hands a new browser
a signed visitor cookie, which names them and stores nothing, and the row is
created here the first time they do something an account is needed for: starting
an adventure, saving a setting, registering. Reading never reaches this module.

Two properties this has to hold, both of which the unique index on
`users.visitor_key` is what actually enforces:

* One visitor gets at most one account, however many of their requests arrive at
  once. The lookup below catches the ordinary case and the index catches the
  race, which is the same failure this whole change exists to remove.
* An account remembers which visitor it was written down for, so the anonymous
  visit counters can carry one person's handle across the moment they got one.
"""

import logging

from fastapi import Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import accesslog, auth, limits, models, starter

logger = logging.getLogger(__name__)


def find(db: Session, visitor: auth.Visitor) -> models.User | None:
    """Returns the account already written down for this visitor, if any."""
    return (
        db.query(models.User)
        .filter(models.User.visitor_key == visitor.id)
        .first()
    )


def adopt(
    db: Session,
    visitor: auth.Visitor,
    request: Request,
    response: Response,
) -> models.User:
    """Writes a visitor down as a guest, and points their cookie at the account.

    The caller is `auth.get_current_user`, which means this runs inside whatever
    request first needed an account. That request then proceeds as the new guest
    and the response carries their session cookie, so the visitor never sees the
    upgrade — they see the thing they asked for.

    Raises 429 through the rate limiter when one address is doing this too fast.
    Each call is a row and a copied adventure, which is exactly what a limiter is
    for, and it sits here rather than on `/auth/me` because this is now the only
    call that costs anything.
    """
    existing = find(db, visitor)
    if existing is not None:
        # A second request from the same browser, arriving while the first was
        # still in flight or after it finished. Either way they already have an
        # account, and this is the whole point of keying it on the visitor.
        return existing

    limits.rate_limit("guest", request)
    user = models.User(is_guest=True, visitor_key=visitor.id)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two of this visitor's requests raced past the lookup above and both
        # tried to insert. The unique index let one win. Ours lost, so roll back
        # and use the winner's account.
        db.rollback()
        winner = find(db, visitor)
        if winner is None:  # pragma: no cover - the index failed on something else
            raise
        return winner

    # The guest is committed first, so a failure while copying the starter
    # adventure still leaves them with an account.
    starter.give(db, user)
    db.commit()
    auth.set_session_cookie(response, user.id)
    # The log already holds this browser's arrival under their visitor name.
    # This is the row that connects the two, and it is the reason the visitor
    # name is written into it rather than only the new account's.
    accesslog.record(
        db,
        accesslog.ADOPTED,
        request,
        user=user,
        who=f"{accesslog.describe(user)} (was {visitor.label})",
    )
    return user
