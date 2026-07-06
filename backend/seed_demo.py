"""Seed a demo scenario + adventure with sample scripts for testing.

Run from the backend folder:  .venv\\Scripts\\python.exe seed_demo.py
Safe to rerun: it deletes any previous rows titled "[Demo] ..." first.

Phase 8: the scenario is seeded as PUBLIC (user_id NULL + is_public), so in
multi-user mode every guest sees it as read-only starter content. The sample
adventure and script-library copies belong to the local user (only relevant
on single-user installs).
"""

from app import auth, models, migrations
from app.database import SessionLocal, engine

# create_all + user_version stamp; plain create_all would leave a fresh DB at
# version 0 and the server would replay every ALTER TABLE migration on start.
migrations.bootstrap(engine)

DEMO_PREFIX = "[Demo]"

# ---------------------------------------------------------------------------
# Sample scripts — AI Dungeon contract: define modifier(text), call it last.
# ---------------------------------------------------------------------------

DICE_ROLLER = dict(
    name=f"{DEMO_PREFIX} Dice Roller",
    description=(
        "Input hook + shared library. Type '!roll 2d6' or '!roll d20' in a Do/Say/"
        "Story action and the command is replaced with the rolled result."
    ),
    library_js="""\
// Shared library: available to every hook of this script.
function rollDice(count, sides) {
  var total = 0, rolls = [];
  for (var i = 0; i < count; i++) {
    var r = Math.floor(Math.random() * sides) + 1;
    rolls.push(r);
    total += r;
  }
  return { total: total, rolls: rolls };
}
""",
    input_js="""\
const modifier = (text) => {
  // Replace every "!roll NdS" (N optional) with the roll result.
  var out = text.replace(/!roll\\s+(\\d*)d(\\d+)/gi, function (m, n, s) {
    var count = parseInt(n || "1", 10);
    var sides = parseInt(s, 10);
    var res = rollDice(count, sides);
    log("Rolled " + count + "d" + sides + ": [" + res.rolls.join(", ") + "] = " + res.total);
    return "(rolled " + count + "d" + sides + ": " + res.total + ")";
  });
  return { text: out };
};
modifier(text);
""",
)

TURN_TRACKER = dict(
    name=f"{DEMO_PREFIX} Turn & HP Tracker",
    description=(
        "Demonstrates persistent state. Counts turns; '!hp -3' or '!hp +5' in input "
        "adjusts HP (starts at 20). Current stats appear in state.message."
    ),
    input_js="""\
const modifier = (text) => {
  if (state.hp === undefined) state.hp = 20;
  state.turns = (state.turns || 0) + 1;

  var out = text.replace(/!hp\\s*([+-]\\d+)/gi, function (m, delta) {
    state.hp += parseInt(delta, 10);
    return "";
  });

  state.message = "Turn " + state.turns + " | HP: " + state.hp + "/20";
  log(state.message);

  if (state.hp <= 0) {
    // stop:true ends the turn before the AI is called.
    return { text: out + "\\n\\nYou have fallen. (HP reached 0 — turn stopped by script.)", stop: true };
  }
  return { text: out };
};
modifier(text);
""",
)

CONTEXT_INSPECTOR = dict(
    name=f"{DEMO_PREFIX} Context Inspector",
    description=(
        "Context hook: logs the size of the assembled context each turn and appends "
        "a style directive. Check the logs/context in the action's context snapshot."
    ),
    context_js="""\
const modifier = (text) => {
  log("Context size: " + text.length + " chars, actions so far: " + info.actionCount
      + ", story cards: " + storyCards.length);
  // Anything returned here replaces what is sent to the model.
  return { text: text + "\\n[Style: keep the response under three paragraphs.]" };
};
modifier(text);
""",
)

OUTPUT_POLISH = dict(
    name=f"{DEMO_PREFIX} Output Polish + Card Discovery",
    description=(
        "Output hook: trims a trailing incomplete sentence from the AI reply, and "
        "auto-creates a story card the first time the ghost Vharos is mentioned."
    ),
    output_js="""\
const modifier = (text) => {
  var out = text;

  // Drop a trailing sentence fragment (no ending punctuation).
  var m = out.match(/^([\\s\\S]*[.!?"'\\u2026])[^.!?"'\\u2026]*$/);
  if (m && m[1].length > 40) {
    if (m[1].length < out.length) log("Trimmed incomplete final sentence.");
    out = m[1];
  }

  // Demonstrate script-created story cards.
  if (/vharos/i.test(out)) {
    var added = addStoryCard(
      "Vharos, ghost, spirit",
      "Vharos was the crypt's architect, now a restless ghost bound to the amulet he was buried with. He speaks in echoes and cannot lie.",
      "character"
    );
    if (added !== false) log("Vharos mentioned — story card created.");
  }

  return { text: out };
};
modifier(text);
""",
)

