# The protagonist has no name, and the summarizer is told nothing

Two changes, in order. Phase 1 gives the adventure a persona. Phase 2 uses it,
along with the cast, to fix the memories. Phase 1 is worth shipping on its own;
Phase 2 depends on it and is much smaller once it lands.

**Both phases are built and green (631 backend tests). Phase 1 was driven in a
browser (21/21 checks). Phase 2 was run end to end against a real model, as a
controlled A/B on one story — see "Run with a real model". A bank written under
the old prompt can be rewritten in place — see the last section.**

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

## Driven in a browser

Chromium via Playwright, against a fresh database with the demo scenarios
seeded. No API key needed: Insights assembles the prompt without calling a
model, so every check below runs on the real assembled context rather than on
a unit-test stub. 21/21 checks passed.

What was confirmed on screen and in the live `/context` payload:

| | |
|---|---|
| The modal opens for a scenario with **no** `${...}` placeholders | it is now the only way to name a character, so it can no longer be conditional |
| The section order is real | `narrator, world_state_guide, world_state_rule, ai_instructions, **persona**, plot_essentials, …` |
| The persona is in the system half | `You are Kaelen (he/him). A half-elf ranger…` present in `prompt.system`, absent from `prompt.story` |
| The live stat line carries the name and the path | `Kaelen (player): hp 100/100 (full health), mana 30/50 (brimming)…` |
| The stat guide ties the name to the path | `Protagonist Kaelen … player.<stat>` |
| The drawer heading follows the name | rail read `KAELEN`, not `You` |
| A rename propagates | renamed to Aria in the Plot panel → drawer read `ARIA` after a reload, and the prompt read `You are Aria (he/him).` / `Aria (player):` |
| Clearing every field restores the old behavior exactly | no `persona` section, stat line back to `You:`, no `Protagonist` line in the guide |
| The blank-adventure button collects a persona and keeps its title | `title='Blank Adventure' persona='Wren'` |
| **A persona works with no RPG layer at all** | a blank adventure's sections were `['narrator', 'persona', 'length_hint']` — the case this feature was added for |

Two request failures in the run were the sandbox rather than the app: Google
Fonts is blocked by the egress policy, and the analytics beacon is aborted when
the page unloads. Neither appears with normal network access.

The driver script is not in the repo. There is no frontend test runner yet
(plan/17 stage 5), and one Playwright script is not the place to start one.

## A note for whoever runs the tests

`tiktoken` downloads `cl100k_base` from `openaipublic.blob.core.windows.net` on
first use, and 182 tests fail with a proxy error where that host is blocked.
The encoding is reconstructable offline from the npm package `js-tiktoken`,
whose `dist/ranks/cl100k_base.cjs` holds the same ranks in a compressed form —
decode it, write `<base64> <rank>` per line sorted by rank, and the result
matches the SHA-256 that `tiktoken_ext/openai_public.py` hardcodes, so the
reconstruction is verifiable rather than trusted. Drop it at
`$TIKTOKEN_CACHE_DIR/<sha1 of the URL>`.

---

# Phase 2 as built

## The cast comes from the story cards, not from `stat_schema`

This is the one thing the sketch above got wrong, and it made the change much
smaller. `scenario_text.scenario_card_specs` already turns **every schema NPC
into a story card** on the adventure at creation, deduplicated against the
hand-written cards by name. So the cards are a single unified cast source that
covers schema NPCs, an author's own cards, and an adventure with no RPG layer,
through one path instead of three. `memorybank` never reads `stat_schema`.

## Keyword matching alone was not enough

The sketch said to run `_match_cards` over the block. Built that way first, and
it failed the exact case the change exists for.

The block `You push the door open. She grabs your arm.` matches **no** card
keyword, so the brief listed the protagonist and nobody else — leaving the
summarizer guessing at precisely the moment it was handed a brief to stop
guessing. Seed 04 gives Gwen the trigger keys `"Gwen, ranger, her"`, and even
that does not save it: the text says "she", not "her".

So the roster is **matched cards first, then topped up with the other
`character` cards** to `MAX_CAST_MEMBERS`. Places and items are not topped up —
an unmentioned tavern is not who "she" was — but a place that *is* mentioned
still matches normally.

That asymmetry with the turn prompt is deliberate. Including an untriggered card
as lore would be wrong: it is not relevant to the next sentence. Including an
untriggered character in a roster is right: the question the roster answers is
"who could these pronouns be", not "what is on stage".

## `_match_cards` became `match_cards`

Two callers now run the same rule, so it is public and exported from
`app.context`. One rule, one implementation.

## What actually gets sent

Verified against the seeded Bandit Camp scenario, with the real
`_create_due_memories` path and a stub provider:

```
Cast:
- Kaelen (he/him) — the protagonist. A half-elf ranger, exiled from the
  northern holds for a killing he still won't explain.
- Bandit Camp — A rough camp of bandits in a forest clearing, holding a
  stolen caravan strongbox.
- Gwen — A loyal ranger and the player's ally. Quick with a bow,
  dry-humoured, fiercely protective. …
- Bandit Leader — The scarred leader of the bandit camp, guarding the
  strongbox. …

Setting:
The player and Gwen, a loyal ranger ally, are raiding a bandit camp to
recover a stolen strongbox. …

Story excerpt:

… You are six paces from the strongbox when she hisses a warning. …

Memory:
```

