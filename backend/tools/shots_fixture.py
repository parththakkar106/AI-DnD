"""The story the README screenshots are taken of.

`tools.tree_fixture` builds a tree with a shape worth drawing, on a scenario
with one stat, because a map only cares about the shape. A screenshot cares
about everything else: the world-state rail wants a scenario with bands, flags,
milestones and a named cast, and the story wants prose somebody would read.

So this script drives the seeded Bandit Camp demo through eight turns of written
prose and written deltas. That is the same scenario the older images were shot
on, which keeps the set consistent. It then forks three discarded attempts onto
branches of their own, one of them off a branch, so the map has to nest. It
leaves the reader on the first telling, on a turn that has a second attempt, so
one screen shows the world state, the attempt pager, and the branch rail at
once.

    cd backend
    .venv/Scripts/python.exe -m tools.shots_fixture /tmp/shots.db
    AIDND_DB_PATH=/tmp/shots.db .venv/Scripts/python.exe \
        -m uvicorn app.main:app --port 8010

Then open http://127.0.0.1:8010/play/1. The SPA is served out of
`frontend/dist`, so run `npm run build` first if it is stale.

No LLM is called: the provider is scripted, and every delta below is the one
the referee is being shown clamping.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

OUT = sys.argv[1] if len(sys.argv) > 1 else "shotsfixture.db"
if os.path.exists(OUT):
    os.remove(OUT)
os.environ["AIDND_DB_PATH"] = OUT
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)
os.environ.pop("AIDND_MULTI_USER", None)

import json  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import auth, limits, models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.migrations import LATEST_VERSION  # noqa: E402
from app.routers import adventures  # noqa: E402

SCENARIO_TITLE = "[Demo] The Bandit Camp (RPG world state)"

# (what the player typed, what the model said, what it claimed changed).
#
# The deltas are chosen so the rail has something to show at every level: a
# counter that only climbs, two flags that flip both ways, a text stat that is
# rewritten rather than added to, a milestone that sticks, and one delta the
# per-turn cap has to cut down (-60 hp against a max_delta_per_turn of 35).
TURNS = [
    (
        "Signal Gwen to circle left, and keep my eyes on the fire.",
        "Gwen goes without a sound, low along the ditch, and the fire keeps its "
        "own counsel. Two bedrolls, not three. Whoever owns the third is awake "
        "somewhere in the dark, and that is the one worth knowing about.",
        {"npc.gwen.trust": 8},
    ),
    (
        "Cut the horse line so they scatter through the camp.",
        "The rope parts under the knife and eleven hands of frightened horse go "
        "through the fire pit sideways. Somebody shouts your name for a thing "
        "you have not done yet. The camp is awake now, all of it at once.",
        {"flags.alarm_raised": True, "flags.player_hidden": False,
         "npc.bandit_leader.aggression": 20},
    ),
    (
        "Get behind the wagon before anyone finds the ditch.",
        "You make the wagon's shadow with a spear-length to spare and put your "
        "back against a wheel that has not turned in a season. Gwen's arrow "
        "answers from the treeline — once, and then not again, which is her way "
        "of saying she is fine and busy.",
        {"flags.player_hidden": True, "player.mana": -6},
    ),
    (
        "Wait for the leader to pass, then take him from behind.",
        "You wait a beat too long. He turns for a noise Gwen is making and walks "
        "onto the knife himself, and it is the least honourable thing you have "
        "ever done well. He is dead before the surprise finishes crossing his "
        "face.",
        {"npc.bandit_leader.health": -120, "npc.gwen.trust": -10,
         "milestones.camp_cleared": True},
    ),
    (
        "Fall back to Gwen and let her finish it.",
        "You give ground the way she taught you, keeping him square to the "
        "treeline, and the arrow takes him through the shoulder blade at "
        "eleven paces. He sits down in the ash of his own fire and does not "
        "get up. The camp goes quiet in pieces.",
        {"npc.bandit_leader.health": -70, "npc.gwen.trust": 15,
         "milestones.camp_cleared": True, "flags.alarm_raised": False},
    ),
    (
        "Search the wagon for the strongbox.",
        "It is under the false floor, of course it is, and it is heavier than "
        "the caravan master implied. The lock has been worked at by somebody "
        "patient and unsuccessful. You take the dead man's coat as well; yours "
        "is one long tear from armpit to hip.",
        {"milestones.strongbox_found": True,
         "player.outfit": "a bandit's tarred coat over torn leather, strongbox under one arm"},
    ),
    (
        "Bind my side before we move.",
        "Gwen does it, badly and fast, with a strip off the same coat. \"You "
        "went in alone,\" she says, in the voice she uses when she has decided "
        "not to have the argument. The bleeding stops. The rest of it does not.",
        {"player.hp": 18, "npc.gwen.trust": -5},
    ),
    (
        "Take the north road while it's still dark.",
        "You are two miles out when the sky starts telling on you, and the camp "
        "behind is only smoke by then. Gwen walks ahead where she can see the "
        "road, which means she is still angry, which means she is still here.",
        {"world.day": 1, "milestones.gwen_survives": True, "player.mana": 9},
    ),
]

# The attempts the story did not keep. Each one is retried at the turn with the
# same index below, and then forked onto a branch of its own. A discarded attempt
# is the only thing a fork can be made from.
RETAKES = {
    3: (
        "He passes close enough that you can smell the tar on his coat, and the "
        "knife goes in under the arm where the plate is not. He is not a man who "
        "goes down for it. He turns inside the blow and opens your side with the "
        "back-swing, and the two of you come apart bleeding.",
        {"player.hp": -60, "npc.bandit_leader.health": -45,
         "npc.bandit_leader.aggression": 25},
    ),
    6: (
        "You tell her you will do it yourself and she lets you, which is worse "
        "than the argument. The knot is bad. You will feel it in the morning, "
        "and she will watch you feel it and say nothing at all.",
        {"player.hp": 9, "npc.gwen.trust": -18},
    ),
}


class ScriptedProvider:
    """Serves whatever the driver loaded, so a retry can differ from the attempt
    it replaces.

    That difference is what the screenshots show.
    """

    next_reply = ("", {})

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts, *, temperature, max_tokens):
        prose, delta = ScriptedProvider.next_reply
        block = f"\n```state\n{json.dumps(delta)}\n```" if delta else ""
        yield ("text", prose + block)


adventures.turns.OpenAICompatibleProvider = ScriptedProvider
auth.resolve_provider_config = lambda s: auth.ProviderConfig(
    "http://fake", "k", "test-model", False)
limits.rate_limit = lambda *a, **k: None
limits.check_row_cap = lambda *a, **k: None

# `bootstrap()` runs at import and seeds the public demo scenarios, so the Bandit
# Camp already exists. Shooting on it matters because it is the scenario a
# visitor meets.
client = TestClient(app)

db = SessionLocal()
user = models.User(is_guest=False, email=None)
db.add(user)
db.flush()
db.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
scenario = db.query(models.Scenario).filter(
    models.Scenario.title == SCENARIO_TITLE).one()
db.commit()
scenario_id = scenario.id
db.close()

r = client.post("/api/adventures", json={
    "scenario_id": scenario_id,
    "title": "The Bandit Camp",
    "placeholders": {},
})
assert r.status_code == 201, r.text
adv_id = r.json()["id"]
base = f"/api/adventures/{adv_id}"


def play(i):
    """One turn of the written story."""
    player, prose, delta = TURNS[i]
    ScriptedProvider.next_reply = (prose, delta)
    r = client.post(f"{base}/actions", json={"type": "do", "text": player})
    assert r.status_code == 200, r.text


def retake(i):
    """Retry turn `i` with the other reply, and hand back the take it left."""
    ScriptedProvider.next_reply = RETAKES[i]
    return retry_last()


def retry_last():
    """Retries whatever is at the tip with whatever reply is loaded.

    Returns the attempt the story just left, which is the only thing a fork can
    be made from.
    """
    before = live_ai_ids()
    r = client.post(f"{base}/retry")
    assert r.status_code == 200, r.text
    dead = [a for a in all_ai() if not a.live and a.id in before]
    assert dead, "retry left no discarded take"
    return dead[-1].id


def all_ai():
    db = SessionLocal()
    out = db.query(models.Action).filter(
        models.Action.adventure_id == adv_id,
        models.Action.type == "ai").order_by(models.Action.id).all()
    db.expunge_all()
    db.close()
    return out


def live_ai_ids():
    return [a.id for a in all_ai() if a.live]


def branches():
    return client.get(f"{base}/branches").json()


def name(branch_id, label):
    r = client.patch(f"{base}/branches/{branch_id}", json={"name": label})
    assert r.status_code == 200, r.text


# ---- the first telling, with the knife-in-the-back turn retried ----
for i in range(4):
    play(i)
knife = retake(3)
for i in range(4, 7):
    play(i)
binding = retake(6)
play(7)
root = branches()[0]["id"]
name(root, "The quiet way in")

# ---- the take where he died easy, given a line of its own ----
r = client.post(f"{base}/actions/{knife}/fork")
assert r.status_code == 200, r.text
ScriptedProvider.next_reply = (
    "The camp finds him before you have finished wiping the knife, and there "
    "is nothing quiet about the next four minutes.", {"flags.alarm_raised": True,
                                                      "player.hp": -22})
r = client.post(f"{base}/actions", json={"type": "do", "text": "Take his horn and blow it."})
assert r.status_code == 200, r.text
name([b for b in branches() if b["is_head"]][0]["id"], "Loud, and early")

# ---- and a line that left that one again, so the map has to nest ----
#
# Forking the attempt at the tip only switches to it, because the attempts there
# are still leaves that nothing was built on. The story is therefore moved one
# turn past it first, and only then does the attempt it left deserve a branch.
ScriptedProvider.next_reply = (
    "Nobody comes. The horn was the wrong horn, or the camp has been empty of "
    "anyone who cares since before you got here.", {"npc.gwen.trust": -4})
horn = retry_last()
ScriptedProvider.next_reply = (
    "The tents are as empty as the sound was. Somebody left in a hurry and did "
    "not take the good rope.", {"flags.player_hidden": True})
r = client.post(f"{base}/actions", json={"type": "do", "text": "Search the tents."})
assert r.status_code == 200, r.text

r = client.post(f"{base}/actions/{horn}/fork")
assert r.status_code == 200, r.text
ScriptedProvider.next_reply = (
    "They come at the sound the way water finds a crack, and you spend the next "
    "hour learning the camp by running through it.",
    {"player.hp": -28, "npc.bandit_leader.aggression": 15})
r = client.post(f"{base}/actions", json={"type": "do", "text": "Run for the treeline."})
assert r.status_code == 200, r.text
name([b for b in branches() if b["is_head"]][0]["id"], "Through the camp")

# ---- and the argument that was never had ----
client.post(f"{base}/branches/{root}/switch")
r = client.post(f"{base}/actions/{binding}/fork")
assert r.status_code == 200, r.text
ScriptedProvider.next_reply = (
    "She lets it go, and the road out is quieter for it than either of you "
    "wanted.", {"npc.gwen.trust": -6})
r = client.post(f"{base}/actions", json={"type": "do", "text": "Say nothing and walk."})
assert r.status_code == 200, r.text
name([b for b in branches() if b["is_head"]][0]["id"], "Said nothing")

# Leave the reader on the first telling, on the turn that has two attempts. That
# is the one screen showing the rail, the pager, and the branches.
client.post(f"{base}/branches/{root}/switch")

with engine.begin() as conn:
    conn.execute(text(f"PRAGMA user_version = {LATEST_VERSION}"))

for b in branches():
    print(f"  branch {b['id']}: name={b['name']!r} parent={b['parent_branch_id']} "
          f"fork_depth={b['fork_depth']} depth={b['depth']} own={b['own_actions']} "
          f"head={b['is_head']}")
print(f"fixture written: {OUT} (adventure {adv_id})")