SCRIPTS = [DICE_ROLLER, TURN_TRACKER, CONTEXT_INSPECTOR, OUTPUT_POLISH]

# ---------------------------------------------------------------------------
# Scenario content
# ---------------------------------------------------------------------------

SCENARIO = dict(
    title=f"{DEMO_PREFIX} The Sunken Crypt of Vharos",
    description=(
        "A short dungeon-crawl demo scenario with story cards and one of each "
        "script hook, for testing the app end to end."
    ),
    prompt=(
        "Rain hammers the moors as you descend the moss-slick steps beneath the "
        "ruined chapel. Your torch gutters in the stale air. Below, the Sunken "
        "Crypt of Vharos waits — its iron door ajar, as if someone (or something) "
        "expected you. Mira's warning rings in your ears: bring back the Ember "
        "Amulet before nightfall, or the village of Hollowmere burns.\n\n"
        "You stand before the iron door, water pooling around your boots."
    ),
    memory=(
        "The player is an adventurer hired by Mira, blacksmith of Hollowmere, to "
        "retrieve the Ember Amulet from the Sunken Crypt of Vharos before "
        "nightfall. The crypt is flooded, dark, and haunted. Tone: classic D&D "
        "dungeon crawl, dangerous but fair."
    ),
    authors_note="Keep scenes tense and grounded; offer clear choices; consequences matter.",
    ai_instructions=(
        "Write in second person, present tense. End each response at a moment "
        "where the player can act."
    ),
    tags="demo, dungeon, fantasy, short",
)

STORY_CARDS = [
    dict(
        type="character",
        name="Mira the Blacksmith",
        keys="Mira, blacksmith",
        entry=(
            "Mira is Hollowmere's blacksmith: broad-shouldered, gray-braided, "
            "practical. She hired the player and paid half up front. She knows "
            "more about the crypt than she has admitted."
        ),
        notes="Secretly a descendant of Vharos.",
    ),
    dict(
        type="location",
        name="The Sunken Crypt",
        keys="crypt, tomb, Vharos",
        entry=(
            "A flooded burial complex beneath a ruined chapel. Knee-deep black "
            "water, collapsed pillars, and phosphorescent moss. Three chambers: "
            "the Drowned Hall, the Ossuary, and the sealed Reliquary where the "
            "Ember Amulet rests."
        ),
        notes="",
    ),
    dict(
        type="item",
        name="The Ember Amulet",
        keys="amulet, ember",
        entry=(
            "A fist-sized garnet on a bronze chain that glows like a coal. It "
            "keeps Hollowmere's protective hearth-ward burning. Touching it bare-"
            "handed brands the flesh but does no lasting harm."
        ),
        notes="",
    ),
]

# ---------------------------------------------------------------------------

db = SessionLocal()
try:
    owner = auth.local_user(db)

    # Remove earlier demo rows so reruns stay clean.
    for adv in db.query(models.Adventure).filter(models.Adventure.title.like(f"{DEMO_PREFIX}%")):
        db.delete(adv)
    for sc in db.query(models.Scenario).filter(models.Scenario.title.like(f"{DEMO_PREFIX}%")):
        db.delete(sc)
    for s in db.query(models.Script).filter(models.Script.name.like(f"{DEMO_PREFIX}%")):
        db.delete(s)
    db.commit()

    # Scripts attached to the public scenario are unowned (user_id NULL) so
    # they ship with it everywhere; they're copied into each adventure at
    # creation, so they never need to appear in anyone's script library.
    scripts = [models.Script(**s) for s in SCRIPTS]
    db.add_all(scripts)

    # Scenario with cards and scripts attached — public starter content.
    scenario = models.Scenario(**SCENARIO, is_public=True)
    scenario.scripts = scripts
    db.add(scenario)
    db.flush()
    for card in STORY_CARDS:
        db.add(models.StoryCard(scenario_id=scenario.id, **card))

    # Adventure created from the scenario, mirroring POST /api/adventures
    adventure = models.Adventure(
        user_id=owner.id,
        scenario_id=scenario.id,
        title=scenario.title,
        memory=scenario.memory,
        authors_note=scenario.authors_note,
        ai_instructions=scenario.ai_instructions,
    )
    db.add(adventure)
    db.flush()
    for card in STORY_CARDS:
        db.add(models.StoryCard(adventure_id=adventure.id, **card))
    for position, s in enumerate(SCRIPTS):
        db.add(models.AdventureScript(adventure_id=adventure.id, position=position, **s))
    db.add(models.Action(adventure_id=adventure.id, index=0, type="start", text=scenario.prompt))
    db.commit()

    print(f"Scenario  id={scenario.id}: {scenario.title}")
    print(f"Adventure id={adventure.id}: {adventure.title}")
    print(f"Scripts: {', '.join(s.name for s in scripts)}")
finally:
    db.close()
