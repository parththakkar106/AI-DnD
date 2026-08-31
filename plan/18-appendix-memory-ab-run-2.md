# Appendix: the memory-prompt A/B, run 2 — a fresh story

An independent replication of `plan/18-appendix-memory-ab-run.md`, produced by
`backend/tools/memory_ab.py` through `tools/claude_shim.py` after
`MEMORY_MAX_WORDS` was added. The story is **newly generated**, not the one run 1
used, so this tests the prompts against different prose rather than re-scoring
the same text.

## What replicated, and what did not

| | run 1 before | run 2 before | run 1 after | run 2 after |
|---|---|---|---|---|
| memory 1 | 34 w, "you" | 89 w, "the player" | 72 w, named | 54 w, named |
| memory 2 | 105 w, "the player" | 107 w, "the player" | 68 w, named | 55 w, named |

**Naming replicated cleanly.** Four control memories across two runs, and not one
of them names the protagonist. Four treatment memories, and all four do. That is
the change working, and it is the finding to rely on.

**Length replicated, and the ceiling holds.** The control ran 34, 89, 105, 107
words — a four-fold spread with no stated budget. The treatment after
`MEMORY_MAX_WORDS` ran 54 and 55.

**The person-drift did not recur, and the claim about it should be read
narrowly.** Run 1 produced two control memories in two different persons — "You
crept low" and "The player asked Gwen" — which is exactly the reported
complaint. Run 2's controls were both "the player", consistently. So drifting
between *second* and *third* person is a real thing a model does, observed once,
not something it does every time. What is consistent across both runs is that
the control never reaches for the character's name, because it has never been
told one.

---

*Everything below is the raw run, unedited.*

One story, generated through `build_context` against `sonnet` at `http://127.0.0.1:8787/v1`. Both prompts then summarize the same blocks, so the prompt is the only variable. The control is `MEMORY_SYSTEM_PROMPT` as of `9cdcb55`.

| memory | arm | words | framing |
|---|---|---|---|
| 1 | before | 89 | "the player" |
| 1 | after | 54 | named |
| 2 | before | 107 | "the player" |
| 2 | after | 55 | named |

## Memory 1

**Before:** The player and Gwen scouted a bandit camp at dawn, agreeing on a quiet approach with Gwen circling the right flank (targeting the spear-carrying guard) while the player advanced from the left; the strongbox sat hidden under an oiled tarp in the camp's center. The player searched an empty bedroll undetected, taking a belt knife, a handful of copper and silver coins, and a charcoal-marked parchment map of the camp, then began moving low toward the strongbox while the guard remained oblivious and Gwen stayed ready with her bow.

**After:** Kaelen and Gwen infiltrated the bandit camp at dawn, splitting flanks with Gwen covering right from the tree line, bow ready, while Kaelen approached left. Kaelen looted a bedroll near an overturned cart, taking a belt knife, coins, and a charcoal map of the camp, then began moving low toward the tarp-covered strongbox undetected.

## Memory 2

**Before:** The player crept through the bandit camp toward a tarp-covered iron strongbox, reflexively reached to pull Gwen down beside him despite her being forty feet away at the tree line, and the resulting half-second lapse nearly caused a second bandit (a wood-gatherer) to spot him at the woodpile; Gwen almost loosed an arrow but held on his signal, and the bandit left unaware. The player then waited for the spearman to turn toward the fire and moved undetected to crouch beside the strongbox, where Gwen signaled approval and pointed to the simple hasp-and-padlock lock before he asked her whether she still trusted his read on the situation.

**After:** Kaelen infiltrated the bandit camp toward the tarp-covered strongbox while Gwen covered him from the tree line; he instinctively reached to pull her down beside him but she was forty feet away, nearly exposing him to an unnoticed second bandit gathering wood, which frayed Gwen's trust before he safely reached the strongbox's simple hasp-and-padlock lock.

## The story both arms summarized

**start:** Dawn breaks grey over the treeline as you and Gwen crouch at the edge of the bandit camp. Smoke curls from a dying fire; three bedrolls lie empty. Somewhere ahead, the stolen caravan strongbox waits.

Gwen checks her bowstring and looks to you. "Quiet, or loud?"

**do:** go quiet and signal Gwen to circle around the right flank

**ai:** You lean close, voice barely a breath. "Quiet. Take the right flank, stay in the tree line — I'll come at them from the left when you're set. Don't loose until I move first, unless someone spots you."

Gwen's mouth quirks, half approval, half habit. "Try not to trip over anything this time." She melts sideways into the brush without a sound, her passage barely stirring a fern, and angles wide around the camp's edge, bow already loose in her hand.

