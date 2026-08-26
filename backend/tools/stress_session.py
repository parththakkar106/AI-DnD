"""Drives a production-sized adventure through the real routes and reports what
each one costs in database bytes.

    cd backend
    .venv/Scripts/python.exe -m tools.stress_session
    .venv/Scripts/python.exe -m tools.stress_session --actions 200 --memories 100
    .venv/Scripts/python.exe -m tools.stress_session --no-embeddings

The memory bank is on by default, and that matters. The round-two stress
harness ran with no embedding model configured, and embedding providers are
BYOK-only by construction, so `retrieve_memories` returned early every time.
That run measured the turn loop with its heaviest read disabled and reported
23 MB for a playthrough that costs an order of magnitude more.
`--no-embeddings` reproduces that configuration on purpose, to show the gap. It
is never the default, and it prints a warning.

Everything here is synthetic. The fixture is generated to the shape of
production data, with 1536-dimension embeddings, context snapshots of about
74 kB, and retry variants. It never reads a real adventure, user, or backup.

Only the network is faked, which means the LLM and the embedding endpoint.
Routing, sessions, the ORM, the scripting engine, and the context builder are
the real ones, because the bugs this harness exists to catch are in the layer a
mock would replace.

It runs on a throwaway SQLite file by default. The measurement is which columns
of which rows a code path requests, and the ORM decides that identically on both
dialects. The dialects differ in how a value is encoded on the wire, especially
JSON, so treat the absolute figures as production-shaped rather than
production-exact, and compare one run against another.

To measure the encodings SQLite cannot reach, which are bytea for the packed
vectors and json columns that psycopg parses before the meter sees them, set
`AIDND_STRESS_DATABASE_URL` to a throwaway Postgres database:

    AIDND_STRESS_DATABASE_URL=postgresql://…/stress_scratch \
        .venv/Scripts/python.exe -m tools.stress_session

The harness writes, so it refuses any target whose database name does not
contain 'stress' or 'scratch'. Never point it at a database holding real users.

Calibration. The fixture is sized from production and was re-measured on
2026-08-17 against the live Neon database, reading aggregates only: counts and
`octet_length` sums, never row contents.

    per action, text      886 B      -> --narration-bytes 1700, alternating
                                        with a one-line player input
    longest adventure     607 actions -> --actions 600
    context_snapshot      232 KB/row  -> --snapshot-bytes 232000
    memory bank, largest  100 memories, 6,144 B a vector

The previous defaults were wrong in both directions at once and happened to
land near the right total. Actions were modeled at about 2.1 kB against a real
886 B, and stories at 200 actions against a real 607. The width was too large
and the length was too small, and the length is what a page load pays for.

Filler text is generated word by word rather than repeated. A repeated sentence
compresses about a hundredfold and prose three- or fourfold, so the old fixture
would have made any compression measurement on `context_snapshot` meaningless.
"""

import os
import sys
import tempfile
from pathlib import Path


def _early_keep(argv: list[str]) -> str:
    """Reads `--keep` before argparse exists.

    The database location has to be decided before `app.database` is imported,
    and that import is a few lines below. argparse still declares the flag, so
    `--help` documents it and a typo is still an error.
    """
    for i, arg in enumerate(argv):
        if arg == "--keep" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--keep="):
            return arg.split("=", 1)[1]
    return ""


_keep = _early_keep(sys.argv[1:])

