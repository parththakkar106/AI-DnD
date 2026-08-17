"""Drive a production-sized adventure through the real routes and report what
each one costs in database bytes.

    cd backend
    .venv/Scripts/python.exe -m tools.stress_session
    .venv/Scripts/python.exe -m tools.stress_session --actions 200 --memories 100
    .venv/Scripts/python.exe -m tools.stress_session --no-embeddings

**The memory bank is ON by default, and that is the point.** The round-two
stress harness ran without an embedding model configured, and embedding
providers are BYOK-only by construction, so `retrieve_memories` returned early
every time — the whole exercise measured the turn loop with its heaviest read
switched off, and reported 23 MB for a playthrough that actually costs an order
of magnitude more. `--no-embeddings` reproduces that blindness deliberately, to
show the gap; it is never the default and it prints a warning.

Everything here is synthetic. The fixture is generated to production *shape* —
1536-dimension embeddings, ~74 KB context snapshots, retry variants — and no
real adventure, user or backup is ever read.

Only the network is faked: the LLM and the embedding endpoint. Routing,
sessions, the ORM, the scripting engine and the context builder are the real
ones, because the bugs this exists to catch live in exactly the layer a mock
would replace.

It runs on a throwaway SQLite file by default. What is being measured is which
columns of which rows a code path asks for, and that is decided by the ORM,
identically on both dialects. The dialects disagree on how a value is encoded
on the wire — JSON especially — so treat the absolute figures as
production-shaped rather than production-exact, and compare before against
after.

To measure the encodings SQLite cannot reach — bytea for the packed vectors,
and json columns psycopg parses before the meter sees them — set
AIDND_STRESS_DATABASE_URL to a **throwaway** Postgres database:

    AIDND_STRESS_DATABASE_URL=postgresql://…/stress_scratch \
        .venv/Scripts/python.exe -m tools.stress_session

The harness writes, so it refuses any target whose database name does not say
'stress' or 'scratch'. Never point it at a database holding real users.

Calibration. The fixture is sized from production, re-measured 2026-08-17
against the live Neon database (aggregates only — counts and octet_length
sums, never row contents):

    per action, text      886 B      -> --narration-bytes 1700, alternating
                                        with a one-line player input
    longest adventure     607 actions -> --actions 600
    context_snapshot      232 KB/row  -> --snapshot-bytes 232000
    memory bank, largest  100 memories, 6,144 B a vector

The previous defaults were wrong in both directions at once and happened to
land near the right total: actions were modelled at ~2.1 KB against a real
886 B, and stories at 200 actions against a real 607. Width was flattering,
length was not, and length is what a page load pays for.

Filler text is generated word by word rather than repeated. A repeated
sentence compresses about a hundredfold and prose three- or fourfold, so the
old fixture would have made any compression measurement on context_snapshot
meaningless.
"""

import os
import sys
import tempfile

# Must precede the app import: database.py reads these at module scope.
#
# Default is a throwaway SQLite file. AIDND_STRESS_DATABASE_URL points the
# harness at a real Postgres instead, which is the only way to reach the
# encodings SQLite cannot exercise: bytea for the packed vectors, and json
# columns that psycopg parses into Python before the meter ever sees them.
#
# The name guard is not paranoia. This harness *writes* — it builds a whole
# synthetic adventure — so a URL that happened to point at the production
# database would quietly seed it with fake users and fake play. The target
# must say it is disposable.
_stress_url = os.environ.get("AIDND_STRESS_DATABASE_URL", "").strip()
if _stress_url:
    _dbname = _stress_url.rsplit("/", 1)[-1].split("?")[0]
    if not any(mark in _dbname.lower() for mark in ("stress", "scratch")):
        sys.exit(
            f"refusing to run against database {_dbname!r}.\n"
            "This harness writes a synthetic adventure, so its target must be a\n"
            "throwaway database with 'stress' or 'scratch' in the name."
        )
    os.environ["AIDND_DATABASE_URL"] = _stress_url
    os.environ.pop("DATABASE_URL", None)