You give her a slow count — enough time to get into position — watching the camp in the meantime. A lone bandit paces near the fire, spear resting on his shoulder, yawning like the night watch had been long and dull. Beyond him, tents sag in a loose ring, and past those, half-hidden under an oiled tarp, a squat iron-banded shape sits guarded by nothing more than habit and complacency. The strongbox.

You catch a flicker of movement at the tree line opposite — Gwen, settling into a crouch behind a fallen log, bow half-drawn, patient as stone. She lifts two fingers, then points toward the spear-carrier: hers if it goes loud.

You ease forward through the underbrush, testing each footfall before committing your weight, keeping the dying fire's smoke between you and the pacing guard's sightline. Your pulse is steady, senses sharpened — the cool mana still humming faintly under your skin from old habit, ready if you need it. No alarm yet. No eyes turned your way. The camp sleeps in its false security, and for now, the quiet is yours to keep or break.

You reach the shadow of an overturned cart at the camp's rim, close enough now to hear the guard mutter something to himself. Gwen is in position, arrow nocked, waiting on your signal.

**do:** search the nearest bedroll for anything useful

**ai:** You glance toward the nearest bedroll — one of the three empty ones, rumpled blankets still holding the shape of whoever slept there, maybe ten feet from the cart's shadow. The guard's back is turned, spear-shoulder rotating slowly as he paces his short loop. You judge the distance, the timing, and decide it's worth the risk.

You slip low across the open ground, boots finding bare dirt instead of dry leaves, and drop to a crouch beside the bedroll. Your fingers move quick and quiet through the folded blanket, a stiff leather pack shoved half-underneath. Coins clink faintly — you cup your palm over the pouch before it can rattle again — and your hand closes on a stubby belt knife, a handful of copper and silver, and a folded scrap of parchment that might be a rough map of the camp's layout, marks scratched in charcoal.

The guard's pacing falters. He half-turns, spear dipping, some animal instinct prickling at the edge of his awareness. You freeze mid-motion, breath held, willing yourself into the cart's shadow. Across the clearing, you catch the barest shift from Gwen — her draw hand tightening, ready to loose if this goes wrong.

The moment stretches. Then the guard grunts, spits into the dying fire, and resumes his slow circuit, oblivious. You let your breath go slow and silent, tucking the coin pouch and parchment into your jerkin, the knife into your belt beside your own.

It's not much, but it's something — coin for later, and maybe a map worth reading twice before you commit to a route through this camp. You ease back from the bedroll, retreating to the cart's cover, eyes flicking to Gwen. She gives the faintest nod: *still clear, still yours to call.*

The guard yawns again, spear drooping, utterly unaware how close he came to dying loud. The strongbox waits under its tarp, patient as ever. The quiet is still yours — for now.

**do:** move toward the strongbox, keeping low

**ai:** You keep to the cart's shadow a moment longer, judging the guard's rhythm — three steps out, a pause, three steps back, spear dipping low on the turn. When his back rotates fully away, you move.

Low and fast, you cross the gap between the cart and the nearest tent, one hand brushing the canvas to steady yourself rather than grip it — no rustle, no tug on the guy-lines. You thread between two more tents, close enough now to smell tallow and old woodsmoke, and drop into a crouch behind a stack of crated supplies not ten feet from the tarp-covered strongbox.

From here you can see it properly: iron bands dark with rust, a heavy lock through a hasp, the oiled tarp thrown over it more to keep off dew than to hide it. Careless. Whoever's running this camp trusts their numbers more than their diligence.

You risk a glance back toward the tree line. Gwen hasn't moved, but her head has tracked your progress the whole way, bow still half-drawn, patient. She catches your eye and gives the barest tilt of her chin toward the strongbox — *go on, I've got you* — before her gaze slides back to the pacing guard, who is still oblivious, still yawning his way through a boring watch.

Closer now, you can make out voices from one of the tents — low, unhurried, two men talking about nothing in particular. Not alarmed. Not aware. The camp's quiet holds, fragile as it is, and the strongbox sits within reach, close enough that you could touch the tarp if you stretched out a hand.

Your fingers itch to check the lock, to see if it needs force or finesse — but that's a decision for the next breath, not this one. For now you crouch in the crate-shadow, heartbeat steady, senses stretched wide across the camp: the pacing guard, the murmuring tent, Gwen's stillness at your back. Nothing has broken yet. The strongbox is yours to reach for.

**do:** grab her wrist and pull her down behind the woodpile