# This has to run before the app import, because `database.py` reads these at
# module scope.
#
# The default is a throwaway SQLite file. `AIDND_STRESS_DATABASE_URL` points the
# harness at a real Postgres instead, which is the only way to reach the
# encodings SQLite cannot exercise: bytea for the packed vectors, and json
# columns that psycopg parses into Python before the meter sees them.
#
# The name guard matters. This harness writes a whole synthetic adventure, so a
# URL that pointed at the production database would seed it with fake users and
# fake play. The target has to name itself as disposable.
_stress_url = os.environ.get("AIDND_STRESS_DATABASE_URL", "").strip()
if _stress_url:
    if _keep:
        sys.exit(
            "--keep writes a SQLite file for the app to serve; it cannot be\n"
            "combined with AIDND_STRESS_DATABASE_URL."
        )
    _dbname = _stress_url.rsplit("/", 1)[-1].split("?")[0]
    if not any(mark in _dbname.lower() for mark in ("stress", "scratch")):
        sys.exit(
            f"refusing to run against database {_dbname!r}.\n"
            "This harness writes a synthetic adventure, so its target must be a\n"
            "throwaway database with 'stress' or 'scratch' in the name."
        )
    os.environ["AIDND_DATABASE_URL"] = _stress_url
    os.environ.pop("DATABASE_URL", None)
elif _keep:
    # A fixture to start the app against, rather than a temporary file the
    # report discards. Every run rebuilds it from empty, because
    # `build_fixture()` assumes an empty database on the SQLite path and a
    # second run would otherwise add a second adventure next to the first.
    _keep_path = Path(_keep).resolve()
    _keep_path.parent.mkdir(parents=True, exist_ok=True)
    _keep_path.unlink(missing_ok=True)
    os.environ["AIDND_DB_PATH"] = str(_keep_path)
    os.environ.pop("AIDND_DATABASE_URL", None)
    os.environ.pop("DATABASE_URL", None)
else:
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    os.environ["AIDND_DB_PATH"] = _tmp.name
    os.environ.pop("AIDND_DATABASE_URL", None)
    os.environ.pop("DATABASE_URL", None)

import argparse
import asyncio
import json
import random

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, limits, memorybank, models, security, seed, tree, worldstate
from app.context import cursors
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import PromptParts
from app.routers import adventures

from .dbmeter import Meter, kb
from .fakeprose import prose

EMBEDDING_DIMS = 1536

# About 232 kB, measured on the largest adventure in production on 2026-08-17.
# The old figure here was 74 kB, taken from the comment in `models.py`. The real
# column averages 163 kB per row across the whole table and 232 kB on the
# adventure that matters, because the assembled prompt grows with the story
# behind it.
#
# The text is varied rather than one sentence repeated. A repeated sentence
# compresses about a hundredfold and real prose three- or fourfold, so a fixture
# built from repeats would make any compression measurement meaningless, and
# shrinking this column is the open question the fixture exists to answer.
SNAPSHOT_SYSTEM = None  # set by _build_text()
SNAPSHOT_STORY = None

PLAYER_INPUT = "> You crouch and look more closely at the grit on the floor."

# `_build_text()` sets all three from the fixture arguments.
NARRATION = None
MEMORY_TEXT = (
    "You found a bandit camp above the ford and agreed to guide Gwen through "
    "the tunnels in exchange for the iron key she took from the quartermaster."
)


def _build_text(args, rng: random.Random) -> None:
    """Sizes the three variable-length fixture strings from the arguments.

    This is separate from `build_fixture` so that the sizes are decided once,
    before anything is written, and so that a shape's cost depends on the flags
    rather than on how many rows were generated first.
    """
    global NARRATION, SNAPSHOT_SYSTEM, SNAPSHOT_STORY
    NARRATION = prose(rng, args.narration_bytes)
    # The assembled prompt is a system block plus the story so far. The split
    # is roughly one to five in production.
    SNAPSHOT_SYSTEM = prose(rng, args.snapshot_bytes // 6)
    SNAPSHOT_STORY = prose(rng, args.snapshot_bytes - args.snapshot_bytes // 6)


# --------------------------------------------------------------- fake network


class FakeProvider:
    """Stands in for the LLM, streaming one fixed line.

    It makes no network call, costs nothing, and returns the same text every
    time.
    """

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        yield ("text", NARRATION)

    async def complete(self, system, user, *, max_tokens=None):
        return MEMORY_TEXT


class FakeEmbeddings:
    """Stands in for the embedding endpoint.

    It returns vectors of the production width, so what the turn writes back is
    the size production writes back.
    """

    def __init__(self, rng: random.Random):
        self.rng = rng

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [self.rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIMS)]
            for _ in texts
        ]


