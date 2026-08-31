# The protagonist has no name, and the summarizer is told nothing

Two changes, in order. Phase 1 gives the adventure a persona. Phase 2 uses it,
along with the cast, to fix the memories. Phase 1 is worth shipping on its own;
Phase 2 depends on it and is much smaller once it lands.

**Phase 1 is built and green (593 backend tests, frontend builds). Phase 2 is
not started.** Phase 1 has NOT been driven in a browser yet — see "Still to
check by hand" at the end.

**Last updated: 2026-08-31.**

---

## The one sentence version

Memories come back inconsistent and vague because the summarizer is handed six
actions of second-person prose and nothing else — no protagonist, no cast, no
setting, no instruction about what person to write in — so it cannot say who
"you" is or who "she" is, and neither can any memory it writes.

## The evidence

This is the entire prompt that writes a memory (`memorybank.py:391`):

```
system: You compress interactive-fiction story excerpts into memories. Respond
        with 1-2 plain sentences in past tense stating the concrete facts and
        events (names, places, items, promises, injuries). No preamble, no
        commentary.

user:   Story excerpt:

        <6 actions, joined by blank lines, truncated to 2000 tokens>

        Memory:
```

Nothing else is passed. Not `adventure.memory` (the plot essentials), not the
NPC names and descriptions the scenario already defines, not the world state,
and not the protagonist — because until Phase 1 there is no protagonist to pass.

Two failures follow from that, and they are the two complaints:

**Inconsistent framing.** The prompt never says what person to write in. The
model picks one per call. Across a single bank you get "You entered the crypt",
"The player entered the crypt", and "He entered the crypt" describing the same
kind of event.

**No idea who anyone is.** Given `You push the door open. She grabs your arm.`
the only honest memory is *"You entered a room and she stopped you."* Retrieved
forty turns later into a scene with three women in it, that memory is worse than
nothing.

The summary inherits both problems, because `_update_story_summary` builds from
the memory bullets.

---

# Phase 1 — the persona

## What already exists, and what does not

`stat_schema` already gives the player a stat block, and it is already
namespaced beside the NPCs:

```
world.day
player.hp          <- the player's stats, already sectioned
npc.gwen.trust
flags.alarm_raised
milestones.escaped
```

NPCs carry `name`, `keys`, and `desc` (`schema.py`, `render_reference`). The
`player` section carries none of those. That asymmetry is the whole gap: the
player has stats but no identity, so the block renders as `You: hp 100/100` and
reads to the model as a floating global rather than a character.

## The paths do not change

`player.hp` stays `player.hp`. It does not become `kaelen.hp` or `user.hp`.

The reason is `_history_text()` in `context/builder.py`. It replays every past
turn's stored delta back into the prompt, and those stored blobs contain literal
strings like `{"player.hp": -15}`. A path that carries the persona's name breaks
the moment a player renames their character: every replayed block in history
then names a path the schema no longer defines, `applied_delta` renders it
anyway, and the model copies the broken form. A rename would also require
migrating every stored `world_state`, every seed scenario, and the `EMIT_RULE`
example text, for no functional gain.

What changes is the **label**, using the trick NPCs already use — print the
display name and the path together:

```
before:  You: hp 100/100, mana 30/50.
after:   Kaelen (player): hp 100/100, mana 30/50.
```

The name is visible to the reader, the path stays copyable by the model.

## Where the persona lives

Three plain columns on `adventures`, alongside `memory` and `authors_note`:

```python
persona_name: Mapped[str] = mapped_column(String(80), default="")
persona_pronouns: Mapped[str] = mapped_column(String(40), default="")
persona_desc: Mapped[str] = mapped_column(Text, default="")
```

**Not in `stat_schema`.** Two reasons. It has to work for an adventure with no
RPG layer at all, which is most of them, and `_initials()` in
`worldstate/schema.py` treats every dict inside a section as a stat definition —
a `persona` key dropped into `stat_schema.player` would be instantiated as a
stat, rendered as a stat line in the guide, and handed an `initial` value.

**Adventure-level, not scenario-level.** Two people playing the same scenario
are different characters. A scenario steers the protagonist through its plot
essentials, which it already can. Keeping personas off `scenarios` also avoids
having to decide what "Update from scenario" does to a persona the player has
edited: the answer is nothing, because the scenario never had one.

**Not per-branch.** Branches are alternative futures within one adventure; the
protagonist is the same person down all of them.

Empty `persona_name` means the feature is off and behavior is exactly what it is
today. That is the entire backward-compatibility story — no backfill.

## Pronouns get their own field

One short string: `he/him`, `she/her`, `they/them`. It exists because Phase 2
will tell the summarizer to write in third person. Without a stated pronoun the
model infers one from the name, and once it infers wrong that error is baked
into every memory it writes from then on and into the summary built from them.
A field is cheaper than a wrong guess repeated forever.