**ai:** Your hand moves on instinct before thought catches up — reaching sideways for Gwen's wrist to haul her down out of sight — but your fingers close on nothing but cool air. She isn't beside you. She's forty feet off across the clearing, still nested behind her log at the tree line, exactly where you left her.

The mistake costs you half a second, and half a second is enough. Boots crunch on the far side of the crates — close, closer than the pacing guard should be. A second bandit, one you hadn't accounted for, rounds the supply stack with a coil of rope over one shoulder, heading for the woodpile stacked just past your cover.

You drop flat behind the crates instead, breath locked in your chest, willing your travel-stained leathers to melt into the shadow. The bandit hums tunelessly, kneels at the woodpile — near enough you could count the notches in his boot heel — and starts loading split logs into his arms, utterly unaware he's sharing six feet of dirt with an exiled ranger and a very recent, very foolish reflex.

Across the camp, Gwen has gone rigid, bow lifting a fraction — she saw the second man appear, saw you freeze, and for one taut moment she's clearly bracing to put an arrow in his throat if he so much as glances your way. You catch her eye through the crate-gap and give the smallest shake of your head. *Not yet. Hold.*

The wood-gatherer straightens, arms full, and ambles off toward the fire without ever knowing how close he came. Your heart hammers against your ribs. Gwen eases her draw, but her jaw stays tight, and even at this distance you can feel the flicker of irritation under her patience — not at the bandit, at you, for a half-second of carelessness that nearly bought you both a fight you hadn't chosen.

The strongbox still waits behind its tarp, closer now than ever. But the quiet feels thinner than it did a moment ago.

**do:** wait for the bandit to turn, then move

**ai:** You settle back against the crates, still as the wood itself, and let your breathing fall into the same slow rhythm as the camp around you. The wood-gatherer's footsteps fade toward the fire pit, and a moment later you hear the clatter of logs dropped beside the flames, a muttered word to the pacing spearman, then silence again but for the crackle of embers catching new fuel.

You count another ten heartbeats before you risk a look. The spearman has stopped to warm his hands, back fully to you now, attention on the fire instead of his rounds. No one else stirs between you and the tarp.

This is the gap.

You rise from the crate-shadow in one smooth motion, low and quick, closing the last stretch of open ground between the crates and the strongbox in a handful of silent strides. The tarp is close enough to touch now — you drop to a crouch beside it, back pressed to the iron-banded corner, and finally let yourself breathe.

Across the clearing, Gwen's stance has eased a fraction. She tracked every step of that crossing, and though her jaw is still set from the wood-gatherer's near miss, there's the faintest approval in how she holds herself now — the tension of someone watching a mistake corrected rather than repeated. She lifts two fingers again, then points toward the lock hasp: *your move.*

The lock itself is heavy, old iron, a simple hasp-and-padlock rather than anything clever — the kind of thing built to deter opportunists, not a trained hand. You could pick it quiet, or put a boot to it loud. The voices still murmur from the nearby tent, unconcerned. The spearman still warms his hands at the fire. The quiet holds, thinner than before, but yours.

**do:** ask her whether she still trusts my read on this

**ai:** You crouch there a moment longer, one hand still braced on the strongbox's cold iron corner, and risk a glance across the clearing. Gwen's eyes are already on you — they usually are — and you mouth more than whisper the question, tilting your head just enough to ask it: *still with me on this?*

For a beat she doesn't answer, just watches the spearman warming his hands, the murmuring tent, the whole fragile balance of the camp's false calm. Then she gives a small, economical shrug, the kind that means *mostly*, and taps two fingers against her bow stave — a gesture you've learned means *your instincts, not your luck.* It isn't a full yes. The wood-gatherer's near miss still sits behind her eyes, and you can read it plainly enough: she'll follow your read, but she's watching it closer than she was an hour ago.

Her hand shifts, index finger drawing a small circle in the air, then pointing at the lock — *pick it, don't break it. Loud is still your last resort, not your first.* It's about as much reassurance as you're going to get from a ranger who's had one scare too many this morning, and honestly, it's fair. You nod once, slow, so she can see you take it.

The spearman shifts his weight at the fire but doesn't turn. The tent's murmur continues, unbroken. Whatever trust frayed at the woodpile hasn't snapped — it's just drawn tighter, watchful, waiting to see what you do with the lock in front of you. The strongbox sits solid and patient at your back, and beyond the thin canvas walls, the camp sleeps on in its dangerous, borrowed quiet.

You turn back to the hasp, letting your fingers find the mechanism, senses still split between the metal under your hands and the fire-lit shape of the spearman thirty feet off. The moment is yours to use well — or not.