# -------------------------------------------------------------- rich fixture

# The correctness fixture, as opposed to the scale fixture.
#
# The default fixture is sized from production and exists to measure bytes, so it
# leaves every column it does not measure at its default. That makes it a poor
# test of semantics, and phase 14 replaces the storage model underneath all of
# it. On a freshly built default fixture, the columns a story tree has to migrate
# correctly hold this:
#
#   state_after, world_state_after      The same value on all 600 rows. They
#                                       were state_before, and NULL, until SP4
#                                       reversed them. A rollback over identical
#                                       snapshots tests nothing.
#   scenario_id, world_state            Absent. There is no RPG layer, so the
#                                       cooldown clock SP5 must not advance
#                                       never runs.
#   adventure_scripts                   None. A branch switch reuses the
#                                       script_state rollback.
#   memory_cursor, summary_cursor       Both 0. SP3 replaces the cursors.
#   sibling attempts                    Two attempts of byte-identical text with
#                                       the first always live, so the one
#                                       question SP4 has to answer, which
#                                       attempt is live, has no observable
#                                       answer.
#
# `--rich` fills in those columns and changes nothing else, so the measuring
# fixture's numbers stay comparable from run to run. It is a correctness fixture,
# so keep it small, such as `--rich --actions 30`. It provides variety per row
# rather than many rows.

RICH_SUMMARY = (
    "You tracked the bandits to a camp above the ford, freed Gwen from the "
    "quartermaster's cage, and struck a bargain: the iron key for safe passage "
    "through the tunnels. The alarm has not yet been raised."
)

RICH_CARDS = [
    ("character", "Gwen", "Gwen, ranger, her",
     "A loyal ranger who owes you her life and says so rarely."),
    ("location", "The Ford", "ford, river, crossing",
     "A shallow crossing overlooked by the bandit camp."),
    ("item", "Iron Key", "iron key, key",
     "Taken from the quartermaster. Opens something below the camp."),
]

# Ten gold a turn, so a state snapshot that failed to roll back shows a wrong
# total rather than no value. The retry tests use the same shape.
RICH_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


def rich_stat_schema() -> dict:
    """Returns the demo RPG schema, read from the seed data rather than invented.

    Using the real schema means the fixture exercises bands, cooldowns,
    `max_delta_per_turn`, and NPC stat blocks in their real shapes. An invented
    schema would diverge from the one it stands in for.
    """
    path = seed.SEED_DIR / "04-rpg-world-state.json"
    return json.loads(path.read_text(encoding="utf-8"))["stat_schema"]


def rich_script_state(turn: int) -> dict:
    """Returns the script state as of `turn`.

    The values increase with the turn, so any snapshot identifies the turn it was
    taken at, which is what makes an incorrect rollback visible.
    """
    return {"gold": turn * 10, "turn": turn}


