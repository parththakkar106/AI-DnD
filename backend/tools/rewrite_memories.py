"""Rewrite memories already in the bank with the current summarizer prompt.

Phase 18b changed what the summarizer is told: a cast brief naming the
protagonist and the other characters, a rule to write in the third person, and
`MEMORY_MAX_WORDS`. Only memories written after that change get any of it. An
adventure played before it keeps a bank of "You entered the crypt" — unnamed,
in the second person, and occasionally a hundred-word paragraph — and those are
the memories that get injected into every turn from now on.

This rewrites them in place, from the same actions they were written from. It is
a one-off backfill, not part of the app.

**In place, rather than deleting and letting the app re-summarize.** A memory
row carries more than its text: whether it is pinned, how often it has been
retrieved, and the node it hangs off, which is what makes a fork inherit the
right memories. Rewriting `text` keeps all of that. Deleting the bank and
rewinding the cursor would lose it, and would then trickle memories back at
`MAX_MEMORIES_PER_RUN` per turn.

**The prompt is not assembled here.** `memorybank.summarize_block` is what the
app itself calls, so a rewritten memory is written by the prompt that is
actually shipping rather than by a copy of it that can drift.

Reading the block back is the one thing the app has never had to do. A memory
records `source_start`, `source_end` and `branch_id`; `memorybank.source_block`
turns those back into actions, on the branch the memory was written on rather
than whichever branch the adventure is on now.

What it will not touch:

- A memory with no source range: hand-written, or migrated from before memories
  had coordinates. There is no block to rewrite it from, and the player may have
  typed it.
- A memory whose actions have since been deleted.
- An adventure whose owner has no API key in Settings. Summarization spends the
  user's own key by construction and never the shared demo key (see the comment
  above `memorybank.summary_provider`), and this holds to that. Pass
  `--endpoint`/`--model`/`--api-key` to summarize with something else.

The vector is cleared for every memory it rewrites, because the stored one
describes the old wording. `--embed` re-embeds them here; without it the app's
own post-turn pass does it, `MAX_EMBED_BATCH` per turn, and until then those
memories are out of the ranked bank. **Stop the app before running with
`--embed`.** A running process caches vectors by memory id and expects to be the
one writing them (see `memorybank._vector_cache`), so a vector written from
outside it can sit behind a stale cached copy until it restarts.

`--embed` always uses the embedding model and endpoint in the owner's Settings,
never `--endpoint`. A vector is only meaningful against the vectors it is ranked
beside, so re-embedding two memories with a different model would quietly put
two coordinate systems in one bank. `--endpoint` moves the summarizer alone.

Usage, from `backend/`:

    python -m tools.rewrite_memories                     # what would change
    python -m tools.rewrite_memories --write --limit 3   # try three of them
    python -m tools.rewrite_memories --write --embed     # the whole backfill

Without `--write` it makes no model calls, spends nothing, and only reports the
scope. Every run reads the database the app reads: `AIDND_DB_PATH`, or
`DATABASE_URL` for a hosted Postgres. Take a copy of it first — the old text is
overwritten and is not kept anywhere.

**On the hosted deploy, name whose adventures you mean.** That database holds
other people's stories, and each adventure is summarized with *its owner's* key,
so an unfiltered `--write` spends other people's money on memories they did not
ask to have rewritten. `--email` restricts the run to the accounts you name and
`--adventure` to single adventures; a dry run costs nothing and lists both, with
the owner of each. Guests have no email and can only be reached by id.

Two environment variables reach that database from a checkout:

    AIDND_DATABASE_URL=<the Neon URL from the Render dashboard> \
    AIDND_SECRET_KEY=<the same value the web service has> \
        python -m tools.rewrite_memories --email you@example.com

`AIDND_SECRET_KEY` is not optional there. Stored API keys are encrypted with it,
and with the wrong one `decrypt_secret` returns "" and every adventure is skipped
as having no key (see `security.py`). The deployed image does not carry this
directory — the Dockerfile copies `backend/app` alone — so run it from a
checkout against the hosted database rather than from a shell on the box.
"""
import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import func, inspect as sa_inspect, select


def words(text: str) -> int:
    return len(text.split())


