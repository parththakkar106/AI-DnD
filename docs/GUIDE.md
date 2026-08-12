# AI D&D — design notes

How the engine works and why it is built this way. The README covers what the project does
and how to run it; this covers the reasoning behind the parts that had a real choice in
them.

Part 1 is the AI layer, which is where most of the design effort went. Parts 2 and 3 are
what makes it a service rather than a demo. Part 4 is the web plumbing, kept short.

---

## Contents

- [Part 0 — Orientation](#part-0--orientation)
- [Part 1 — The AI layer](#part-1--the-ai-layer)
  - [1.1 The turn pipeline](#11-the-turn-pipeline)
  - [1.2 Context assembly is a budget problem](#12-context-assembly-is-a-budget-problem)
  - [1.3 World state: the AI proposes, Python referees](#13-world-state-the-ai-proposes-python-referees)
  - [1.4 Output length, by measurement](#14-output-length-by-measurement)
  - [1.5 The memory bank](#15-the-memory-bank)
  - [1.6 Streaming](#16-streaming)
  - [1.7 The scripting sandbox](#17-the-scripting-sandbox)
  - [1.8 Why there is no agent framework](#18-why-there-is-no-agent-framework)
- [Part 2 — Data and correctness](#part-2--data-and-correctness)
- [Part 3 — Production concerns](#part-3--production-concerns)
- [Part 4 — The web plumbing, briefly](#part-4--the-web-plumbing-briefly)
- [Part 5 — Measured results and known limitations](#part-5--measured-results-and-known-limitations)

---

# Part 0 — Orientation

## What the thing is

An AI Dungeon clone. You write a scenario, then play an open-ended text adventure where a
language model narrates the world. You type "I open the door", the model writes what
happens next, and it remembers what came before.

Three things make it more than a chat wrapper:

1. **A context engine.** The model has a limited input window. The app decides, every
   single turn, which pieces of the story get to be in the prompt and which get dropped.
2. **A world-state engine.** The scenario declares stats (`hp`, `trust`, `day`). The model
   proposes changes to them each turn; a Python engine decides what actually sticks.
3. **A scripting sandbox.** Real AI Dungeon JavaScript scripts import and run, inside an
   embedded QuickJS interpreter.

Runs locally against Ollama for free, or hosted against any OpenAI-compatible endpoint.

## The stack, and what each part is doing

| Piece | What it does here |
|---|---|
| **FastAPI** (Python) | The HTTP server. Every URL like `/api/adventures/3/actions` maps to a Python function. Also does the SSE streaming. |
| **SQLAlchemy** | The ORM. `Adventure`, `Action`, `Memory` are Python classes; SQLAlchemy turns them into tables and turns attribute access into `SELECT`s. |
| **SQLite / Postgres** | The database. SQLite is a single file on disk (local). Postgres is a server (hosted, on Neon). Same code talks to both. |
| **React** (JavaScript) | The UI. Describes what the screen should look like for a given state; when the state changes it re-renders. |
| **Vite** | The frontend build tool and dev server. Bundles React into plain JS the browser can load. |
| **httpx** | The HTTP client used to call the model endpoint. |
| **tiktoken** | Counts tokens, so the budgeting is arithmetic rather than a guess. |
| **QuickJS** | A small embeddable JavaScript engine, used as a sandbox for user scripts. |

The whole thing is one process in production: FastAPI serves the API *and* the built React
files from the same port.

## The shape of one request

```
you tap "Do"
  → browser sends POST /api/adventures/3/actions   {type:"do", text:"open the door"}
  → FastAPI route: check ownership, rate limit, turn lock
  → assemble the prompt
  → POST to the model endpoint with stream=true
  → tokens come back one at a time
  → each token is forwarded to the browser as a Server-Sent Event
  → React appends it to the screen as it arrives
  → when the stream ends: parse the state block, referee it, save the action
```

---

# Part 1 — The AI layer

## 1.1 The turn pipeline

Everything that happens between "player pressed a button" and "text is on screen".
Source: `backend/app/routers/adventures.py` (`_generate_turn`).

```
player input
  → onInput script hook          (user JS may rewrite or block it)
  → store the player action
  → retrieve memories            (embed recent story, cosine-rank the bank)
  → snapshot script + world state (so undo/retry can roll back)
  → build_context()              (the budget allocator)
  → onModelContext script hook   (user JS may rewrite the whole prompt)
  → snapshot the exact prompt    (for the Insights panel)
  → provider.generate()          (streamed, token by token)
  → onOutput script hook
  → extract the fenced state block, referee the delta, strip it from the prose
  → save the action
  → fire-and-forget: summarize + embed in the background
```

Two design choices are visible in that list before any of the details.

**The prompt is snapshotted, not reconstructed.** Every AI action stores the exact text
that was sent to the model. That's what powers the Insights panel — open any turn and see
each context component, its token cost, and why it was included. It's also what makes
prompt bugs findable. The cost is storage (~74 KB per turn), which turns into a real
performance problem later — see [2.5](#25-the-189x-egress-fix).

**Snapshots happen before the model call, not after.** `state_before` and
`world_state_before` are stapled onto the action *before* the hooks and the delta run.
That's the entire mechanism behind undo and retry actually rewinding rather than just
deleting text.

---

## 1.2 Context assembly is a budget problem

Source: `backend/app/context/builder.py`.

### The problem

The model can only read so much. Say the budget is 8,000 tokens. A 200-turn adventure has
far more story than that. Something has to be dropped, and *what* gets dropped decides
whether the story stays coherent.

### The naive version

Send the last N turns. That breaks in two directions: N turns of short exchanges wastes the
window, and N turns of long ones overflows it. It also throws away the things that matter
most — the premise, the character sheet, the fact that you promised the innkeeper you'd
return.

### What this app does

Split the prompt into **fixed** sections and **elastic** ones.

Fixed (always included, whatever they cost):

| Section | What it is |
|---|---|
| `narrator` | The system prompt — how to write. |
| `world_state_guide` | The stat legend: what each stat means, its range, its bands. |
| `world_state` | Current values of every stat, plus NPCs in scene. |
| `world_state_rule` | How to report changes. |
| `ai_instructions` | Per-adventure steering. |
| `plot_essentials` | AI Dungeon's "Memory" — the premise. |
| `story_summary` | The auto-maintained running summary. |
| `used_memories` | Top-K retrievals from the memory bank. |

Elastic (fit into what's left):

| Section | Rule |
|---|---|
| `world_lore` | Story cards triggered by keywords in recent text. Capped at 40% of the remaining budget. |
| `history` | Story turns, newest first, until the budget runs out. |

The algorithm is three lines of arithmetic:

```python
reserved  = sum of every fixed section + author's note + length hint + reminder
available = max(256, context_token_budget - reserved)
```

Then cards spend up to `available * 0.4`, and history spends `available - cards_used`,
filling backwards from the newest turn.

### The details that are decisions

**Cards are capped at 40% of the elastic budget.** Story cards are triggered by keyword
match, so a scene mentioning six named things could pull in six lore entries and leave no
room for the story itself. The cap makes the failure mode "some lore is missing" instead
of "the model has no idea what just happened". Cards that don't fit are still *reported*
to Insights with `included: false`, so the UI can show the lore that got squeezed out.

**History fills newest-first and stops.** Oldest turns fall out. This is the right
direction because the old material is not actually lost — it has been summarized into
memories and the running summary, which are in the fixed section.

**If even the single newest turn is over budget, it gets hard-truncated** rather than
dropped. A prompt with no story at all would produce nonsense; a prompt with the tail end
of the last turn produces something.

**The author's note is injected 3 actions from the end**, not at the top.
`AUTHORS_NOTE_DEPTH = 3`. Instructions placed near the end of a prompt have more influence
on what comes next than instructions at the top — recency. The author's note is a steering
control ("keep it tense"), so it goes where steering works.

**The world-state reminder goes dead last.** The full emit rule lives up in the system
block, hundreds of tokens away from where the model starts writing. A one-line reminder
occupies the final slot. Same recency logic, applied to the thing most likely to be
forgotten.

**Past AI turns get their state block re-attached.** The state block is stripped from the
text before it's stored, so a replayed history would show the model twenty of its own past
turns that *contain no state block* — which teaches it, by imitation, to stop emitting one.
So `_history_text()` reconstructs the block from the stored delta and re-appends it when
building history. The model sees its own pattern and keeps following it.

### The performance trap hiding in this

Building the context needs the newest ~6,000 tokens of story. The obvious implementation
reads `adventure.actions` — which loads every row of the adventure — and then throws 90%
of it away. At turn 200 that was 839 KB of database reads to use maybe 70 KB, and it grew
every single turn.

`backend/app/context/history.py` fixes it by serving three shapes directly from SQL: a
tail, a slice, and a count. `window_covering()` fetches the newest 32 actions, measures
their real token count, and if that's short of the budget it *projects* how many more it
needs from the average length just measured rather than blindly doubling:

```python
average   = tokens / len(actions)
projected = int(budget / average * 1.15) + 8
```

Each round fetches only what it doesn't already hold, so no row is read twice. Result: the
same turn costs 129 KB instead of 839 KB, and stops growing at around turn 50 — the cost
is bounded by the context budget instead of by the length of the story.

There's a second rule in that module worth naming: **if the actions are already loaded in
memory, slice them instead of querying.** The scripting pipeline hands the whole history to
user scripts (AI Dungeon's API requires it), so on a scripted adventure the rows are
already there — issuing a query beside them would mean paying twice.

---

## 1.3 World state: the AI proposes, Python referees

Source: `backend/app/worldstate/engine.py`, `plan/12-phase-rpg-world-state.md`.

### The problem

You want an RPG layer — hit points, trust, quest progress. Who owns the numbers?

### Three options, and why two lose

**Option A: a deterministic dice engine.** The player types "attack the goblin", the
engine rolls, applies damage, and the model narrates the result. This is what a real RPG
does. It loses here because the action space is unbounded — the player can type anything,
and mapping arbitrary natural language onto a fixed rules system is a harder problem than
the one being solved.

**Option B: the model owns the numbers.** Let it track hp in the prose and trust it. This
fails immediately. Models are bad at arithmetic, worse at remembering a number across
twenty turns, and completely unable to obey their own frequency rules — tell one "only
change this every 5 turns" and it will change it every turn.

**Option C, chosen: the model proposes, the engine disposes.** The model narrates and
appends a JSON delta of what changed. Python validates and clamps it before anything is
stored.

````
narration: "The blade catches your shoulder. Gwen shouts and drags you back."

```state
{"player.hp": -15, "npc.gwen.trust": 5, "milestones.escaped": true}
```
````

The engine then applies, in order:

| Rule | What it stops |
|---|---|
| Path must exist in the schema | Hallucinated stats |
| Value must be the right type | `"a lot"` instead of `-15` |
| Cooldown | Changing a stat more often than the scenario allows |
| Counters can't decrease | The in-game day going backwards |
| `max_delta_per_turn` | Losing 90 hp to a stubbed toe |
| Clamp to `min`/`max` | Negative hp, trust above 100 |
| Milestones are sticky, `true` only | Un-completing a quest |
| Flags are two-way booleans | (Deliberately unrestricted — that's what flags are for) |

Everything it rejects is *reported*, not silently swallowed — the Insights panel shows
applied, clamped and rejected paths per turn, and the chip under each narration shows what
actually changed.

### The reliability mechanism: word bands

A stat can carry **bands**:

```json
"hp": { "min": 0, "max": 100, "initial": 100,
        "bands": [[0,20,"very weak"],[20,40,"hurt"],[40,60,"minor damage"],
                  [60,90,"healthy"],[90,100,"full health"]] }
```

Two things use them. The live state block shows the current band label — `hp 55/100 (minor
damage)` — so the model reads a *word*, not just a number. And the stat guide shows the
whole ladder once per turn, so the model can see the full scale it's reasoning across.

The point: models reason well over semantics and badly over arithmetic. "He's badly hurt,
so a solid hit should take him to very weak" is a judgement a model can make. "55 minus 22
is 33" is one it will get wrong often enough to matter.

### The failure philosophy

Nothing in the world-state engine raises. A malformed delta returns `{}` and the turn
continues. The parser is deliberately tolerant — it strips trailing commas and leading `+`
signs on numbers, both of which weaker free models emit and strict JSON rejects. It accepts
a fence labelled `state`, one labelled `json`, or an unlabelled one, and falls back to a
bare JSON object hugging the end of the text — but only if it parses into something that
looks like a delta, so prose ending in `}` is never eaten.

This matters because the hosted demo runs on free-tier models. A stricter parser would mean
a good model works and a free one doesn't.

### One call, not two

The model narrates *and* emits the delta in a single request. The alternative — narrate,
then a second call to extract structured state — is more reliable per call and costs twice
the latency and twice the rate-limit budget. On the free tier (20 requests/minute) that
would halve the playable turn rate. The tolerant parser plus the terminal reminder was the
cheaper way to buy the same reliability.

---

## 1.4 Output length, by measurement

### The problem

`max_output_tokens` is a hard wall the endpoint enforces mid-sentence. Hit it and whatever
is being written gets cut off. Since the state block is emitted *last*, the state block is
what gets lost. The turn narrates fine and silently records nothing.

### First attempt

Tell the model its budget: *"keep this turn under about N words"*.

### What the measurement showed

Average turn length went from **174 words to 246** — every run longer than every unhinted
run (n=5). Phrased as a budget, the number reads as a *target to fill*. The hint pushed
turns toward the very wall it existed to protect.

### The fix

Phrase it as a ceiling, and say explicitly that a typical turn is much shorter:

```
[Hard limit: this turn must not exceed 412 words. Write only as much as the
moment needs — a typical turn is much shorter. Finish the narration and append
the state block well inside the limit.]
```

Average came back to 170 words, and the state block survived at tight caps.

### And the arithmetic around it

```python
words = int((max_output_tokens - 50) * 0.75 * 0.90)
```

- `- 50` (`LENGTH_HEADROOM`) — tokens held back for the state block itself.
- `* 0.75` (`WORDS_PER_TOKEN`) — models can't count their own tokens, but they do follow a
  word budget. English prose is roughly 0.75 words per token.
- `* 0.90` (`LENGTH_BUFFER`) — a word budget is a suggestion the model overshoots; the cap
  it protects is a hard wall. Aim 10% short so the overshoot lands in slack.
- Below 40 words the hint is dropped entirely — it stops earning its tokens.

---

## 1.5 The memory bank

Source: `backend/app/memorybank.py`.

### The problem

Story history falls out of the context window as the adventure grows. Turn 4 said you
promised the innkeeper you'd return. At turn 90 that's long gone from the prompt — but if
you walk back into the inn, it should come back.

### The three layers

```
raw turns   →  memories        →  story summary
(verbatim)     (every 6 turns)    (rewritten every 15 turns)
                    ↓
               embeddings  →  cosine similarity  →  top-K into the prompt
```

| Layer | Cadence | Purpose |
|---|---|---|
| **Memory** | Every 6 actions, starting at 12 | One or two past-tense sentences of concrete fact. |
| **Story summary** | Every 15 actions | A single ≤250-word overview of the whole plot, rewritten by folding in the new memories. |
| **Retrieval** | Every turn | Embed the last 4 actions (≤600 tokens), cosine-rank the bank, inject the top K (default 5). |

Retrieval is the part that answers the innkeeper problem: the promise is a memory, the
memory has a vector, walking into the inn produces a query vector near it, and it comes
back into the prompt.

### The decisions inside it

**Only *settled* actions get summarized.** The newest action is always held back one turn.
Reason: only the last action can be retried. If a memory summarized the newest action and
the player then retried it, the memory would describe narration that no longer exists — and
because its cursor has already advanced, it would never be regenerated. Holding one action
back costs a turn of latency and makes that state unreachable.

**Cursors only advance on success.** Every AI call in this module is best-effort. If
summarization fails, the function returns and the cursor is unchanged, so the same block is
retried on a later turn. There is no retry loop, no dead-letter queue, no backoff — the
cadence *is* the retry mechanism. Failures are logged to the debug page.

**Summarization is fire-and-forget, in a background task with its own DB session.** The
player's turn is already on screen; making them wait for a summarization call would add a
second or two of latency to every sixth turn for no visible benefit. The task holds strong
references to itself (the event loop only keeps weak ones, so a fire-and-forget task can
otherwise be garbage-collected mid-run) and a per-adventure guard set stops two from
overlapping.

**Pinned memories count toward `top_k`.** Pinned ones are always injected; unpinned ones
fill up to `top_k - len(pinned)`. Without that, 6 pinned memories plus `top_k=5` injects 11
and blows the budget the whole context engine exists to respect.

**A dimension mismatch scores 0.0, it doesn't crash.** If the user changes their embedding
model, old 768-dim vectors get compared against a new 1536-dim query. `zip()` would happily
truncate and score garbage silently. An explicit length check returns 0.0 instead.

**Eviction is LRU-ish, and evicted memories are kept.** Over capacity (default 200), the
least-used, least-recently-used unpinned memories are marked `forgotten` rather than
deleted — so the UI can still show them and you can un-forget one.

**Background calls never spend the shared demo key.** The summarization and embedding
providers are built directly from the user's own settings and never from the demo config,
and their call sites are skipped when the turn is running on the demo key. Summarization is
unmetered background spend; the demo key is server-funded. Both facts together would be a
bill.

---

## 1.6 Streaming

The model produces tokens one at a time. Waiting for the whole reply before showing
anything makes a 20-second generation feel broken.

**Server-Sent Events (SSE)** is the mechanism: an HTTP response that stays open and pushes
`data: {...}` lines as they become available. It's one-directional (server → browser),
which is exactly what's needed here — WebSockets would be a bidirectional connection for a
unidirectional problem.

The chain:

```
model endpoint  --SSE-->  FastAPI  --SSE-->  browser  -->  React state  -->  screen
```

FastAPI reads the provider's stream, and for each chunk yields
`data: {"type":"chunk","text":"..."}`. The frontend reads the response body with a
`ReadableStream` reader, buffers on `\n\n` boundaries, and dispatches each parsed event.

Event types: `player` (the stored player action), `reasoning` (thinking-model traces, which
stream into a separate collapsible panel with their own token budget), `chunk` (story
text), `stopped` (a script blocked the turn), `error`, `done`.

Two production details that only show up when hosted:

- `X-Accel-Buffering: no` — nginx-style reverse proxies buffer responses by default, which
  turns a stream into one big delivery at the end. This header tells them to flush each
  event.
- The security-headers and body-size middlewares are written as **pure ASGI** rather than
  Starlette's `BaseHTTPMiddleware`, because the latter buffers the response body and would
  break streaming.

**The empty-reply case is diagnosed, not reported as "empty".** If a reasoning model
streams thinking but no story text, it spent its whole budget thinking — the error says so
and tells you which three settings to change.

---

## 1.7 The scripting sandbox

Source: `backend/app/scripting/`.

Real AI Dungeon scripts are JavaScript files defining `modifier(text)` and calling it as
the last line, with globals like `state`, `history`, `storyCards`. To be compatible, this
app runs the same contract in an embedded **QuickJS** interpreter.

The safety properties are mostly structural:

| Property | How |
|---|---|
| No filesystem, network, or process access | QuickJS has none by default — nothing was removed, nothing was added |
| Memory cap | 16 MB per run |
| CPU cap | 2 seconds per run |
| No shared state between runs | A fresh `Context` per hook execution |
| A broken script can't break a turn | Every failure comes back as `.error` with text/state/cards unchanged; the pipeline logs it and continues |

Data crosses the boundary as JSON — Python serializes `{state, text, history, storyCards,
info}` in, and the script's results out. There is no object bridge to exploit.

One deliberate bug-compatibility: `addStoryCard` returns the new card's *index*, so the
first card returns `0`, which is falsy, so `if (!addStoryCard(...))` misfires. That's
upstream AI Dungeon's behaviour. It's documented in the code and left alone, because
matching real scripts is the whole point of the feature.

---

## 1.8 Why there is no agent framework

Graph-based agent frameworks (LangGraph and similar) earn their complexity with
**branching, cyclic, multi-step control flow** — a graph of nodes where the path depends on
what the model decides, with loops, retries, tool calls, and persisted state between steps.

This turn pipeline is a **fixed linear sequence with exactly one model call**. There is no
routing decision, no tool selection, no loop. The "graph" is:

```
hook → retrieve → build → hook → call → hook → referee → store
```

Every turn takes that path. Adding a graph framework would mean carrying its state
abstraction, its serialization model, and its debugging surface to express a straight line.

There is also a specific reason a framework's context handling wouldn't fit: **the
budgeting logic is the product.** Buffer-window and summary-memory abstractions are
opinionated about how to fit history into a window. Here the Insights panel exposes each
context component, its token cost, and the trigger word that pulled it in — which means the
assembly has to be explicit and inspectable.

**When it would be the right call:** if the design went toward the two-call version —
narrate, then a separate structured-extraction step, with a retry branch when extraction
fails and a tool-calling path for dice — that is a graph, and hand-rolling it would get
ugly fast.

---

# Part 2 — Data and correctness

## 2.1 The domain model

```
User
 ├─ Scenario   (the template)      ── stat_schema, prompt, memory, author's note
 │    └─ StoryCard, Script
 └─ Adventure  (the playthrough)   ── world_state, script_state, cursors
      ├─ Action  (one story entry) ── text, context_snapshot, variants, state_before
      ├─ StoryCard  (its own copy)
      ├─ Memory     (text, embedding, source_start/end, use_count)
      └─ AdventureScript
```

**The decision that shapes everything: template vs instance.** A scenario declares what
stats *exist*; an adventure holds what they *are* right now. Creating an adventure copies
the scenario's story cards, scripts and plot fields into it, so editing a scenario later
never mutates a game in progress. (There's an explicit opt-in "Update from scenario" flow
for when you *do* want that, which diffs the two and shows you what would change.)

Same reasoning as instantiating a class: shared definition, independent state.

## 2.2 Two coordinate systems, and the bug class they create

The subtlest thing in the codebase.

There are two ways to identify an action:

- **`Action.index`** — a stable number stored on the row. Gaps appear when actions are
  deleted.
- **Position** — where an action sits in the filtered, index-ordered list of *story*
  actions (non-empty text only). Shifts whenever anything before it is deleted.

The memory cursors (`memory_cursor`, `summary_cursor`) are **positions**.
`Memory.source_start` / `source_end` are **`Action.index` values**.

The two spaces are identical until the first deletion, and diverge forever after. Mixing
them means summarization silently skips or duplicates blocks — no crash, no error, just a
memory describing the wrong turns.

Three things hold it together:

1. `history.position_of_index()` is the explicit translation between the spaces, and every
   crossing goes through it.
2. `note_action_removed()` is called *before* a delete: if the removed action sat before a
   cursor, the cursor decrements, so an unsummarized action can't slide into the
   "already covered" range and be skipped forever.
3. One definition of "story action", written twice — `_STORY_TEXT` in SQL and
   `is_story_text()` in Python — with a comment on both saying to keep them in step. The
   SQL version folds newlines and tabs into spaces before `trim()`, because SQLite's and
   Postgres' single-argument `trim()` only strips spaces while Python's `.strip()` also
   drops newlines. An action of nothing but a newline would otherwise count as story text
   in one and not the other, and every cursor after it would be off by one.

## 2.3 Undo and retry that actually rewind

Most implementations of undo delete the last message. That's wrong here, because a turn
mutates three things: the text, the scripting scoreboard (`script_state`), and the RPG
stats (`world_state`).

**The mechanism:** every action carries `state_before` and `world_state_before` — deep
copies taken before the turn's hooks ran. Undo restores from them. Retry rolls back to
them, then regenerates.

**Retry keeps every attempt.** Instead of deleting and replacing, the row survives and each
attempt is appended to `Action.variants`; `variant_index` names the live one. The UI shows
`‹ 2/3 ›` and you can page back to a discarded take. A variant stores only what differs
between attempts — the narration, its reasoning trace, and the state it produced — never
the assembled prompt, which is identical across attempts of the same turn and is by far the
biggest thing in the snapshot.

Three details that are easy to get wrong:

**The row being retried is excluded from its own context.** It's still attached to the
adventure (it holds the variant history), so without `exclude_action_id` the model would be
shown the attempt it's replacing as established story and would write a continuation of it
instead of a replacement.

**A retry reuses the turn's index**, not the next one. Cooldowns are measured in action
indexes, so using `next_index()` would advance the clock the cooldown rules run on and a
retry would quietly unlock stats that should still be on cooldown.

**If the regeneration fails, the rollback is reversed.** `generate_turn` wraps the
generator in a `try/finally`: if it ends without saving — a provider error, an empty reply,
a script `stop`, or the browser hanging up — the previous variant is put back in charge.
Otherwise the state on the server would drift from the text still on the user's screen.

## 2.4 The turn lock

One turn at a time per adventure. Double-clicking "Continue" must not run two generations.

The subtlety: the check has to happen in the **request phase**, not when the SSE generator
first runs. A `StreamingResponse` doesn't start iterating its generator until the response
begins, so a check-inside-the-generator lets two rapid requests both pass before either one
claims the slot. And because sync FastAPI endpoints run in a threadpool, the test-and-set
needs a real `threading.Lock`.

```python
def acquire_turn_lock(adventure_id):        # in the request handler
    with _active_turns_guard:
        if adventure_id in _active_turns:
            raise HTTPException(409, "A turn is already generating…")
        _active_turns.add(adventure_id)

async def with_turn_lock(adventure_id, gen):  # wraps the SSE generator
    try:
        async for event in gen: yield event
    finally:
        _active_turns.discard(adventure_id)
```

In-memory, so it's a single-process guarantee. That's honest for the deployment this
targets — one Render web service. Two processes would need the lock in the database.

## 2.5 The 189x egress fix

**The setup:** `Action.context_snapshot` holds the entire assembled prompt for a turn —
about 74 KB per row, 94% of the database.

**The bug:** every adventure load pulled that column for every action, to read two small
fields out of it (the world-state delta, for the "what changed" chip, and the applied
report). SQLAlchemy loads all columns by default.

**The fix, in three parts:**

1. Move the two small things that *are* needed for every action into their own column
   (`Action.world_delta`).
2. Mark the heavy columns `deferred` — `context_snapshot`, `variants`, `reasoning` — so
   they're only fetched when explicitly asked for.
3. Backfill the new column with dialect-specific server-side SQL, so the old data is
   extracted inside the database and never crosses the wire.

**The result:** one adventure load went from **38.5 MB to 0.20 MB**.

**The part that makes it stick:** `tests/test_egress.py` hooks into SQLAlchemy's
`before_cursor_execute` event, captures every statement the ORM sends, and fails if a bulk
load ever names those columns again. The regression is caught by asserting on the *SQL*,
not on a timing.

One more detail from that test's design: the count query is written as a real
`SELECT count(...)` rather than `query.count()`, because SQLAlchemy's `.count()` wraps the
entity select in a subquery, so the emitted SQL names every column — including the deferred
ones. No bytes come back either way, but the database still has to read them, and a guard
that greps SQL cannot tell the two apart.

There's a companion denormalization for the same reason: `variants` is deferred, so
`variant_count` exists as its own column to answer "how many attempts?" without fetching
them. `set_variants()` is the only function allowed to write `variants`, precisely so the
two can't drift and the pager can't lie about how many takes a turn has.

## 2.6 Migrations, hand-rolled

No Alembic. An append-only list of `(version, SQL)` pairs, with the current version stored
in SQLite's `PRAGMA user_version` or a one-row table on Postgres. 37 versions so far.

- A **fresh** database is created by `Base.metadata.create_all()` (always current) and
  stamped at the latest version — it never replays history.
- An **existing** database runs every migration above its stored version, in order.

Why this and not Alembic: for a single-file SQLite app that a user might have been running
for months, the entire requirement is "add a column, don't lose their data". Alembic's
autogenerate, branching, and down-migrations are machinery for a team with a staging
environment. This is 250 lines and you can read all of it.

The constraint it creates is written at the top of the file: change `models.py` (so fresh
databases are current) *and* append a pair here (so existing ones upgrade). Migrations 2–23
predate Postgres support and use SQLite-only syntax — harmless, because every Postgres
database starts fresh and never replays them, but anything added since must run on both
dialects.

One migration worth reading (#10, repairing duplicate action indexes) uses `UPDATE … FROM`
with a window function rather than a correlated subquery, because SQLite may evaluate a
correlated subquery against partially-updated rows and produce duplicates again while
"repairing" them.

---

# Part 3 — Production concerns

## 3.1 Two modes, one codebase

`AIDND_MULTI_USER` switches the whole app between two personalities:

| | Local (default) | Hosted |
|---|---|---|
| Users | One auto-created "local user" | Guest on first visit, optional account |
| Auth | None — no cookies, no login UI | Signed session cookie |
| Rate limits | Off | On |
| Row caps | Off | On |
| API docs (`/docs`) | On | Off |
| Provider | Whatever Settings points at | User's key, or the shared demo key |

The reasoning: a person running this on their own laptop should never be throttled by their
own app, never see a login screen, and should get the interactive API docs. A hosted
deployment needs all four of those to be the opposite. Rather than two builds, the
differences are gated at each site.

**Guests upgrade in place.** A visitor gets a guest `User` row on first load. Registering
sets `email` and `password_hash` on that *same row* — so every adventure they played as a
guest survives with no re-parenting and no migration step. Three kinds of row share the
users table: local (email NULL, not guest), guest (email NULL, guest), registered (email
set).

## 3.2 The shared demo key

The demo lets people play with no signup and no API key, on a key the server pays for. That
is a spending surface, so it's the most defended code in the project.

`resolve_provider_config()` is the single place the BYOK-vs-demo decision is made, and on
the demo branch it pins **two** things:

- **The model** — to a whitelist. A caller-supplied override or a hand-edited settings row
  can't aim a server-funded key at an expensive model. Anything unrecognised falls back to
  the first whitelisted model.
- **The endpoint** — to the configured demo URL. Otherwise the key could be redirected to a
  URL the user controls and harvested.

Plus a daily per-user turn cap (default 20), checked *before* the player's input is stored
so a capped player doesn't get their message saved with no reply, and counted only after a
successful turn.

There's a defensive `__post_init__` on the config object that raises if a demo config
somehow carries a non-whitelisted model. The comment on it records a real bug: the check
tests `using_demo`, **not** `api_key == DEMO_API_KEY`. Keying on the key value looks
stricter but is wrong — the demo key is an ordinary OpenRouter key, so a user can
legitimately paste that same key into their own settings as BYOK, and then every resolution
raised, 500ing even `GET /auth/me` and taking the whole SPA down. `using_demo` is what
actually means "the server is paying".

Background work (summarization, embeddings) is excluded from the demo key entirely — those
are unmetered calls, and unmetered calls on a server-funded key is a bill.

## 3.3 Secrets

Everything derives from one server-side secret (`AIDND_SECRET_KEY`).

| Thing | Mechanism |
|---|---|
| Passwords | `hashlib.scrypt`, N=2^14, r=8, p=1, per-password salt, constant-time compare. Stdlib, so no extra dependency. |
| Sessions | `v1.<user_id>.<HMAC-SHA256>`, no expiry — long-lived guest sessions are the point. |
| Stored LLM API keys | Fernet (AES) encryption at rest, key derived from the secret, `enc:` prefix so legacy plaintext rows are recognisable and migratable. |

The secret auto-generates into a file next to the database for local installs (zero config),
but **multi-user mode refuses to start without the env var** — with an error message that
explains why and gives you the command to generate one. Hosted filesystems are ephemeral; a
regenerated secret on every deploy would silently log out every user and orphan their stored
API keys.

A rotated secret makes stored keys undecryptable. `decrypt_secret` treats that as "unset"
rather than raising, so the user just re-enters their key instead of hitting a 500.

## 3.4 Abuse guards

| Guard | Value |
|---|---|
| Turn generation | 10 / minute |
| Auth attempts | 10 / 5 min, per IP |
| Guest creation | 30 / 5 min, per IP (each guest is a DB row) |
| Script test runs | 30 / minute (each costs up to 2s CPU) |
| Connection test | 10 / minute (outbound HTTP to a user-supplied URL) |
| Adventures / scenarios / scripts per user | 100 / 200 / 200 |
| Actions per adventure | 5,000 |
| Request body | 2 MB, 20 MB on import endpoints |

Rate limits are keyed per user when one is known (accounts survive IP changes) and per IP
otherwise, in fixed windows held in memory, with a pruning pass so the per-IP dict can't
grow without bound. Import endpoints check bundle list lengths against the same caps live
creation enforces — otherwise the cap is trivially bypassed by uploading a file.

Security headers on every response: `nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: same-origin`, and a CSP allowing exactly what the SPA uses — same-origin
everything, inline styles (React needs them), Google Fonts.

## 3.5 Deployment

One Docker web service on Render, serving the SPA and the API same-origin, with Postgres on
Neon.

The Postgres decision was forced: Render's free tier has no persistent disk, so a SQLite
file wouldn't survive a deploy. The database lives off-box on Neon's free tier.

Two things worth knowing about the free tier:

- The service **sleeps after ~15 minutes idle**, and the first request then takes 30–60s.
- `/api/health` deliberately **doesn't touch the database**, so a keep-warm pinger wakes the
  web service without waking the database. Waking a database around the clock costs far more
  than the cold start is worth.

CI runs the backend tests, the frontend lint and build, and a Docker image build on every
push.

---

# Part 4 — The web plumbing, briefly

For the parts that are just how the web works, not decisions.

**Frontend and backend are two programs.** In development they're two servers — Vite on
5173 serving React, FastAPI on 8000 serving the API — and Vite proxies `/api` to FastAPI so
the browser thinks it's all one origin (which avoids CORS entirely). In production there's
one server: FastAPI serves the built React files as static assets from the same port.

**SPA routing.** React Router handles URLs like `/play/3` in the browser without a round
trip. But if you *reload* that URL, the browser asks the server for `/play/3`, which isn't a
file. So `SPAStaticFiles` catches the 404 and returns `index.html`, letting React take over
and read the URL itself. API routes are matched before the static mount, so they're
unaffected.

**Sessions.** A cookie is a small value the browser stores and automatically attaches to
every request to that site. Here it holds `v1.<user_id>.<signature>`. The server doesn't
store sessions anywhere — it re-verifies the signature on each request, which is why there's
no session table.

**The 401 retry.** If the cookie is missing or stale, any API call returns 401. The frontend
catches that once, calls `/api/auth/me` (which mints a fresh guest session), and retries the
original request. So a returning visitor with an expired cookie never sees an error.

**React, in one paragraph.** A component is a function that returns a description of some
UI. `useState` holds a value; changing it re-renders the component. The streaming turn is
the clearest example: each SSE chunk appends to a state string, React re-renders, and the
text appears to type itself.

---

# Part 5 — Measured results and known limitations

## Measured results

| | |
|---|---|
| Database egress per adventure load | 38.5 MB → **0.20 MB** (~189x) |
| Prompt snapshot size | ~74 KB/turn, 94% of the database |
| Turn read cost at turn 200 | 839 KB → **129 KB**, flat after ~turn 50 |
| Length-hint phrasing | 174 → 246 words phrased as a budget; **170** phrased as a ceiling (n=5) |
| Backend tests | 151, LLM mocked, real QuickJS engine |
| Schema versions | 37 |
| Sandbox limits | 16 MB, 2 s CPU, fresh context per run |
| Context defaults | author's note at depth 3, cards capped at 40% of elastic budget |
| Memory cadence | memory / 6 turns, summary / 15 turns, top-5 retrieval |

Two of the tests encode a performance property rather than a behaviour:
`test_egress.py` asserts on the SQL the ORM emits, and `test_history_window.py` asserts
that the read cost stops growing with story length.

## Known limitations

Deliberate trades for a single-user-first app that also happens to be hosted, listed so
nobody has to discover them the hard way.

- **Single process.** The turn lock, the rate limiter and the summarization task all assume
  one worker. A second worker would need the lock in the database (a row-level advisory
  lock) and the rate limiter in Redis.
- **No vector index.** Retrieval does cosine similarity in Python over the whole bank. Fine
  at the 200-memory cap; at 10,000 it would want pgvector.
- **Prompt snapshots are heavy** even after the egress fix — they're deferred, not smaller.
  Compressing them or expiring old ones is the real fix.
- **In-memory rate-limit windows reset on restart**, so a restart grants a brief extra
  allowance.
- **Background summarization is a fire-and-forget asyncio task**, so it does not survive a
  restart. At real load it belongs in a queue.
- **The demo key depends on a free-tier provider's daily cap**, which the app can only
  detect after the fact by string-matching the 429 body.

## Cleanup backlog

`docs/self-review.md` carries an open list of non-bugs — reuse, simplification and
efficiency items — kept deliberately separate from the correctness list, which is empty.
The largest ones:

- `Section.tokens` is uncached, so the context gets tokenized two or three times a turn.
- `onModelContext` flattens system and story into one string before handing it to user
  scripts; if a script modifies it, the structure is gone and everything ships as user
  content. Passing structure through the hook would be better but would break AI Dungeon
  compatibility, which is the point of the feature.
- The import endpoints hand-coerce raw dicts instead of using Pydantic bundle schemas.
- `Action` has no `UniqueConstraint('adventure_id', 'index')`; index allocation is ad-hoc
  per writer, and a database constraint would make the turn-lock race impossible rather
  than merely fixed.

---

*Source: [github.com/parththakkar106/AI-DnD](https://github.com/parththakkar106/AI-DnD) ·
[Project page](https://parththakkar106.github.io/AI-DnD/)*