That last line is the case in miniature: "she" is now resolvable.

**With no persona set**, the roster still lists the NPCs and the setting, and
the system prompt tells the model to call the protagonist "the player". Phase 2
therefore improves adventures that never set a persona at all.

**With no persona, no cards and no plot essentials**, the user message is byte
for byte what it was before this change — `Story excerpt:` first, no stray blank
lines. A test holds that.

## Decided, from the sketch's open questions

**Existing memories: left alone at first, and now rewritable on demand.** The
original answer was to let eviction age them out at `memory_bank_capacity` (80),
on the grounds that re-summarizing would duplicate whatever was still in the
bank, because nothing deletes the old rows. That reasoning was wrong about the
only option: a memory can be rewritten in place rather than written again.
See "Rewriting a bank written under the old prompt", at the end of this file.

**Retrieval framing mismatch: watched, not fixed.** `retrieve_memories` still
embeds the last 4 actions raw, in second person, while new memories are third
person and named. Embeddings handle paraphrase well, so this is speculative.
If retrieval quality visibly dips, prepending the same brief to the query text
is the first thing to try.

## Run with a real model, and what it changed

Run end to end against a real model. One story generated through the app's own
`build_context` a turn at a time, then **both** memory prompts run over the
**same** blocks, so the story is held constant and the prompt is the only
variable. Neither arm sees the other's output, and the model is never told what
is being measured. The control is `MEMORY_SYSTEM_PROMPT` as of commit `9cdcb55`,
read out of git rather than pasted, so it cannot drift from what shipped.

The harness is `backend/tools/memory_ab.py`, and it goes through
`OpenAICompatibleProvider` rather than calling a model directly, so the run
exercises the provider, the streaming path and `complete()`. Pointed at
`tools/claude_shim.py` it spends a Claude subscription instead of API credit:

    python tools/claude_shim.py --port 8787 &
    python tools/memory_ab.py --out ab.md

**The full transcript, with every memory, both summaries and the story they were
written from, is in `plan/18-appendix-memory-ab-run.md`.**

### The reported fault reproduced, and the fix held

| | words | framing |
|---|---|---|
| memory 1, before | 34 | second person — "**You** crept low through the mist…" |
| memory 2, before | 105 | third person — "**The player** asked Gwen to…" |
| memory 1, after | 72 | "**Kaelen** and Gwen crouched at dawn…" |
| memory 2, after | 68 | "**Kaelen** and Gwen infiltrated the bandit camp…" |

Two consecutive memories in one bank, written from the same story minutes apart,
in two different persons. That is the complaint, reproduced under controlled
conditions rather than argued from the prompt text.

**A second run, on a freshly generated story, qualifies that.** Its two control
memories were both "the player", consistently — the second-to-third person drift
did not recur. So the drift is a real thing a model does, seen once, not
something it does every time. Read the person-drift row as one observation.

What holds across both runs is the thing the change is actually for: **four
control memories, and not one names the protagonist. Four treatment memories,
and all four do.** The control has never been told a name. See
`plan/18-appendix-memory-ab-run-2.md`.

The control also got a fact wrong that the treatment did not. The player's move
was `grab her wrist and pull her down behind the woodpile`; the before-memory
recorded "The player asked Gwen to grab her wrist and pull her down behind the
woodpile", inverting who acted. One sample, so this is an observation rather
than a claim — but it is the failure mode the brief predicts, since without a
cast there is no way to tell whose wrist "her wrist" is.

### It also found a real problem, which is now fixed

"1-2 plain sentences" is not a length. The same model wrote 34 words for one
block and 105 for the next. A 105-word memory is a paragraph, and
`memory_top_k` injects five of them every turn, so the bank's running cost is
set by a number nobody had ever stated.

`MEMORY_MAX_WORDS = 50` now states it, and the prompt says which details to keep
when trimming: the ones a later scene could turn on. Re-run over the identical
story, the same two blocks came back at **32 and 58 words**, still naming
Kaelen, still third person, and still carrying every load-bearing fact — the
camp map, the strongbox behind the second tent, the strap frayed near through.
The second run, on different prose, came back at **54 and 55** against controls
of 89 and 107. Overshooting 50 slightly is expected: models exceed word budgets,
which is why `builder.length_hint` carries a `LENGTH_BUFFER` for the same reason.

Across both runs the control ran 34, 89, 105 and 107 words — a four-fold spread
with no budget stated anywhere. That is the number this found.

The variance is the real gain. Before, across both runs: 34 to 107. After: 32
to 58.

### What this does not show

The model behind the run is a Claude model. The app talks to an
OpenAI-compatible endpoint, and the parser in `worldstate/parse.py` exists
because weaker free models emit trailing commas and leading `+`. So this shows
the prompt is followable and that the brief supplies the missing information.
It does not show that a weaker production model complies as well. An explicit
framing rule is usually worth *more* on a weaker model, but that is an
expectation, not a measurement.