def rich_world_state(schema: dict, turn: int) -> dict:
    """Returns a live world state that has been played to `turn`.

    `instantiate` returns the initial state, and a fixture whose every row holds
    that same state cannot distinguish a restored snapshot from an unrestored
    one. Here hp declines, mana declines, and a flag changes partway through.
    """
    ws = worldstate.instantiate(schema)
    ws["player"]["hp"] = max(20, 100 - turn)
    ws["player"]["mana"] = max(0, 30 - turn // 2)
    ws["world"]["day"] = 1 + turn // 20
    ws["flags"]["alarm_raised"] = turn > 12
    ws["flags"]["player_hidden"] = turn <= 12
    ws["_meta"]["last_changed"] = {"player.hp": turn}
    return ws


def rich_attempts(rng: random.Random, index: int) -> tuple[list[str], int]:
    """Returns distinguishable retry attempts, and which one the story tells.

    Every attempt in the default fixture carries the same text, with the live one
    fixed at index 0. That is the behavior SP4 has to get right and the behavior
    that fixture cannot test, so here the texts differ, the counts differ, and
    the live attempt is often not the last.
    """
    count = 3 if index % 12 == 1 else 2
    texts = [
        f"[attempt {n + 1} of {count} at turn {index}] {prose(rng, 240)}"
        for n in range(count)
    ]
    # The live attempt is not always the newest. A player who retried twice and
    # then went back to the first attempt is the case that breaks code assuming
    # the live attempt is the last one written.
    return texts, (0 if index % 18 == 1 else count - 1)


def add_rich_extras(db, args, rng: random.Random, user, adventure) -> None:
    """Adds everything `--rich` contributes apart from the actions themselves.

    The caller flushes the adventure first and writes the actions afterwards,
    because the actions need the schema to snapshot a world state from.
    """
    schema = rich_stat_schema()
    scenario = models.Scenario(
        user_id=user.id,
        title="Stress (RPG)",
        prompt="You crouch at the ford, watching the bandit camp.",
        stat_schema=schema,
    )
    db.add(scenario)
    db.flush()

    adventure.scenario_id = scenario.id
    adventure.world_state = rich_world_state(schema, args.actions)
    adventure.script_state = rich_script_state(args.actions)
    # The post-turn passes do nothing unless summarization is on and the marks
    # are past the start. They are written here as positions and translated into
    # anchors once the actions exist. See `build_fixture`. A database being
    # migrated to SP3 still holds positions, so the fixture carries both forms
    # and the two have to agree.
    adventure.auto_summarize = True
    adventure.story_summary = RICH_SUMMARY
    adventure.memory_cursor = max(0, args.actions - 8)
    adventure.summary_cursor = max(0, args.actions - 20)

    for kind, name, keys, entry in RICH_CARDS:
        db.add(models.StoryCard(
            adventure_id=adventure.id, type=kind, name=name, keys=keys, entry=entry,
        ))

    db.add(models.AdventureScript(
        adventure_id=adventure.id, position=0, enabled=True,
        name="Gold", description="Ten gold a turn.", output_js=RICH_SCRIPT,
    ))
    return schema


def add_second_adventure(db, rng: random.Random, user) -> int:
    """Adds a short second adventure, so the fixture can detect cross-adventure
    leaks.

    A tree scopes every read by branch, and a branch clause that omitted its
    adventure would still look correct on a database holding one adventure.
    """
    other = models.Adventure(
        user_id=user.id, title="Stress (second)", script_state={},
        memory_bank_enabled=True, auto_summarize=False,
    )
    db.add(other)
    db.flush()
    for i in range(6):
        action = models.Action(
            adventure_id=other.id, index=i,
            type="ai" if i % 2 else "do",
            text=f"[second adventure] turn {i}. {prose(rng, 200)}",
            state_after=rich_script_state(i),
        )
        tree.place_action(db, other, action)
        db.add(action)
    memory = models.Memory(
        adventure_id=other.id,
        text="This memory belongs to the second adventure and must never be "
             "retrieved for the first.",
        source_start=0, source_end=5,
    )
    memorybank.set_vector(memory, [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIMS)])
    tree.place_memory(db, other, memory)
    db.add(memory)
    return other.id


def _assert_live_variant_invariant(db, adventure_id: int) -> None:
    """Checks that exactly one attempt per turn is live, on every turn.

    The check runs here rather than being assumed, because a coordinate with two
    live siblings renders its turn twice and a coordinate with none omits the
    turn. Both failures produce wrong output rather than an exception, so a
    fixture that broke the rule would let incorrect code look correct.
    """
    groups: dict[tuple, list[models.Action]] = {}
    for action in (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adventure_id)
        .all()
    ):
        groups.setdefault((action.branch_id, action.depth), []).append(action)
    retried = 0
    for (branch_id, depth), rows in groups.items():
        live = [a for a in rows if a.live]
        if len(live) != 1:
            sys.exit(
                f"fixture is inconsistent: branch {branch_id} depth {depth} has "
                f"{len(live)} live attempts out of {len(rows)}."
            )
        if len(rows) > 1:
            retried += 1
    if not retried:
        sys.exit("--rich built no retried actions; raise --actions above 6.")


