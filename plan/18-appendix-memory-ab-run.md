# Appendix: the memory-prompt A/B, run 2026-08-31

The evidence behind `plan/18-persona-and-memory-quality.md`. Reproduce it with:

    python tools/claude_shim.py --port 8787 &
    python tools/memory_ab.py --out ab.md

One story, generated a turn at a time through the app's own `build_context`.
Both prompts then summarize the **same** blocks, so the story is held constant
and the prompt is the only variable. Every call is a separate request, so
neither arm sees the other's output, and the model is never told what is being
measured. The control is `MEMORY_SYSTEM_PROMPT` as of commit `9cdcb55`, read
from git rather than pasted, so it cannot drift from what shipped.

The model was a Claude model, reached through `tools/claude_shim.py`. See
"What this does not show" in plan/18 before generalising from it.

## What it showed

| memory | arm | words | how it names the protagonist |
|---|---|---|---|
| 1 | before | 34 | second person — "**You** crept low…" |
| 1 | after | 72 | "**Kaelen** and Gwen crouched…" |
| 2 | before | 105 | "**The player** asked Gwen to…" |
| 2 | after | 68 | "**Kaelen** and Gwen infiltrated…" |

Two things came out of this, and only one of them was the thing being tested.

**The reported fault reproduced.** Two consecutive memories, from one story,
written minutes apart, in two different persons. That is the complaint, observed
rather than argued from the prompt text. Both after-memories name Kaelen.

**A fault nobody had noticed.** "1-2 plain sentences" is not a length: 34 words
for one block, 105 for the next. `memory_top_k` injects five memories every
turn, so the bank's running cost was set by a number never stated.
`MEMORY_MAX_WORDS = 50` now states it. Re-run over this same story, the two
blocks came back at **32 and 58 words**, still named, still third person, still
carrying the camp map, the strongbox behind the second tent, and the strap
frayed near through:

> Kaelen crept low toward the strongbox, now known from the map to be behind
> the second tent, with the dozing watchman still unaware and Gwen holding her
> covering position on the flank.

