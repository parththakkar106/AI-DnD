"""A tree with a shape worth drawing, for driving the branch map by hand.

`tools.branch_fixture` builds two branches of equal length, which is the case
the panel-refresh bug needed and the smallest tree that proves a switch. A map
needs the other case: several lines, leaving at different moments, one of them
forked off a fork, so lanes have to nest and the moment axis has to mean
something. Four branches, depths 26 / 20 / 22 / 22, forks at 5, 15 and 17.

There is no frontend test runner, so this is the whole of the map's coverage:
it exists to be looked at.

    cd backend
    .venv/Scripts/python.exe -m tools.tree_fixture /tmp/tree.db
    AIDND_DB_PATH=/tmp/tree.db .venv/Scripts/python.exe \
        -m uvicorn app.main:app --port 8010

Then open http://127.0.0.1:8010/play/1, Branches → See the tree. The SPA is
served out of `frontend/dist`, so run `npm run build` first if it is stale.

No LLM is called: the provider is scripted.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

OUT = sys.argv[1] if len(sys.argv) > 1 else "treefixture.db"
if os.path.exists(OUT):
    os.remove(OUT)
os.environ["AIDND_DB_PATH"] = OUT
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)
os.environ.pop("AIDND_MULTI_USER", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import auth, limits, models  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.migrations import LATEST_VERSION  # noqa: E402
from app.routers import adventures  # noqa: E402

SCHEMA = {"player": {"hp": {"min": 0, "max": 100, "initial": 100}}}

PROSE = [
    "The stair turns and the lantern light goes with it.",
    "Water somewhere ahead, and it is not running.",
    "A door, and the frame around it is warm to the touch.",
    "Something has been eating the salt off the stones.",
    "The passage opens and the ceiling goes out of reach.",
    "A shape at the edge of the light declines to move.",
    "The floor here has been swept, recently, by something patient.",
    "You find the second lantern, still warm, and nobody holding it.",
]


class ScriptedProvider:
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts, *, temperature, max_tokens):
        i = ScriptedProvider.calls
        ScriptedProvider.calls += 1
        yield ("text", f"{PROSE[i % len(PROSE)]}\n```state\n{{\"player.hp\": -2}}\n```")


Base.metadata.create_all(bind=engine)
db = SessionLocal()
user = models.User(is_guest=False, email=None)
db.add(user)
db.flush()
db.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
scenario = models.Scenario(user_id=user.id, title="Thornwick", stat_schema=SCHEMA)
db.add(scenario)
db.flush()
adv = models.Adventure(
    user_id=user.id, title="The Hollow Beneath Thornwick", scenario_id=scenario.id,
    script_state={}, world_state={"player": {"hp": 100}}, memory_bank_enabled=True,
)
db.add(adv)
db.flush()
db.add(models.Action(
    adventure_id=adv.id, index=0, type="start",
    text="The cellar door has been shut since your grandmother died."))
db.commit()
adv_id = adv.id
db.close()

adventures.turns.OpenAICompatibleProvider = ScriptedProvider
auth.resolve_provider_config = lambda s: auth.ProviderConfig(
    "http://fake", "k", "test-model", False)
limits.rate_limit = lambda *a, **k: None
limits.check_row_cap = lambda *a, **k: None

client = TestClient(app)
base = f"/api/adventures/{adv_id}"


def play(n=1):
    for _ in range(n):
        r = client.post(f"{base}/actions", json={"type": "do", "text": "go on"})
        assert r.status_code == 200, r.text


def retry_and_get_discarded():
    """Retry the last turn and hand back the take the story left behind."""
    before = live_ai_ids()
    r = client.post(f"{base}/retry")
    assert r.status_code == 200, r.text
    db = SessionLocal()
    dead = [a.id for a in db.query(models.Action)
            .filter(models.Action.adventure_id == adv_id, models.Action.type == "ai")
            .order_by(models.Action.id) if not a.live]
    db.close()
    assert dead, "retry left no discarded take"
    # The one that was live a moment ago is the one this retry replaced.
    return [i for i in dead if i in before][-1]


def live_ai_ids():
    db = SessionLocal()
    out = [a.id for a in db.query(models.Action)
           .filter(models.Action.adventure_id == adv_id, models.Action.type == "ai")
           .order_by(models.Action.id) if a.live]
    db.close()
    return out


def fork(action_id):
    r = client.post(f"{base}/actions/{action_id}/fork")
    assert r.status_code == 200, r.text


def branches():
    return client.get(f"{base}/branches").json()


# ---- the first telling: a long line, with two turns retried along it ----
play(3)
early = retry_and_get_discarded()
play(6)
late = retry_and_get_discarded()
play(4)
root = branches()[0]["id"]
client.patch(f"{base}/branches/{root}", json={"name": "The hard way down"})

# ---- a line that left early, and a line that left it again ----
fork(early)                       # now reading the early attempt, on its own branch
play(5)
mid = retry_and_get_discarded()
play(2)
second = [b for b in branches() if b["is_head"]][0]["id"]
client.patch(f"{base}/branches/{second}", json={"name": "Down the other stair"})

fork(mid)                         # a fork off a fork
play(3)

# ---- and one more off the first telling, much later ----
client.post(f"{base}/branches/{root}/switch")
fork(late)
play(2)

# Leave the reader on the deepest line, so the map opens on a nested lane.
deep = [b for b in branches() if b["parent_branch_id"] not in (None,)
        and b["name"] is None][0]
client.post(f"{base}/branches/{deep['id']}/switch")

with engine.begin() as conn:
    conn.execute(text(f"PRAGMA user_version = {LATEST_VERSION}"))

for b in branches():
    print(f"  branch {b['id']}: name={b['name']!r} parent={b['parent_branch_id']} "
          f"fork_depth={b['fork_depth']} depth={b['depth']} own={b['own_actions']} "
          f"head={b['is_head']}")
print(f"fixture written: {OUT} (adventure {adv_id})")
