"""Storing and comparing embedding vectors.

A 1536-dimension vector written as a JSON list is about 31 KB, because every
component is spelled out as a decimal string of seventeen-odd digits. The same
vector as packed float32 is 6,144 bytes — a straight 5x, and the memory bank is
read in full on every turn, so those bytes are paid over and over.

**Float32 is not an approximation here.** The embedding endpoints return
vectors computed in float32, rendered into JSON as the shortest decimal string
that round-trips through a double; converting that back to float32 recovers the
original bits exactly. Nothing is lost that was ever there, which is why the
conversion needs no re-embedding and carries no retrieval-quality risk.

Dimensions are deliberately unchanged. Dropping to 512 or 768 would have saved
another 3x and cost an API call per stored memory to re-embed, against a bank
that the packing and the in-process cache together already make cheap.
"""

import math
import struct


def pack(vector: list[float]) -> bytes:
    """A vector as little-endian float32."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    """The inverse of `pack`. Length is implied: four bytes per component."""
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    # Different lengths means the embedding model changed since this vector was
    # stored; zip() would silently score garbage.
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