**A likely accuracy win, at n=1.** The player's move was `grab her wrist and
pull her down behind the woodpile`. The before-memory recorded "The player asked
Gwen to grab her wrist and pull her down", inverting who acted. This is the
failure the brief predicts — with no cast there is nothing to say whose wrist
"her wrist" is — but one sample is an observation, not a result.

---

*Everything below is the raw run, unedited.*

## Memory 1

**Before:** You crept low through the mist toward the second tent, following the map's mark toward the strongbox while Gwen held her flanking position, the sleeping watchman still dozing on his spear near the lean-to.

**After:** Kaelen and Gwen crouched at dawn outside the bandit camp; Kaelen signaled Gwen to circle the right flank while a drowsy watchman kept post near the lean-to, and Gwen took position unseen near the strongbox tent. Kaelen searched an abandoned bedroll, finding a whetstone, a few copper coins, dried meat, and a charcoal-scrawled camp map revealing the stolen strongbox was hidden behind the second tent, then began moving low toward that location.

## Memory 2

**Before:** The player asked Gwen to grab her wrist and pull her down behind the woodpile just before a second bandit—half-dressed, carrying a piss-bucket—stumbled out, relieved himself, and returned to bed in the first tent; Gwen lingered a beat before freeing her wrist and murmured "Good eyes." After waiting five heartbeats to confirm the bandit was asleep, the player and Gwen returned to the strongbox tent, where they found the old iron caravan lock too sturdy to pick and instead began testing the straps, discovering one frayed near through at the fold; the player then asked Gwen whether she still trusted their read on the situation.

**After:** Kaelen and Gwen infiltrated the bandit camp to reclaim a stolen strongbox, sheltering behind a woodpile while a second bandit relieved himself nearby before returning to sleep; afterward Kaelen returned to the strongbox tent, found its lock too sturdy to force by hand but noticed one strap frayed near through, and paused there with Gwen covering him to ask whether she still trusted his read on the situation.

## Summary

**Before:** Under cover of mist, the player and Gwen scouted a bandit camp, aiming to reach a strongbox marked on their map while a watchman dozed near a lean-to. Approaching the second tent, the player pulled Gwen down behind a woodpile just in time to avoid a half-dressed bandit who stepped out to relieve himself before returning to sleep in the first tent; Gwen lingered a moment before pulling free, murmuring "Good eyes." After waiting to confirm the camp was settled, the two returned to the strongbox tent, finding its old iron caravan lock too sturdy to pick. They shifted to testing the straps securing it instead, discovering one frayed nearly through at the fold—a possible way in. The player then turned to Gwen to ask whether she still trusted their read on the situation, leaving the moment of decision unresolved.

**After:** At dawn, Kaelen and Gwen scouted a bandit camp holding a stolen strongbox. Kaelen sent Gwen to circle the right flank near the strongbox tent while a drowsy watchman kept post, then searched an abandoned bedroll himself, finding a whetstone, a few copper coins, dried meat, and a charcoal map marking the strongbox's location behind the second tent. Moving low toward it, the pair sheltered behind a woodpile while a second bandit briefly woke and relieved himself before returning to sleep. Reaching the strongbox tent, Kaelen found its lock too sturdy to force by hand, but noticed one securing strap frayed nearly through. With Gwen covering him, he paused to ask whether she still trusted his read on the situation before attempting to exploit the weak strap.

## The story that was summarized

**start:** Dawn breaks grey over the treeline as you and Gwen crouch at the edge of the bandit camp. Smoke curls from a dying fire; three bedrolls lie empty. Somewhere ahead, the stolen caravan strongbox waits.

Gwen checks her bowstring and looks to you. "Quiet, or loud?"

**do:** go quiet and signal Gwen to circle around the right flank

**ai:** You melt back from the ridge line without a sound, boots finding moss instead of twigs, and catch Gwen's eye. Two fingers point right, then curl in a half-circle — *swing wide, come at the flank*. She reads it instantly, dips her chin, and peels off through the bracken, bow already half-raised, moving low and quiet along the tree line until the mist swallows her.

You hold position at the camp's edge, breath slow, watching the dying fire spit its last embers. A bandit shifts on watch near the lean-to, spear butt planted in the dirt, more bored than alert. No one else stirs. Somewhere off to the right you catch the faintest whisper of disturbed leaves — Gwen, further along than you'd expect, patient as ever. She's given herself a clean angle on the strongbox tent from the flank, out of the watchman's sightline entirely.

The plan is good. Quiet holds. All you need now is the first move — and the choice of who falls first, and how.

The watchman yawns, oblivious, close enough that a single silent step would put you in reach of his throat before he ever draws breath to shout.

**do:** search the nearest bedroll for anything useful

**ai:** You crouch by the nearest bedroll — a heap of stitched furs still holding the sleeper's warmth, though its owner is long since up and yawning by the fire. You keep one eye on the watchman as your fingers work through the folds. A whetstone, a handful of copper coins that clink softly against your palm before you still them with a fist, a scrap of oilcloth wrapped around dried meat gone hard at the edges. Nothing worth the risk so far.

Then your fingers catch on something stiffer, tucked beneath the bedroll's edge — a folded scrap of parchment, half-torn, charcoal-scrawled. A rough map of the camp, crude but legible: the lean-to marked with an X, a second mark deeper in camp labeled in shorthand you recognize as bandit cant for *strongbox*. It's tucked behind the second tent, not the first — further from the watchman than you'd guessed, closer to where Gwen is now waiting in the mist.

You fold it away inside your jerkin, pulse ticking a little faster. Small find, real value — it just turned guesswork into a plan. The watchman still hasn't stirred, spear planted, chin dropping toward his chest. Gwen's silhouette is a smudge of shadow at the flank, patient, waiting on your signal.



The camp holds still around you — watchman drowsing, fire dying, Gwen coiled and ready on the flank. The map sits warm against your ribs, the strongbox's true location now known. Your move.

**do:** move toward the strongbox, keeping low

**ai:** You drop low, weight rolling from heel to ball of each foot, threading between the empty bedroll and a stack of split firewood. The watchman's chin has sunk near to his chest, spear listing in his slack grip. You give him a wide berth anyway — no need to gamble breath on a man already halfway to sleep.

The second tent is smaller than the first, its flap laced shut with a crude leather cord. Just as the parchment promised, its bulk sits wrong for a tent meant to sleep anyone — square-cornered, iron-banded, too heavy for the ground it rests on. You crouch beside it, easing the lacing open one loop at a time, and the flap parts on a dull gleam of banded oak and iron: the strongbox, lock intact, straps still buckled from the caravan raid.

A shift of cloth behind you — not the watchman, too far behind you for that. You go still, hand dropping to your knife hilt, eyes cutting sideways through the tent's shadow toward the sound. Nothing resolves yet, just a settling, maybe wind through canvas, maybe not. Across the clearing, Gwen has gone motionless too, bow half-drawn, watching the same dark gap between tents that's caught your attention.

The strongbox is here, within reach, real weight under your palm instead of a rumor on a scrap of parchment. But something in that gap between the tents hasn't declared itself yet, and the watchman is still only a few careless steps away from waking to find you crouched over his camp's whole reason for being.

**do:** grab her wrist and pull her down behind the woodpile

**ai:** Your hand shoots out and closes around Gwen's wrist before she's fully turned toward the gap between the tents. She goes rigid for half a heartbeat — a killer's instinct, ready to break the grip — then reads your face and lets herself be pulled. You drag her down behind the woodpile together, shoulders pressed to split logs, breath shallow.

The shape that had been shifting in the shadow resolves itself: a second bandit, roused early, stumbling out from behind the tents with his breeches half-laced and a piss-bucket in hand, utterly uninterested in anything but the treeline. He doesn't look toward the strongbox tent. Doesn't look toward the woodpile. He shuffles past, empties the bucket against a stump, and turns back the way he came, already yawning.

Gwen's breath eases out slow beside you. She doesn't pull her wrist away immediately — a beat longer than she needs to — before easing it free herself. "Good eyes," she breathes, barely sound at all. Her gaze flicks to your hand, then back to your face, something unreadable and warm passing behind it. Watching your back instead of charging in — that's the kind of thing she remembers.

Through the gap in the logs you track the second bandit's path: he ducks into the first tent, not the watch line, and doesn't reemerge. Three accounted for now — the drowsing watchman, this one bedding back down, and whoever else the camp is hiding. The strongbox tent is still open behind you, lock waiting, straps waiting. Gwen settles back into her crouch, bow across her knees, eyes on you for the next signal.

**do:** wait for the bandit to turn, then move

**ai:** You go still as stone, eyes fixed on the tent flap the second bandit vanished into. Seconds crawl. Then the canvas stirs one last time — a grunt, a shifting weight settling onto a bedroll, the particular silence of a man who's decided the day can wait a while longer. You count five heartbeats to be sure, watching for any second shadow, any sign he's only feigning sleep. Nothing. Even the watchman by the fire hasn't stirred, spear still planted, chin still low.

You ease up from the woodpile, moving on the balls of your feet, and slip back to the strongbox tent. Gwen catches the motion and rises with you, silent as a drawn breath, bow angled to cover the gap between tents while you work. The lock is old iron, caravan-make, not bandit work — it'll need proper tools or proper force, not fingers.

You crouch and test the straps first, feeling for a weak buckle, a give point. One strap is frayed near through at the fold. With steady pressure it might part without the sharp crack of metal — quieter than forcing the lock, if slower. Gwen shifts her weight beside you, patient, trusting you to read the moment right, her eyes flicking once toward the dozing watchman and back.

The camp holds its breath around you both. The strongbox waits under your hands, and the next move is yours to make.

**do:** ask her whether she still trusts my read on this

**ai:** 

