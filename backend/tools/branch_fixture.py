"""Build a bootable adventure that has actually gone two ways (Phase 14, SP7).

`tools.stress_session --keep` answers the scrolling question and nothing else.
Its world state and script state are both empty, so it cannot show whether a
branch switch restores them. This script builds the small counterpart: a stat
schema, a script that counts gold, two attempts at one turn that do visibly
different damage, a fork, and a hand-written memory on each branch.

**The two branches are deliberately the same length.** Both paths are five
actions, so `actions.length` is identical either side of a switch. That is what
makes this fixture worth keeping: every panel that refreshed on the length of
the story looked correct until something asked it to tell two equal-length
branches apart, and then four of them went on showing the branch just left.

    cd backend
    .venv/Scripts/python.exe -m tools.branch_fixture /tmp/branches.db
    AIDND_DB_PATH=/tmp/branches.db .venv/Scripts/python.exe \\
        -m uvicorn app.main:app --port 8010

Then open http://127.0.0.1:8010/. The SPA is served out of `frontend/dist`, so
run `npm run build` first if that directory is stale. Expect hp 60 on "The hard
way down" and hp 95 on the fork, and expect both to change as soon as you
switch.

No LLM is called: the provider is scripted and its replies carry their own
fenced `state` blocks.
"""
import os
import sys

# Run from `backend/`; Python puts this script's own directory on the path, not
# the working one.
sys.path.insert(0, os.getcwd())

OUT = sys.argv[1] if len(sys.argv) > 1 else "branchfixture.db"
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

SCHEMA = {
    "player": {
        "hp": {"min": 0, "max": 100, "initial": 100},
        "mana": {"min": 0, "max": 50, "initial": 50, "cooldown": 2},
    }
}

GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


class ScriptedProvider:
    replies: list = []
    # The turn engine records the cost of the call, so a stand-in provider has
    # to carry this attribute even when it never calls anything.
    last_usage = None
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts, *, temperature, max_tokens):
        i = min(ScriptedProvider.calls, len(ScriptedProvider.replies) - 1)
        ScriptedProvider.calls += 1
        yield ("text", ScriptedProvider.replies[i])


Base.metadata.create_all(bind=engine)
db = SessionLocal()
# Local mode looks for the row with email IS NULL and is_guest false.
user = models.User(is_guest=False, email=None)
db.add(user)
db.flush()
db.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
scenario = models.Scenario(user_id=user.id, title="Thornwick", stat_schema=SCHEMA)
db.add(scenario)
db.flush()
adv = models.Adventure(
    user_id=user.id, title="The Hollow Beneath Thornwick", scenario_id=scenario.id,
    script_state={}, world_state={"player": {"hp": 100, "mana": 50}},
    memory_bank_enabled=True,
)
db.add(adv)
db.flush()
db.add(models.Action(
    adventure_id=adv.id, type="start",
    text="The cellar door has been shut since your grandmother died. "
         "Tonight the lantern is lit and the key is in your hand."))
db.add(models.AdventureScript(
    adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT))
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


def play(text):
    r = client.post(f"{base}/actions", json={"type": "do", "text": text})
    assert r.status_code == 200, r.text


# Turn one is a scratch, retried into a beating. The story continues from the
# beating, so the scratch is the attempt left behind. The two differ by 35 hit
# points, which is the number a switch has to restore.
ScriptedProvider.replies = [
    "You ease the door open and a nail catches your wrist.\n"
    "```state\n{\"player.hp\": -5}\n```",
    "The door slams back and takes you off your feet, down four steps onto "
    "stone.\n```state\n{\"player.hp\": -40}\n```",
    "The cellar is colder than the night outside, and it smells faintly sweet.",
    "Something moves along the far wall, keeping the dark between you.",
]
play("light the lantern and open the cellar door")
r = client.post(f"{base}/retry")
assert r.status_code == 200, r.text
play("go down, one hand on the wall")

db = SessionLocal()
discarded = [
    a.id for a in db.query(models.Action)
    .filter(models.Action.adventure_id == adv_id, models.Action.type == "ai")
    .order_by(models.Action.id) if not a.live
][0]
db.close()

client.post(f"{base}/memories", json={
    "text": "Fell down the cellar stairs; badly hurt, moving slowly."})

r = client.post(f"{base}/actions/{discarded}/fork")
assert r.status_code == 200, r.text
play("keep the lantern high and look for the far wall")
client.post(f"{base}/memories", json={
    "text": "Only a scratched wrist; still quick on your feet."})

# Leave the reader on the original line, so the difference is one click away.
root = client.get(f"{base}/branches").json()[0]["id"]
client.patch(f"{base}/branches/{root}", json={"name": "The hard way down"})
client.post(f"{base}/branches/{root}/switch")

with engine.begin() as conn:
    conn.execute(text(f"PRAGMA user_version = {LATEST_VERSION}"))

for b in client.get(f"{base}/branches").json():
    print(f"  branch {b['id']}: name={b['name']!r} fork_depth={b['fork_depth']} "
          f"own={b['own_actions']} head={b['is_head']}")
state = client.get(f"{base}/world-state").json()["state"]
print(f"  world state on head: {state.get('player')}")
print(f"  script state on head: {client.get(f'{base}/script-state').json()['state']}")
print(f"  memories: {len(client.get(f'{base}/memories').json())}")
print(f"fixture written: {OUT}")
