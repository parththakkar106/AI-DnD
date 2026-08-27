"""Storing and comparing embedding vectors.

A 1536-dimension vector written as a JSON list is about 31 kB, because every
component is written as a decimal string of about seventeen digits. The same
vector as packed float32 is 6,144 bytes, which is five times smaller, and the
memory bank is read in full on every turn, so those bytes are paid repeatedly.

Float32 is not an approximation here. The embedding endpoints return vectors
computed in float32 and render them into JSON as the shortest decimal string
that round-trips through a double. Converting that back to float32 recovers the
original bits exactly. Nothing that was present is lost, which is why the
conversion needs no re-embedding and carries no risk to retrieval quality.

Dimensions are deliberately unchanged. Dropping to 512 or 768 would have saved
another 3x and cost an API call per stored memory to re-embed, against a bank
that the packing and the in-process cache together already make cheap.
"""

import math
import struct
import sys
from array import array


def pack(vector) -> bytes:
    """A vector as little-endian float32."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> array:
    """The inverse of `pack`. Length is implied: four bytes per component.

    Returns an `array("f")` rather than a list, because these are held in
    memory between turns: the array is the same 4 bytes a component the column
    is, where a list of Python floats is eight times that. It indexes, zips and
    lens like a list, which is all the ranking needs.
    """
    vector = array("f")
    vector.frombytes(blob)
    if sys.byteorder != "little":
        vector.byteswap()
    return vector


def cosine(a: list[float], b: list[float]) -> float:
    # Different lengths means the embedding model changed since this vector was
    # stored; zip() would silently score garbage.
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
