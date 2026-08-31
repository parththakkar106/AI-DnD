"""A/B the memory prompt against a real model, on one story held constant.

Reviewing a prompt tells you what it asks for. It does not tell you what a model
does with it. This runs the real pipeline twice over the *same* blocks of the
*same* story, changing only the prompt, and prints the memories side by side.

What it showed the first time it was run, and why `MEMORY_MAX_WORDS` exists, is
written up in `plan/18-persona-and-memory-quality.md`. Two consecutive memories
from one story came back in two different persons, and the same model wrote 34
words for one block and 105 for the next.

Three things make it a fair test rather than a demonstration:

- **The story is generated once**, through the app's own `build_context`, and
  both arms summarize the same actions. The prompt is the only variable.
- **The control is read from git**, at the commit named by `--before`, so it is
  the prompt that actually shipped rather than a paraphrase of it.
- **Every call goes through `OpenAICompatibleProvider`** to the endpoint in
  `--endpoint`, so this exercises the provider, the streaming path and
  `complete()` rather than a stub.

Point it at `tools/claude_shim.py` to spend a Claude subscription instead of API
credit:

    python tools/claude_shim.py --port 8787 &
    python tools/memory_ab.py --out /tmp/ab.md

Point `--endpoint` at the provider the deployed app really uses to learn
something this cannot tell you: whether a weaker model follows the framing rule
as well as a Claude model does.

Options:

    --endpoint  OpenAI-compatible base URL. Defaults to the local shim.
    --model     Model name to ask that endpoint for. Defaults to `sonnet`.
    --before    Git commit holding the prompt to compare against.
    --turns     How many player turns to generate. Six gives two blocks.
    --out       Write the full transcript here as Markdown.

This writes to a scratch SQLite file and drops it afterwards. It never touches
`backend/data.db`.
"""
import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# The moves the player makes. Fixed, so that a re-run summarizes comparable
# story rather than a different adventure.
PLAYER_TURNS = [
    "go quiet and signal Gwen to circle around the right flank",
    "search the nearest bedroll for anything useful",
    "move toward the strongbox, keeping low",
    "grab her wrist and pull her down behind the woodpile",
    "wait for the bandit to turn, then move",
    "ask her whether she still trusts my read on this",
]


def prompt_at(commit: str, name: str) -> str:
    """Reads one prompt constant out of `memorybank.py` as of `commit`.

    Read from git rather than pasted here, so that the control cannot drift out
    of step with what was actually shipped.
    """
    source = subprocess.run(
        ["git", "show", f"{commit}:backend/app/memorybank.py"],
        capture_output=True, text=True, cwd=REPO, check=True,
    ).stdout
    match = re.search(rf"^{name} = \((.*?)^\)$", source, re.S | re.M)
    if not match:
        raise SystemExit(f"{name} not found in memorybank.py at {commit}")
    namespace: dict = {"MEMORY_MAX_WORDS": 50, "SUMMARY_MAX_WORDS": 250}
    return eval(f"({match.group(1)})", namespace)  # noqa: S307 — our own source


def words(text: str) -> int:
    return len(text.split())


def framing(text: str) -> str:
    """How this memory refers to the protagonist. The thing being measured."""
    tags = []
    if re.search(r"\byou(r)?\b", text, re.I):
        tags.append('second person ("you")')
    if re.search(r"\bthe player\b", text, re.I):
        tags.append('"the player"')
    return ", ".join(tags) or "named"


