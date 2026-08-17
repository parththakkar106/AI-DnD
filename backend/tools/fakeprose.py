"""Filler text that behaves like prose under compression.

Shared by the stress harness and the egress tests, because both now measure
something a repeated string would answer wrongly. `"x" * 20_000` compresses
about a thousandfold and English three- or fourfold, so a fixture built from
repeats makes a compressed column look free and any ceiling drawn around it
meaningless.

Not a language model, and does not need to be. What matters is the symbol
distribution, not the sense.
"""
from __future__ import annotations

import random

WORDS = (
    "corridor narrows shoulders brush wet stone torchlight gutters draught "
    "smells cold iron somewhere ahead water moving count nine paces passage "
    "opens chamber ceiling lost dark sound breathing comes back half second "
    "late Gwen catches sleeve without word points floor line pale grit laid "
    "across threshold deliberate arc quartermaster bandit camp above ford "
    "tunnels exchange key lantern rope knife bread rain mud hill road gate "
    "watchman silver debt promise fever horse cart river bridge mill barley "
    "smoke rafters bench ale ledger seal wax parchment ink candle shutter "
    "hinge bolt cellar barrel salt fish nets harbour tide gull mast canvas"
).split()


def prose(rng: random.Random, nbytes: int) -> str:
    """Roughly `nbytes` of varied sentences, deterministic for a given rng."""
    out: list[str] = []
    total = 0
    while total < nbytes:
        sentence = " ".join(rng.choice(WORDS) for _ in range(rng.randint(8, 18)))
        chunk = sentence.capitalize() + ". "
        out.append(chunk)
        total += len(chunk)
    return "".join(out)[:nbytes]