Blank is a valid value. When it is blank nothing is rendered, and the Phase 2
prompt tells the summarizer to use the name rather than a pronoun.

## Where it enters the prompt

A new static section in `build_context`, between `ai_instructions` and
`plot_essentials`:

```
Player character:
You are Kaelen (he/him). A half-elf ranger, exiled from the northern holds.
```

**It goes in the system block on purpose.** The description is user-only and
never changes during a turn, so it sits inside the cached prefix and costs
nothing after the first turn. This is why "only the user can change it" is not
just a product choice — it is what keeps the section free. Anything the AI could
rewrite would have to move below the history with the other live sections, and
would re-price the prompt on every change.

It is emitted whether or not the adventure has an RPG layer. It is not gated
behind `has_ws`.

## Where it enters the world state

`worldstate/render.py`, two edits:

- `render_state_section` takes the display name and prints
  `Kaelen (player): …` in place of the hardcoded `You: …`. Falls back to `You:`
  when no persona is set.
- `render_reference` gains one line tying the name to the path, mirroring the
  NPC header it already writes:
  `- Protagonist Kaelen — stats are addressed as player.<stat>.`
  Without this the model reads "Kaelen" in the prose and invents `kaelen.hp`.

The persona description is **not** repeated in the stat guide. It already has
its own section, and the guide is about paths.

## The AI cannot edit it

Nothing to build. `_resolve()` in `worldstate/apply.py` rejects any path it does
not recognize, so a delta of `{"persona.name": "Bob"}` already falls through to
"`persona.name` is not a tracked value", and that refusal already reaches the
model through `render_refusals`. Worth one test to hold the behavior in place.

## Setting it, and changing it

**At the start.** `PlaceholderModal` (`components.jsx`) already exists and
already opens before an adventure begins — but only when the scenario contains
`${...}` tokens. It becomes a "Begin adventure" modal that always opens, with
the three persona fields at the top and any placeholder fields below.

**Placeholders stay independent of the persona.** A scenario using `${Name}`
will ask for a name twice. That is accepted: no scenario in the repo uses
placeholders at all (grepped across `seed_data/` and `starter_data/` — zero
occurrences), so the collision is hypothetical. If it ever shows up in practice,
pre-filling the `Name` field from the persona is a few lines in the modal with
no backend change.

**Later.** `AdventureUpdate` gains the three fields, and `PlotPanel` gets a
Protagonist block at the top. The debounced-save wiring is already there.
`WorldStateDrawer` shows the name as the character-sheet heading but does not
edit it — one edit surface, not two.

## Files

| File | Change |
|---|---|
| `models.py` | three columns on `Adventure` |
| `migrations.py` | 74, 75, 76 |
| `schemas.py` | `AdventureCreate`, `AdventureUpdate`, `AdventureOut` |
| `context/builder.py` | `persona` section after `plot_essentials` |
| `worldstate/render.py` | state-section label, reference line |
| `worldstate/__init__.py` | export whatever `render.py` newly exposes |
| `routers/adventures/crud.py` | store persona at creation |
| `bundle.py` | export key + import read |
| `frontend/components.jsx` | modal gains persona fields |
| `frontend/pages/Scenarios.jsx`, `Home.jsx` | always open the modal |
| `frontend/pages/Play/panels/PlotPanel.jsx` | Protagonist block |
| `frontend/pages/Play/drawers/WorldStateDrawer.jsx` | heading |
| `frontend/api.js` | pass the fields through |

## Tests

| File | What |
|---|---|
| new `test_persona.py` | create with persona; update; renders in the system block; works with no `stat_schema`; empty name is a no-op; AI delta at `persona.*` refused |
| `test_worldstate.py` | state line reads `Kaelen (player):`; `player.hp` still resolves |
| `test_prompt_caching.py` | persona is in the static prefix, above history |
| `test_bundle_v2.py` | round-trips; a bundle without the key still imports |

## Bundle format

No `FORMAT` bump. The export gains a `persona` key and the import reads it with
`.get()`, so a v2 bundle written before this change imports with an empty
persona — which is the same as not having one.

---

# Phase 2 — what the summarizer is told

Depends on Phase 1 only for the protagonist's name. Everything else it needs is
already in the database and simply never passed.

## The cast brief

Build one short block and prepend it to both the memory prompt and the summary
prompt:

```
Cast:
- Kaelen (he/him) — the protagonist. A half-elf ranger, exiled from the
  northern holds...
- Gwen — a loyal ranger and Kaelen's ally. Quick with a bow, dry-humoured.

Setting:
<adventure.memory, the plot essentials>
```