else:
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    os.environ["AIDND_DB_PATH"] = _tmp.name
    os.environ.pop("AIDND_DATABASE_URL", None)
    os.environ.pop("DATABASE_URL", None)

import argparse
import asyncio
import random

from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, memorybank, models, security
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import PromptParts
from app.routers import adventures

from .dbmeter import Meter, kb
from .fakeprose import prose

EMBEDDING_DIMS = 1536

# ~232 KB, measured on production's largest adventure (2026-08-17). The old
# figure here was 74 KB, taken from the comment in models.py; the real column
# averages 163 KB a row across the whole table and 232 KB on the adventure that
# matters, because the assembled prompt grows with the story behind it.
#
# Built from varied text rather than one sentence repeated. A repeated sentence
# compresses about a hundredfold and real prose three- or fourfold, so a
# fixture made of repeats would make any compression measurement meaningless —
# and shrinking this column is the open question it exists to answer.
SNAPSHOT_SYSTEM = None  # set by _build_text()
SNAPSHOT_STORY = None

PLAYER_INPUT = "> You crouch and look more closely at the grit on the floor."

# All three are bound by _build_text() from the fixture arguments.
NARRATION = None
MEMORY_TEXT = (
    "You found a bandit camp above the ford and agreed to guide Gwen through "
    "the tunnels in exchange for the iron key she took from the quartermaster."
)