# ------------------------------------------------------------------- fixture


def build_fixture(args, rng: random.Random) -> tuple[int, int]:
    """Builds a user, settings, and one adventure at production scale.

    Returns `(adventure_id, user_id)`.
    """
    # A SQLite run gets a new temporary file every time, so the fixture can
    # assume an empty database. A Postgres scratch target persists between runs,
    # and a second run would collide on the fixture user's unique email, so empty
    # it first. This runs only for a target whose name passed the 'stress' or
    # 'scratch' guard at the top of this module.
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
            # The default this tool exists to keep anyone from forgetting.
            embedding_model="" if args.no_embeddings else "openai/text-embedding-3-small",
            memory_bank_capacity=args.capacity,
        ))
        adventure = models.Adventure(
            user_id=user.id,
            title="Stress",
            script_state={},
            memory_bank_enabled=True,
            # Off, so that a turn measures only the turn. The post-turn pass
            # is its own shape below, and running it during the measurement
            # would combine the two.
            auto_summarize=False,
        )
        db.add(adventure)
        db.flush()

        schema = add_rich_extras(db, args, rng, user, adventure) if args.rich else None

        for i in range(args.actions):
            is_ai = bool(i % 2)
            retried = is_ai and i % 6 == 1
            if args.rich:
                # Distinguishable attempts, with a live one that is often not
                # the last written.
                texts, live = rich_attempts(rng, i) if retried else ([], 0)
                if not retried:
                    texts = [f"[turn {i}] {NARRATION}" if is_ai
                             else f"[turn {i}] {PLAYER_INPUT}"]
            else:
                texts = [NARRATION, NARRATION] if retried else [
                    NARRATION if is_ai else PLAYER_INPUT
                ]
                live = 0
            for n, body in enumerate(texts):
                action = models.Action(
                    adventure_id=adventure.id,
                    index=i,
                    type="ai" if is_ai else "do",
                    text=body,
                    # The turn's assembled prompt is stored once, on the
                    # attempt the story tells. A superseded sibling keeps only
                    # its own slices. `app/attempts.py` maintains that
                    # invariant, and a fixture that ignored it would multiply
                    # the largest column in the database by the retry count.
                    context_snapshot=(
                        {"system": SNAPSHOT_SYSTEM, "story": SNAPSHOT_STORY}
                        if n == live else
                        {"raw_output": body}
                    ),
                    world_delta={"delta": {"player.hp": -3},
                                 "applied": [{"path": "player.hp", "old": 88, "new": 85}]},
                    # Every third AI turn was retried once, so a turn is
                    # sometimes several rows sharing one coordinate.
                    live=(n == live),
                    variant_index=n,
                    variant_count=len(texts) if len(texts) > 1 else 0,
                    # Under `--rich` the value increases with the turn, so an
                    # incorrect rollback shows a wrong number rather than no
                    # value. Otherwise `tree.stamp_outcome` writes the
                    # adventure's state, which does not change and therefore
                    # tests nothing.
                    state_after=rich_script_state(i) if args.rich else None,
                    world_state_after=(
                        rich_world_state(schema, i) if args.rich else None
                    ),
                )
                # `create_all` builds a fresh database and stamps it LATEST,
                # so no migration runs against it and the tree backfill never
                # sees it. The fixture has to stamp its own nodes, or it would
                # be the one database in the project whose actions have no
                # branch.
                tree.place_action(db, adventure, action)
                db.add(action)

        for i in range(args.memories):
            memory = models.Memory(
                adventure_id=adventure.id,
                text=f"{MEMORY_TEXT} ({i})",
                source_start=i * memorybank.MEMORY_INTERVAL,
                source_end=i * memorybank.MEMORY_INTERVAL + memorybank.MEMORY_INTERVAL - 1,
                # A bank in play is not uniformly active. Some memories are
                # pinned, which means they are always retrieved, and some are
                # evicted but kept for the UI. Both states have to survive being
                # re-attached to nodes.
                pinned=bool(args.rich and i % 9 == 0),
                forgotten=bool(args.rich and i % 11 == 5),
            )
            # Use the same call the app uses, so the fixture cannot store
            # vectors in a shape production never produces.
            memorybank.set_vector(
                memory, [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIMS)]
            )
            tree.place_memory(db, adventure, memory)
            db.add(memory)

        if args.rich:
            # Translate the memory mark and the summary mark from the
            # positions `add_rich_layers` set into nodes, now that actions exist
            # to point at. This uses the same call the v1 importer uses. A
            # fixture stamped LATEST never runs a migration, so skipping this
            # would leave the one database whose marks are only positions.
            db.flush()
            cursors.anchor_at_position(adventure, cursors.MEMORY, adventure.memory_cursor)
            cursors.anchor_at_position(adventure, cursors.SUMMARY, adventure.summary_cursor)
            add_second_adventure(db, rng, user)
            _assert_live_variant_invariant(db, adventure.id)

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
    # Post-turn work scheduled in the background would be counted inside
    # whatever scope was open. It is measured deliberately, as its own shape.
    memorybank.schedule_post_turn = lambda adventure: None

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user


