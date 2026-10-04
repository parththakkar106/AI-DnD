"""Stand-ins for the parts of the app a test must not really call, and the
helpers that many test modules share.

Import these rather than writing another copy. Nine test modules each carried
their own `ScriptedProvider`, and the copies had drifted into four different
feature sets, so a test that needed to raise a provider error had to be written
in one of the files whose copy supported it.

The turn helpers take a `client` whose fixture set `client.adv_id`.
"""


class ScriptedProvider:
    """Streams canned replies in place of `OpenAICompatibleProvider`.

    Set `replies` to the texts the model returns, one per call. The last entry
    repeats once the list runs out, so a test that plays more turns than it
    scripted still gets text. To drive the provider-error path, put an
    `Exception` in the list. It is raised rather than streamed.

    State lives on the class, not on the instance, because the turn engine
    constructs the provider itself and a test never sees the object. The autouse
    `reset_scripted_provider` fixture in `conftest.py` clears it between tests.

    `prompts` records every assembled `(system, story)` pair, which is what a
    test asserts on to check what the model was shown.
    """

    last_usage = None
    replies: list = []
    calls = 0
    prompts: list = []

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts, *, temperature, max_tokens):
        index = min(ScriptedProvider.calls, len(ScriptedProvider.replies) - 1)
        ScriptedProvider.calls += 1
        ScriptedProvider.prompts.append((parts.system, parts.story))
        reply = ScriptedProvider.replies[index]
        if isinstance(reply, Exception):
            raise reply
        yield ("text", reply)


def stand_on(adv_id: int, action_id: int) -> None:
    """Moves the story onto the attempt `action_id` without playing a turn.

    A turn played with `after_id` makes this move first. Tests call it directly
    to check the story and the state between the move and the next turn.
    """
    from app import models
    from app.database import SessionLocal
    from app.routers.adventures import nodes

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, adv_id)
        nodes.stand_on(db, adventure, db.get(models.Action, action_id))
        adventure.updated_at = models.utcnow()
        db.commit()
    finally:
        db.close()


def take_id(client, action_id: int, index: int) -> int:
    """Returns the id of attempt `index` of the turn that `action_id` belongs to."""
    r = client.get(f"/api/adventures/{client.adv_id}/actions/{action_id}/variants")
    assert r.status_code == 200, r.text
    return r.json()[index]["id"]


def play_turn(client, text="look around", type="do", after_id=None):
    """Plays one turn and returns the response. `after_id` names the take to
    play below, and omitting it plays at the tip."""
    body = {"type": type, "text": text}
    if after_id is not None:
        body["after_id"] = after_id
    r = client.post(f"/api/adventures/{client.adv_id}/actions", json=body)
    assert r.status_code == 200, r.text
    return r


def retry_turn(client):
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text
    return r


def story_texts(client) -> list[str]:
    """Returns the text of every action in the story window, oldest first."""
    return [a["text"] for a in client.get(f"/api/adventures/{client.adv_id}").json()["actions"]]


def list_branches(client) -> list[dict]:
    r = client.get(f"/api/adventures/{client.adv_id}/branches")
    assert r.status_code == 200, r.text
    return r.json()


def saved_state(adv_id: int) -> tuple:
    """Returns the adventure's stored `(script_state, world_state)`."""
    from app import models
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return adv.script_state, adv.world_state
    finally:
        db.close()