def _build_text(args, rng: random.Random) -> None:
    """Size the three variable-length fixture strings from the arguments.

    Separate from build_fixture so the sizes are decided once, before anything
    is written, and so a shape's cost is a function of the flags rather than of
    how many rows happened to be generated first.
    """
    global NARRATION, SNAPSHOT_SYSTEM, SNAPSHOT_STORY
    NARRATION = prose(rng, args.narration_bytes)
    # The assembled prompt is a system block and the story so far; the split
    # is roughly one to five in production.
    SNAPSHOT_SYSTEM = prose(rng, args.snapshot_bytes // 6)
    SNAPSHOT_STORY = prose(rng, args.snapshot_bytes - args.snapshot_bytes // 6)


# --------------------------------------------------------------- fake network


class FakeProvider:
    """The LLM. Streams one fixed line; no network, no cost, no variance."""

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        yield ("text", NARRATION)

    async def complete(self, system, user, *, max_tokens=None):
        return MEMORY_TEXT


class FakeEmbeddings:
    """The embedding endpoint. Returns vectors of the real width, so what the
    turn writes back weighs what production weighs."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [self.rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIMS)]
            for _ in texts
        ]


# ------------------------------------------------------------------- fixture


def build_fixture(args, rng: random.Random) -> tuple[int, int]:
    """A user, settings and one adventure at production scale. Returns
    (adventure_id, user_id)."""
    # A SQLite run gets a brand-new temp file every time, so the fixture can
    # assume an empty database. A Postgres scratch target persists between
    # runs, and the second one would collide on the fixture user's unique
    # email — so empty it first. Only ever reached for a target whose name
    # passed the 'stress'/'scratch' guard at the top of this module.
    if _stress_url:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = models.User(is_guest=False, email="stress@example.invalid")
        db.add(user)
        db.flush()
        db.add(models.Settings(
            user_id=user.id,
            api_key=security.encrypt_secret("stress-key"),
            model="stress-model",
            endpoint_url="https://fake.invalid/v1",
            # The default this whole tool exists to stop anyone forgetting.
            embedding_model="" if args.no_embeddings else "openai/text-embedding-3-small",
            memory_bank_capacity=args.capacity,
        ))
        adventure = models.Adventure(
            user_id=user.id,
            title="Stress",
            script_state={},
            memory_bank_enabled=True,
            # Off so a turn measures the turn. The post-turn pass is its own
            # shape below; letting it fire mid-measurement would mix the two.
            auto_summarize=False,
        )
        db.add(adventure)
        db.flush()

        for i in range(args.actions):
            is_ai = bool(i % 2)
            db.add(models.Action(
                adventure_id=adventure.id,
                index=i,
                type="ai" if is_ai else "do",
                text=NARRATION if is_ai else PLAYER_INPUT,
                context_snapshot={"system": SNAPSHOT_SYSTEM, "story": SNAPSHOT_STORY},
                world_delta={"delta": {"player.hp": -3},
                             "applied": [{"path": "player.hp", "old": 88, "new": 85}]},
                # Every third AI turn was retried once, so the retry history is
                # carrying weight a list response must not pay for.
                variants=(
                    [{"text": NARRATION, "reasoning": None, "script_state": {},
                      "created_at": "2026-01-01T00:00:00"} for _ in range(2)]
                    if is_ai and i % 6 == 1 else None
                ),
                variant_count=2 if is_ai and i % 6 == 1 else 0,
            ))

        for i in range(args.memories):
            memory = models.Memory(
                adventure_id=adventure.id,
                text=f"{MEMORY_TEXT} ({i})",
                source_start=i * memorybank.MEMORY_INTERVAL,
                source_end=i * memorybank.MEMORY_INTERVAL + memorybank.MEMORY_INTERVAL - 1,
            )
            # Through the same door the app uses, so the fixture cannot end up
            # storing vectors in a shape production never produces.
            memorybank.set_vector(
                memory, [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIMS)]
            )
            db.add(memory)

        db.commit()
        return adventure.id, user.id
    finally:
        db.close()


def install_fakes(user_id: int, rng: random.Random) -> None:
    embeddings = FakeEmbeddings(rng)
    adventures.OpenAICompatibleProvider = FakeProvider
    memorybank.embedding_provider = lambda settings: embeddings
    memorybank.summary_provider = lambda settings: FakeProvider()
    auth.resolve_provider_config = lambda s, **k: auth.ProviderConfig(
        "https://fake.invalid/v1", "stress-key", "stress-model", False
    )
    limits.rate_limit = lambda *a, **k: None
    limits.check_row_cap = lambda *a, **k: None
    # Fire-and-forget post-turn work would land inside whichever scope happened
    # to be open. It is measured on purpose, as its own shape.
    memorybank.schedule_post_turn = lambda adventure: None

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user


# --------------------------------------------------------------------- shapes


def shape_list(client, meter, adv_id):
    """The adventures index — every adventure's latest narration."""
    with meter.scope("GET /adventures  (index)"):
        r = client.get("/api/adventures")
    _check(r)


def shape_load(client, meter, adv_id):
    """Opening a finished adventure: the whole story, in one response."""
    with meter.scope(f"GET /adventures/{{id}}  (page load)"):
        r = client.get(f"/api/adventures/{adv_id}")
    _check(r)


def shape_turn(client, meter, adv_id):
    """One played turn, memory retrieval included."""
    with meter.scope("POST /adventures/{id}/actions  (one turn)"):
        r = client.post(
            f"/api/adventures/{adv_id}/actions",
            json={"type": "do", "text": "look more closely at the grit"},
        )
    _check(r)


def shape_insights(client, meter, adv_id):
    """The Insights dry run — assembles a context without spending a turn."""
    with meter.scope("GET /adventures/{id}/context  (insights)"):
        r = client.get(f"/api/adventures/{adv_id}/context")
    _check(r)


def shape_memories(client, meter, adv_id):
    """The Memories drawer — every memory, and none of their vectors."""
    with meter.scope("GET /adventures/{id}/memories  (drawer)"):
        r = client.get(f"/api/adventures/{adv_id}/memories")
    _check(r)


def shape_post_turn(client, meter, adv_id):
    """Summarization, embedding and eviction, after the turn is saved."""
    with meter.scope("run_post_turn  (background)"):
        asyncio.run(memorybank.run_post_turn(adv_id))


SHAPES = {
    "list": shape_list,
    "load": shape_load,
    "turn": shape_turn,
    "insights": shape_insights,
    "memories": shape_memories,
    "post_turn": shape_post_turn,
}


def _check(response) -> None:
    if response.status_code >= 400:
        sys.exit(f"shape failed: {response.status_code} {response.text[:400]}")


# ----------------------------------------------------------------------- main


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="tools.stress_session", description=__doc__.splitlines()[0]
    )
    # 607 is production's longest adventure as of 2026-08-17, and length is
    # the dimension the old default (200) got wrong: real actions are lighter
    # than this fixture used to make them, but real stories run three times
    # longer, and length is what a page load pays for.
    p.add_argument("--actions", type=int, default=600,
                   help="story actions in the fixture (default: 600, "
                        "production's longest adventure is 607)")
    p.add_argument("--memories", type=int, default=100,
                   help="memories, all embedded (default: 100)")
    # Deliberately not the app's default (80): a measuring instrument should
    # hold the fixture at the size asked for rather than evict it mid-run.
    p.add_argument("--capacity", type=int, default=200,
                   help="Settings.memory_bank_capacity; lower it below "
                        "--memories to exercise eviction (default: 200)")
    # Production's longest adventure carries 886 B of text per action averaged
    # over both kinds. AI actions alternate with a one-line player input, so
    # the AI half has to be about twice that.
    p.add_argument("--narration-bytes", type=int, default=1700,
                   help="length of an AI action's text; alternating with a "
                        "one-line player input this averages ~890 B/action, "
                        "which is what production measures (default: 1700)")
    p.add_argument("--snapshot-bytes", type=int, default=232_000,
                   help="context_snapshot per action; 232 KB is the average "
                        "on production's longest adventure, 163 KB is the "
                        "average across the whole table (default: 232000)")
    p.add_argument("--shapes", default=",".join(SHAPES),
                   help=f"comma-separated subset of: {', '.join(SHAPES)}")
    p.add_argument("--repeat", type=int, default=1,
                   help="run each shape this many times (default: 1)")
    p.add_argument("--no-embeddings", action="store_true",
                   help="unset the embedding model — reproduces the round-two "
                        "blind spot, where the bank's cost is invisible")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--statements", type=int, default=5,
                   help="heaviest statements to print per shape (default: 5)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    chosen = [s.strip() for s in args.shapes.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in SHAPES]
    if unknown:
        sys.exit(f"unknown shape(s): {', '.join(unknown)}")

    rng = random.Random(args.seed)
    _build_text(args, random.Random(args.seed ^ 0x5F5F))
    adv_id, user_id = build_fixture(args, rng)
    install_fakes(user_id, rng)

    meter = Meter()
    # After the fixture: building it is a write path nobody plays, and its
    # bytes would drown everything the shapes report.
    meter.attach(engine)

    print(f"fixture: {args.actions} actions × {args.narration_bytes} B "
          f"(+{args.snapshot_bytes // 1024} kB snapshot, deferred) · "
          f"{args.memories} memories × {EMBEDDING_DIMS} dims · "
          f"capacity {args.capacity}")
    if args.no_embeddings:
        print("WARNING: embedding model unset — memory retrieval will return "
              "early and the bank's cost will not appear below.")
    else:
        print("memory bank: ON (embedding model configured)")

    with TestClient(app) as client:
        for _ in range(args.repeat):
            for name in chosen:
                SHAPES[name](client, meter, adv_id)

    print(meter.render(statements=args.statements))
    print()
    print(f"{'total across all shapes':<44}{kb(sum(s.total.fetched for s in meter.scopes)):>16}")

    app.dependency_overrides.clear()
    adventures._active_turns.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