def one_line(text: str, width: int = 96) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


async def main(args) -> int:
    from app import memorybank, models
    from app.database import DB_PATH, DATABASE_URL, SessionLocal
    from app.providers import OpenAICompatibleProvider, ProviderError

    db = SessionLocal()
    print(f"database: {DATABASE_URL or DB_PATH}")
    if not sa_inspect(db.get_bind()).has_table(models.Adventure.__tablename__):
        # A mistyped path creates an empty SQLite file rather than failing, so
        # say what is wrong instead of raising "no such table: adventures".
        print("There are no tables here. Point AIDND_DB_PATH, or DATABASE_URL "
              "for a hosted deploy, at the database the app uses.")
        return 2

    adventures = db.query(models.Adventure).order_by(models.Adventure.id)
    if args.adventure:
        adventures = adventures.filter(models.Adventure.id.in_(args.adventure))
    if args.email:
        # Compared case-insensitively: an address is typed on the command line
        # here and was typed into a registration form there.
        wanted = [e.strip().lower() for e in args.email]
        owners = db.execute(
            select(models.User.id, func.lower(models.User.email))
            .where(func.lower(models.User.email).in_(wanted))
        ).all()
        unknown = sorted(set(wanted) - {email for _, email in owners})
        if unknown:
            print(f"no account with that email: {', '.join(unknown)}")
            return 2
        adventures = adventures.filter(
            models.Adventure.user_id.in_([user_id for user_id, _ in owners]))
    adventures = adventures.all()
    if args.adventure and len(adventures) != len(set(args.adventure)):
        found = {a.id for a in adventures}
        missing = sorted(set(args.adventure) - found)
        print(f"no such adventure: {', '.join(str(i) for i in missing)}")
        return 2

    # Settings are per user, and several adventures usually share one owner.
    settings_by_user: dict[int, models.Settings | None] = {}

    def settings_for(user_id: int) -> models.Settings | None:
        if user_id not in settings_by_user:
            settings_by_user[user_id] = (
                db.query(models.Settings)
                .filter(models.Settings.user_id == user_id)
                .first()
            )
        return settings_by_user[user_id]

    def provider_for(settings: models.Settings) -> OpenAICompatibleProvider:
        """The adventure owner's own summarizer, unless the run overrides it."""
        if not (args.endpoint or args.model or args.api_key):
            return memorybank.summary_provider(settings)
        return OpenAICompatibleProvider(
            args.endpoint or settings.endpoint_url,
            args.api_key or settings.api_key_plain,
            args.model or settings.summary_model or settings.model,
            settings.api_mode,
            settings.reasoning_max_tokens,
        )

    totals = {"rewritten": 0, "would rewrite": 0, "no source": 0,
              "no story left": 0, "no key": 0, "failed": 0, "empty reply": 0}
    rewritten: list[models.Memory] = []
    budget = args.limit

    for adventure in adventures:
        memories = (
            db.query(models.Memory)
            .filter(models.Memory.adventure_id == adventure.id)
            .order_by(models.Memory.id)
        )
        if not args.include_forgotten:
            memories = memories.filter(models.Memory.forgotten.is_(False))
        memories = memories.all()
        if not memories:
            continue

        settings = settings_for(adventure.user_id)
        # An adventure whose owner has no key is reported rather than skipped
        # silently: it is the one reason a memory this tool can rewrite is left
        # alone, and the operator can fix it with --api-key.
        usable = settings is not None and bool(
            args.api_key or args.endpoint or settings.api_key_plain
        )
        owner = db.get(models.User, adventure.user_id)
        who = (owner.email if owner and owner.email
               else f"guest #{adventure.user_id}")
        print(f"\nadventure {adventure.id}: {adventure.title!r} ({who}) "
              f"— {len(memories)} memories"
              + ("" if usable else "  [owner has no API key in Settings]"))

        for memory in memories:
            if memory.source_start is None or memory.source_end is None:
                totals["no source"] += 1
                print(f"  #{memory.id} skipped: hand-written, no source range")
                continue
            block = memorybank.source_block(db, memory)
            if not block:
                totals["no story left"] += 1
                print(f"  #{memory.id} skipped: the actions it summarized are gone")
                continue
            if not usable:
                totals["no key"] += 1
                continue
            if not args.write:
                totals["would rewrite"] += 1
                print(f"  #{memory.id} would rewrite from {len(block)} actions "
                      f"({words(memory.text)}w): {one_line(memory.text, 70)}")
                continue
            if budget is not None and budget <= 0:
                break
            try:
                text = (await memorybank.summarize_block(
                    adventure, provider_for(settings), block)).strip()
            except ProviderError as exc:
                totals["failed"] += 1
                print(f"  #{memory.id} FAILED: {exc}")
                continue
            if not text:
                totals["empty reply"] += 1
                print(f"  #{memory.id} FAILED: the model returned nothing")
                continue
            print(f"  #{memory.id} {words(memory.text)}w → {words(text)}w")
            print(f"      was: {one_line(memory.text)}")
            print(f"      now: {one_line(text)}")
            memory.text = text
            # The stored vector describes the wording that has just been
            # replaced. Clearing it takes the memory out of the ranked bank
            # (`retrieve_memories` selects on `embedded`) until something
            # embeds the new text.
            memorybank.set_vector(memory, None)
            # Commit each one. A run that dies halfway keeps what it has done,
            # and re-running it costs only the memories still to do.
            db.commit()
            rewritten.append(memory)
            totals["rewritten"] += 1
            if budget is not None:
                budget -= 1
        if budget is not None and budget <= 0 and args.write:
            print("\n--limit reached.")
            break

    embedded = 0
    if args.write and args.embed and rewritten:
        by_adventure: dict[int, list[models.Memory]] = {}
        for memory in rewritten:
            by_adventure.setdefault(memory.adventure_id, []).append(memory)
        print()
        for adventure_id, group in by_adventure.items():
            adventure = db.get(models.Adventure, adventure_id)
            settings = settings_for(adventure.user_id)
            if settings is None or not settings.embedding_model.strip():
                print(f"adventure {adventure_id}: no embedding model in Settings; "
                      f"{len(group)} memories left for the app to embed")
                continue
            done = 0
            for start in range(0, len(group), memorybank.MAX_EMBED_BATCH):
                batch = group[start:start + memorybank.MAX_EMBED_BATCH]
                try:
                    new = await memorybank.embedding_provider(settings).embed(
                        [m.text for m in batch])
                except ProviderError as exc:
                    print(f"adventure {adventure_id}: embedding failed ({exc}); "
                          f"{len(group) - done} left for the app to embed")
                    break
                for memory, vector in zip(batch, new):
                    memorybank.set_vector(memory, vector)
                db.commit()
                done += len(batch)
            embedded += done
            if done:
                print(f"adventure {adventure_id}: embedded {done}")

    print("\n" + "-" * 60)
    for label, count in totals.items():
        if count:
            print(f"{label:>14}: {count}")
    if not args.write:
        print("\nNothing was written. Re-run with --write to rewrite these.")
    elif totals["rewritten"] - embedded > 0:
        left = totals["rewritten"] - embedded
        print(f"\n{left} rewritten {'memory is' if left == 1 else 'memories are'}"
              " unembedded, so they are out of the ranked bank until the app's "
              f"post-turn pass embeds them ({memorybank.MAX_EMBED_BATCH} per "
              "turn).")
    db.close()
    return 1 if totals["failed"] or totals["empty reply"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true",
                        help="rewrite. Without it, only report what would change.")
    parser.add_argument("--adventure", type=int, action="append",
                        help="restrict to this adventure id; repeatable.")
    parser.add_argument("--email", action="append",
                        help="restrict to this account's adventures; repeatable. "
                             "Use it on a shared database.")
    parser.add_argument("--limit", type=int,
                        help="stop after rewriting this many memories.")
    parser.add_argument("--include-forgotten", action="store_true",
                        help="also rewrite evicted memories, which are out of play.")
    parser.add_argument("--embed", action="store_true",
                        help="re-embed here. Stop the app first; see the docstring.")
    parser.add_argument("--endpoint", help="override the owner's endpoint URL.")
    parser.add_argument("--model", help="override the owner's summary model.")
    parser.add_argument("--api-key", help="override the owner's API key.")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(asyncio.run(main(args)))