Two side observations from the same run, both pre-existing behavior working
correctly: the model sent `{"milestones.strongbox_found": false}`, `apply_delta`
refused it ("a milestone is sticky, so only true is accepted"), the refusal
reached the model through `render_refusals`, and the next turn sent `true`. The
Phase 1 persona also held across all six generated turns.

---

# Rewriting a bank written under the old prompt

An adventure played before this change keeps a bank of unnamed, second-person
memories, and those are exactly the rows `memory_top_k` injects into every turn
from now on. Ageing them out only works for an adventure that is still being
played, and only after another 80 memories have been written. So there is a
backfill: `backend/tools/rewrite_memories.py`.

```
cd backend
python -m tools.rewrite_memories                     # what would change
python -m tools.rewrite_memories --write --limit 3   # try three of them
python -m tools.rewrite_memories --write --embed     # the whole backfill
```

Without `--write` it makes no model calls and spends nothing. It reads whichever
database the app reads — `AIDND_DB_PATH`, or `DATABASE_URL` on a hosted deploy.

**In place, not delete-and-regenerate.** The obvious alternative is to drop the
bank and rewind `cursors.MEMORY`, letting the post-turn pass write it again.
That loses everything the row carries besides its text: whether it is pinned,
how often it has been retrieved, and the node it hangs off, which is what makes
a fork inherit the right memories and no others. It would also trickle the bank
back at `MAX_MEMORIES_PER_RUN` per turn, so an adventure nobody is playing would
never recover. Rewriting `text` keeps the row and costs one call per memory.

**One prompt assembly, not two.** `memorybank.summarize_block` is now the single
place a memory prompt is built, and both the post-turn pass and the tool call
it. A backfill that assembled its own prompt would be writing memories with a
prompt that never shipped, and nothing would report the difference.

**Reading the block back is the one genuinely new part.** A memory records
`source_start`, `source_end` and `branch_id`, and `memorybank.source_block`
turns those back into actions. The subtlety is the branch: the read has to use
the lineage of *the branch the memory was written on*, not the branch the
adventure is playing now. After a fork, the same depths hold different actions
on each side, so a read through the adventure's current path would summarize the
wrong story and say nothing about it. `test_memory_rewrite.py` builds that fork
and holds the rule.

**What it will not touch:**

- A memory with no source range — hand-written, or migrated by 62 from before
  memories had coordinates. There is no block to rewrite it from, and the player
  may have typed it.
- A memory whose actions have since been deleted.
- An adventure whose owner has no API key in Settings, because summarization
  spends the user's own key by construction and never the shared demo key.
  `--api-key`, `--model` and `--endpoint` override that — the last of these
  points a run at `tools/claude_shim.py`, so a backfill can spend a Claude
  subscription instead of API credit.

**The vector is cleared for every memory it rewrites**, because the stored one
describes wording that no longer exists. That takes the memory out of the ranked
bank until something embeds the new text: the app's own post-turn pass does it
`MAX_EMBED_BATCH` at a time, or `--embed` does it in the run. Re-embedding always
uses the owner's own embedding model and endpoint, never `--endpoint`, because a
vector is only meaningful against the vectors it is ranked beside.

**Stop the app before running with `--embed`.** A running process caches vectors
by memory id and expects to be the only writer (`memorybank._vector_cache`), so
a vector written from outside it can sit behind a stale cached copy until it
restarts. Clearing alone is safe at any time: an unembedded memory leaves the
catalogue, which is what the cache invalidates on.

## Running it against the hosted deploy

Two things make production different from a local database, and both are easy
to get wrong quietly.

**It holds other people's stories, and each adventure is summarized with its
owner's key.** An unfiltered `--write` would spend other people's money on
memories they did not ask to have rewritten. `--email` restricts a run to named
accounts and `--adventure` to single adventures; the dry run costs nothing and
prints the owner of each. Guests have no email and are reachable only by id,
which is the right amount of friction for rewriting a stranger's bank.

**The stored API keys are encrypted with `AIDND_SECRET_KEY`.** Render generates
that value and holds it for the web service, so a run from a checkout has to
carry the same one. With a different secret, `decrypt_secret` returns "" rather
than failing, and every adventure is reported as having no key — a run that
looks like it worked and did nothing.

```
AIDND_DATABASE_URL=<the Neon URL from the Render dashboard> \
AIDND_SECRET_KEY=<the value the web service has> \
    python -m tools.rewrite_memories --email you@example.com
```

Run it from a checkout rather than from a shell on Render. The image copies
`backend/app` alone, so `tools/` is not on the box, and the free plan has no
shell anyway. The database is the same one either way.

Two smaller notes for that environment. The Neon URL to use is the direct
endpoint, not `-pooler`, for the same reason the sizing queries in STATUS use
it. And an adventure owned by a visitor playing on the shared demo key is
skipped, because summarization has never spent that key.

**The story summary is not rewritten.** It is one text per adventure rather than
a bank, and `_update_story_summary` hands the model the whole of it and asks for
an updated version under the new framing rule, so the next scheduled update
should carry it over to third person by itself. If it does not, that is a
separate and much smaller fix than this one.