# --------------------------------------------------------------------- shapes


def shape_list(client, meter, adv_id):
    """Measures the adventures index, which returns each adventure's latest
    narration."""
    with meter.scope("GET /adventures  (index)"):
        r = client.get("/api/adventures")
    _check(r)


def shape_load(client, meter, adv_id):
    """Measures opening a finished adventure, which returns the whole story in
    one response."""
    with meter.scope(f"GET /adventures/{{id}}  (page load)"):
        r = client.get(f"/api/adventures/{adv_id}")
    _check(r)


def shape_turn(client, meter, adv_id):
    """Measures one played turn, including memory retrieval."""
    with meter.scope("POST /adventures/{id}/actions  (one turn)"):
        r = client.post(
            f"/api/adventures/{adv_id}/actions",
            json={"type": "do", "text": "look more closely at the grit"},
        )
    _check(r)


def shape_insights(client, meter, adv_id):
    """Measures the Insights dry run, which assembles a context without playing
    a turn."""
    with meter.scope("GET /adventures/{id}/context  (insights)"):
        r = client.get(f"/api/adventures/{adv_id}/context")
    _check(r)


def shape_memories(client, meter, adv_id):
    """Measures the Memories drawer, which returns every memory and no
    vectors."""
    with meter.scope("GET /adventures/{id}/memories  (drawer)"):
        r = client.get(f"/api/adventures/{adv_id}/memories")
    _check(r)


def shape_post_turn(client, meter, adv_id):
    """Measures summarization, embedding, and eviction after the turn is
    saved."""
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


# ------------------------------------------------------------------- --keep


def make_bootable() -> None:
    """Applies the two edits that make a measurement fixture servable by the app.

    Both edits are needed because `build_fixture()` builds a database for the
    meter rather than for a browser.
    """
    from app.migrations import LATEST_VERSION

    with engine.begin() as conn:
        # `create_all()` builds the current schema but leaves the stamp at its
        # default, and `bootstrap()` reads an unstamped existing database as
        # very old. It would replay every migration against a schema that
        # already has every column, and fail on the first one.
        conn.execute(text(f"PRAGMA user_version = {LATEST_VERSION}"))
        # In local mode, which is when `AIDND_MULTI_USER` is unset,
        # `get_current_user()` looks for the row with `email IS NULL` and
        # `is_guest` false. The fixture's user is a registered one, so without
        # this update nothing owns the adventure and the app opens on an empty
        # library.
        conn.execute(text("UPDATE users SET email = NULL, is_guest = 0"))