Sources, in order:

1. **Protagonist** — the Phase 1 persona. Falls back to "the player" when unset,
   so the change still helps adventures with no persona.
2. **NPCs** — `stat_schema.npcs`: `name` + `desc`. Already there, never used
   outside the turn prompt.
3. **Story cards** — for adventures with no RPG layer, run the existing
   `_match_cards()` over the block being summarized and take the matched cards'
   names and entries. This reuses the trigger code and yields exactly the
   entities that appear in *that block*, not the whole world.

## Two rules for the brief

**Fixed descriptions only. No live stats.** It is tempting to include
`Gwen: trust 40 (wary)`. Do not. It makes every memory prompt different, which
loses prompt caching, and worse, it makes the same event summarized at two
different times come out framed differently — which is the problem being fixed.

**The summary gets it for free.** `_update_story_summary` builds from the memory
bullets, so better memories produce a better summary with no further change.
Pass the brief there too, because that function falls back to raw story text
when memory-writing has fallen behind.

## The prompt changes

`MEMORY_SYSTEM_PROMPT` gains an explicit framing rule:

- write in third person, never "you"
- refer to the protagonist by name
- name characters rather than using bare pronouns

Expected effect:

```
before:  You entered a room and she stopped you.
after:   Kaelen bribed Gwen with fifty silver to hold the north door while
         he went down alone.
```

## Open questions for Phase 2

**Existing memories.** A bank written under the old prompt will sit alongside
new ones, mixing "you" and "Kaelen" in the same context. Options: leave them and
let eviction age them out at `memory_bank_capacity` (80), or clear
`memory_cursor` and re-summarize from the start. Re-summarizing costs one AI
call per 6 actions and duplicates whatever is still in the bank, since nothing
deletes the old rows. **Leaning: leave them.** Decide when the framing change is
measured, not before.

**Retrieval framing mismatch.** `retrieve_memories` embeds the last 4 actions
raw — second person, "you". New memories will be third person and named.
Embeddings handle paraphrase well, so this is probably minor, but if retrieval
quality visibly dips after Phase 2 this is the first place to look. A fix would
be to prepend the same cast brief to the query text.

**Token cost.** The brief adds roughly 100–200 tokens to one call per 6 actions
and one per 15. Negligible against a turn.

---

# Phase 1 as built

Everything above describes what shipped. Three notes on where it differs from
the sketch, and what has not been verified.

## Decisions taken during the build

**The section reads as one paragraph.** `render_persona` joins the sentence and
the description with a space rather than `SEPARATOR`. A blank line inside a
two-sentence section about one character reads as two unrelated notes.

**`Field` gained a `maxLength` prop.** The persona name is `VARCHAR(80)` and the
schema rejects 81 characters with a 422. Without the attribute the player only
learns that from a toast after typing, so the cap is now enforced in the input
as well. Every other `Field` is unaffected — the prop is optional.

**The blank-adventure button opens the modal too.** It used to create the
adventure immediately. It now collects a persona first, and passes
`title: 'Blank Adventure'` explicitly, because there is no scenario to take a
title from.

**`test_prompt_caching.py`'s fixture now sets a persona**, so every test in the
file that guards the static block runs with one present.

## Verified

- Migration 73 → 76 on a real pre-existing SQLite database: columns added, the
  existing row preserved, persona empty, `render_persona` returns `""`.
- 593 backend tests pass, including 26 new ones in `test_persona.py` and 3 in
  `test_bundle_v2.py`.
- `npm run build` is clean.

## Still to check by hand

None of this was driven in a browser. Worth ten minutes before trusting it:

1. Start an adventure from a scenario. The modal should open even though no
   scenario in the repo uses `${...}` placeholders.
2. Play one turn on the RPG scenario (seed 04) and open Insights. The `persona`
   section should be in the system block, and the world-state line should read
   `<name> (player): hp 100/100`.
3. Rename the character in the Plot panel. The world-state drawer heading
   should follow it.
4. Leave every persona field blank and confirm the prompt is byte-identical to
   what it was before this change.

## A note for whoever runs the tests

`tiktoken` downloads `cl100k_base` from `openaipublic.blob.core.windows.net` on
first use, and 182 tests fail with a proxy error where that host is blocked.
The encoding is reconstructable offline from the npm package `js-tiktoken`,
whose `dist/ranks/cl100k_base.cjs` holds the same ranks in a compressed form —
decode it, write `<base64> <rank>` per line sorted by rank, and the result
matches the SHA-256 that `tiktoken_ext/openai_public.py` hardcodes, so the
reconstruction is verifiable rather than trusted. Drop it at
`$TIKTOKEN_CACHE_DIR/<sha1 of the URL>`.
