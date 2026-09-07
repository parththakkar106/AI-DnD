"""Migrations 77 and 78: the story summary column becomes a row on the tree.

The text in `adventures.story_summary` is the only summary an upgrading player
has, and 78 drops the column that holds it. If 77's backfill misplaces the row
or skips an adventure, nothing raises: the player opens their adventure and the
summary is simply gone, or is invisible from the branch they are on.

    python -m pytest tests/test_summary_migration.py -v
"""
import pytest
from sqlalchemy import text

from app import migrations, models
from app.database import Base, SessionLocal, engine
from tests import schema_rewind


@pytest.fixture()
def pre_summaries():
    """A database stamped at 76, with the column back and three adventures in it.

    * "Anchored" has a summary and a cursor, so the row belongs at that node.
    * "Unanchored" has a summary and no cursor, which is what an adventure whose
      player typed one but never reached an update looks like.
    * "Blank" has no summary at all and must get no row.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    schema_rewind.rewind_to(engine, migrations.SUMMARY_ROWS_VERSION - 1)
    ids = {}
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, is_guest, created_at, demo_turns_used, "
            "demo_turns_date) VALUES (1, 'mig@example.com', 0, CURRENT_TIMESTAMP, 0, '')"
        ))
        for name, summary in (
            ("Anchored", "Folded in up to the third turn."),
            ("Unanchored", "Typed, and never updated."),
            ("Blank", ""),
        ):
            # Every NOT NULL column is named. `create_all` builds them without
            # SQL defaults, because the defaults are Python-side, so a raw
            # INSERT has to supply them.
            conn.execute(text(
                "INSERT INTO adventures ("
                "  user_id, title, memory, authors_note, ai_instructions,"
                "  persona_name, persona_pronouns, persona_desc,"
                "  script_state, world_state, auto_summarize, memory_bank_enabled,"
                "  story_summary, head_depth, memory_cursor_depth,"
                "  summary_cursor_depth, created_at, updated_at"
                ") VALUES ("
                "  1, :t, '', '', '', '', '', '', '{}', '{}', 0, 0,"
                "  :s, 3, -1, -1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"t": name, "s": summary})
            ids[name] = conn.execute(
                text("SELECT id FROM adventures WHERE title = :t"), {"t": name}
            ).scalar()
            conn.execute(text(
                "INSERT INTO branches (adventure_id, parent_branch_id, fork_depth, "
                "lineage, created_at) VALUES (:a, NULL, NULL, '[]', CURRENT_TIMESTAMP)"
            ), {"a": ids[name]})
            branch = conn.execute(text(
                "SELECT id FROM branches WHERE adventure_id = :a"), {"a": ids[name]}
            ).scalar()
            conn.execute(text(
                "UPDATE branches SET lineage = json_array(json_array(id, null)) "
                "WHERE id = :b"), {"b": branch})
            conn.execute(text(
                "UPDATE adventures SET head_branch_id = :b WHERE id = :a"),
                {"b": branch, "a": ids[name]})
            for depth in range(4):
                conn.execute(text(
                    "INSERT INTO actions (adventure_id, branch_id, depth, type, text, "
                    "live, created_at) VALUES (:a, :b, :d, 'ai', :x, 1, CURRENT_TIMESTAMP)"
                ), {"a": ids[name], "b": branch, "d": depth, "x": f"Turn {depth}."})
        conn.execute(text(
            "UPDATE adventures SET summary_cursor_branch_id = "
            "(SELECT id FROM branches WHERE adventure_id = :a), summary_cursor_depth = 3 "
            "WHERE id = :a"), {"a": ids["Anchored"]})
    try:
        yield ids
    finally:
        Base.metadata.drop_all(bind=engine)


def test_the_text_moves_into_a_row_where_the_cursor_says_it_had_got_to(pre_summaries):
    migrations.bootstrap(engine)
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, pre_summaries["Anchored"])
        row = db.query(models.Summary).filter(
            models.Summary.adventure_id == adventure.id).one()
        assert row.text == "Folded in up to the third turn."
        assert row.depth == 3, "the summary cursor named the node it had read to"
        assert row.branch_id == adventure.head_branch_id
        assert row.source_start == 0, (
            "a summary built by repeated updates read from the beginning, so "
            "withdrawing it has to rewind that far"
        )
        assert adventure.story_summary == "Folded in up to the third turn."
    finally:
        db.close()


def test_an_adventure_with_no_cursor_keeps_its_summary_on_the_opening_node(
    pre_summaries
):
    """Depth 0 is at or before every fork point, so the row stays visible from
    every branch the adventure can grow. Anchoring later would hide the only
    summary the player has from a line that forked before it."""
    migrations.bootstrap(engine)
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, pre_summaries["Unanchored"])
        row = db.query(models.Summary).filter(
            models.Summary.adventure_id == adventure.id).one()
        assert row.depth == 0
        assert adventure.story_summary == "Typed, and never updated."
    finally:
        db.close()


def test_an_adventure_with_no_summary_gets_no_row(pre_summaries):
    migrations.bootstrap(engine)
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, pre_summaries["Blank"])
        assert db.query(models.Summary).filter(
            models.Summary.adventure_id == adventure.id).count() == 0
        assert adventure.story_summary == ""
    finally:
        db.close()


def test_the_column_is_gone_and_the_stamp_is_current(pre_summaries):
    migrations.bootstrap(engine)
    with engine.begin() as conn:
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(adventures)"))
        }
        assert "story_summary" not in columns, (
            "the column has to go, or it is a second place a summary can live "
            "and the two disagree the moment either is written"
        )
        assert conn.execute(text("PRAGMA user_version")).scalar() == (
            migrations.LATEST_VERSION
        )


def test_running_the_backfill_twice_writes_one_row(pre_summaries):
    """The pass runs inside the transaction that holds the schema change, so a
    failure anywhere in the loop replays the whole thing on the next boot."""
    migrations.bootstrap(engine)
    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA user_version = {migrations.SUMMARY_ROWS_VERSION - 1}"))
        conn.execute(text(
            "ALTER TABLE adventures ADD COLUMN story_summary TEXT NOT NULL DEFAULT ''"
        ))
        conn.execute(text(
            "UPDATE adventures SET story_summary = 'Folded in up to the third turn.' "
            "WHERE title = 'Anchored'"
        ))
    migrations.bootstrap(engine)
    db = SessionLocal()
    try:
        assert db.query(models.Summary).filter(
            models.Summary.adventure_id == pre_summaries["Anchored"]).count() == 1
    finally:
        db.close()