def print_keep_notes(path: str, actions: int) -> None:
    port = 8010
    print()
    print(f"fixture kept: {path}")
    print(f"  {actions} actions, bootable in local mode. To scroll it:")
    print()
    print(f"    cd backend && AIDND_DB_PATH={path} \\")
    print(f"        .venv/Scripts/python.exe -m uvicorn app.main:app --port {port}")
    print(f"    cd frontend && AIDND_API_PORT={port} npm run dev")
    print()
    # 8000 is the vite proxy's default, and another local app already listens
    # there. That app shadows this API with its own SPA catch-all route, which
    # looks like an empty database rather than a proxy problem.
    print(f"  Port {port} rather than 8000 on purpose; AIDND_API_PORT points vite at it.")


# ----------------------------------------------------------------------- main


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="tools.stress_session", description=__doc__.splitlines()[0]
    )
    # 607 is the longest adventure in production as of 2026-08-17. Length is
    # the dimension the old default of 200 got wrong. Real actions are smaller
    # than this fixture used to make them, but real stories run three times
    # longer, and length is what a page load pays for.
    p.add_argument("--actions", type=int, default=600,
                   help="story actions in the fixture (default: 600, "
                        "production's longest adventure is 607)")
    p.add_argument("--memories", type=int, default=100,
                   help="memories, all embedded (default: 100)")
    # This is not the app's default of 80. A measuring instrument holds the
    # fixture at the requested size rather than evict from it during a run.
    p.add_argument("--capacity", type=int, default=200,
                   help="Settings.memory_bank_capacity; lower it below "
                        "--memories to exercise eviction (default: 200)")
    # The longest adventure in production carries 886 B of text per action,
    # averaged over both kinds. AI actions alternate with a one-line player
    # input, so the AI half has to be about twice that.
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
    p.add_argument("--rich", action="store_true",
                   help="populate the columns the measuring fixture leaves at "
                        "their defaults: state_after/world_state_after, an "
                        "RPG scenario and live world state, adventure scripts, "
                        "non-zero memory/summary cursors, pinned and forgotten "
                        "memories, distinguishable retry attempts, and a second "
                        "adventure. A correctness fixture rather than a scale "
                        "one — prefer it small (--rich --actions 30). Changes "
                        "what the shapes cost, so do not compare a --rich run "
                        "against a plain one")
    # `_early_keep` also reads this flag at import time, because the database
    # location has to be decided before `app.database` loads. It is declared
    # here so that it appears in `--help` and an unknown flag is still
    # rejected.
    p.add_argument("--keep", metavar="PATH", default="",
                   help="write the fixture to PATH and leave it bootable, so "
                        "the app can serve it in a browser (default: a temp "
                        "file, discarded). SQLite only")
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
    # Attach after the fixture is built. Building it is a write path no player
    # takes, and its bytes would hide everything the shapes report.
    meter.attach(engine)

    print(f"fixture: {args.actions} actions × {args.narration_bytes} B "
          f"(+{args.snapshot_bytes // 1024} kB snapshot, deferred) · "
          f"{args.memories} memories × {EMBEDDING_DIMS} dims · "
          f"capacity {args.capacity}")
    if args.rich:
        print("RICH: correctness fixture — RPG scenario, per-action state "
              "snapshots, scripts, cursors, distinguishable attempts, a second "
              "adventure. Byte figures below are NOT comparable to a plain run.")
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

    # After the shapes, not before: make_bootable() writes, and the meter is
    # still attached until the report above is rendered.
    if args.keep:
        make_bootable()
        print_keep_notes(os.environ["AIDND_DB_PATH"], args.actions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