async def main(args) -> None:
    from app import memorybank, models, tree, worldstate
    from app.context import build_context, truncate_to_last_tokens
    from app.database import Base, SessionLocal, engine
    from app.providers import OpenAICompatibleProvider
    from app.routers.adventures.scenario_text import scenario_card_specs
    from app.seed import seed_public_scenarios

    def provider(model: str | None = None) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            args.endpoint, "unused-by-the-shim", model or args.model, "chat", 0)

    async def generate(system: str, story: str) -> str:
        """One turn, through the provider's real streaming path."""
        from app.providers.base import PromptParts
        out = []
        async for kind, piece in provider().generate(
            PromptParts(system=system, story=story), temperature=0.8,
            max_tokens=args.max_tokens,
        ):
            if kind == "text":
                out.append(piece)
        return "".join(out).strip()

    Base.metadata.create_all(bind=engine)
    seed_public_scenarios(engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="memory-ab@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(user_id=user.id, api_key="enc:d",
                               model=args.model, max_output_tokens=args.max_tokens)
    db.add(settings)
    scenario = db.query(models.Scenario).filter(
        models.Scenario.title.like("%Bandit Camp%")).one()

    adventure = models.Adventure(
        user_id=user.id, title=scenario.title, scenario_id=scenario.id,
        script_state={}, auto_summarize=True, memory=scenario.memory,
        authors_note=scenario.authors_note,
        ai_instructions=scenario.ai_instructions,
        world_state=worldstate.instantiate(scenario.stat_schema),
        persona_name="Kaelen", persona_pronouns="he/him",
        persona_desc=("A half-elf ranger, exiled from the northern holds for a "
                      "killing he still won't explain. Wary of nobles, soft on "
                      "strays."),
    )
    db.add(adventure)
    db.flush()
    tree.head_branch(db, adventure)
    for ref, spec in scenario_card_specs(scenario, {}).items():
        db.add(models.StoryCard(adventure_id=adventure.id, source_ref=ref, **spec))
    opening = models.Action(adventure_id=adventure.id, type="start",
                            text=scenario.prompt)
    tree.place_action(db, adventure, opening)
    db.add(opening)
    db.commit()
    db.refresh(adventure)

    print(f"Generating {args.turns} turns through build_context…", flush=True)
    for i, move in enumerate(PLAYER_TURNS[:args.turns], 1):
        player = models.Action(adventure_id=adventure.id, type="do", text=move)
        tree.place_action(db, adventure, player)
        db.add(player)
        db.commit()
        db.refresh(adventure)

        system_text, story_text, _ = build_context(adventure, settings)
        clean, delta = worldstate.extract_delta(await generate(system_text, story_text))
        ai = models.Action(adventure_id=adventure.id, type="ai", text=clean)
        tree.place_action(db, adventure, ai)
        db.add(ai)
        if delta:
            adventure.world_state, report = worldstate.apply_delta(
                adventure.world_state, scenario.stat_schema, delta, i)
            ai.world_delta = report
        db.commit()
        db.refresh(adventure)
        print(f"  [{i}] {move} → {words(clean)} words"
              + (f", state {delta}" if delta else ""), flush=True)

    old_system = prompt_at(args.before, "MEMORY_SYSTEM_PROMPT")
    actions = sorted(
        (a for a in adventure.actions if a.type in ("start", "do", "ai")),
        key=lambda a: (a.depth if a.depth is not None else 0, a.id),
    )
    blocks = [actions[i:i + memorybank.MEMORY_INTERVAL]
              for i in range(0, len(actions) - memorybank.MEMORY_INTERVAL + 1,
                             memorybank.MEMORY_INTERVAL)]
    print(f"\n{len(actions)} actions → {len(blocks)} blocks. Summarizing each twice…",
          flush=True)

    rows = []
    for n, block in enumerate(blocks, 1):
        raw = "\n\n".join(a.text for a in block)
        plain = (f"Story excerpt:\n\n"
                 f"{truncate_to_last_tokens(raw, 2000)}\n\nMemory:")
        brief = memorybank.cast_brief(adventure, raw)
        before = await provider(args.model).complete(old_system, plain)
        after = await provider(args.model).complete(
            memorybank.MEMORY_SYSTEM_PROMPT,
            f"{brief}\n\n{plain}" if brief else plain)
        rows.append((n, before.strip(), after.strip()))
        print(f"  block {n}: before {words(before)}w ({framing(before)}), "
              f"after {words(after)}w ({framing(after)})", flush=True)

    print("\n" + "=" * 74)
    print(f"{'':16}{'words':>6}  framing")
    print("-" * 74)
    for n, before, after in rows:
        for label, text in (("before", before), ("after", after)):
            print(f"memory {n} {label:7}{words(text):>6}  {framing(text)}")
    print("=" * 74)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("# Memory prompt A/B\n\n")
            fh.write(f"One story, generated through `build_context` against "
                     f"`{args.model}` at `{args.endpoint}`. Both prompts then "
                     f"summarize the same blocks, so the prompt is the only "
                     f"variable. The control is `MEMORY_SYSTEM_PROMPT` as of "
                     f"`{args.before}`.\n\n")
            fh.write("| memory | arm | words | framing |\n|---|---|---|---|\n")
            for n, before, after in rows:
                for label, text in (("before", before), ("after", after)):
                    fh.write(f"| {n} | {label} | {words(text)} | {framing(text)} |\n")
            for n, before, after in rows:
                fh.write(f"\n## Memory {n}\n\n**Before:** {before}\n\n"
                         f"**After:** {after}\n")
            fh.write("\n## The story both arms summarized\n\n")
            for a in actions:
                fh.write(f"**{a.type}:** {a.text}\n\n")
        print(f"\nwritten to {args.out}")

    db.close()
    Base.metadata.drop_all(bind=engine)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--endpoint", default="http://127.0.0.1:8787/v1")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--before", default="9cdcb55",
                        help="commit holding the prompt to compare against")
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--out")
    args = parser.parse_args()

    # A scratch database, so a run never touches the developer's own.
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    os.environ["AIDND_DB_PATH"] = handle.name
    os.environ.pop("AIDND_DATABASE_URL", None)
    os.environ.pop("DATABASE_URL", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        asyncio.run(main(args))
    finally:
        Path(handle.name).unlink(missing_ok=True)
