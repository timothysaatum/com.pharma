"""
vector_clock.py
===============
Pure functions for comparing and merging version vectors.

A version vector is a dict mapping branch_id (or "server") to a
monotonically increasing integer.  Missing keys are treated as 0.

Semantics:
  dominates(a, b)  — a is strictly newer: a[k] >= b[k] for all k,
                     and at least one component is strictly greater.
  concurrent(a, b) — neither dominates the other: a has a component
                     strictly greater than b, AND b has a component
                     strictly greater than a.
  merge(a, b)      — component-wise maximum (used when applying a
                     resolved conflict or merging two known states).
  bump(vec, key)   — increment key's component by 1.
"""

from __future__ import annotations

from typing import Dict


VectorClock = Dict[str, int]


def _geq(a: VectorClock, b: VectorClock) -> bool:
    """True iff a[k] >= b[k] for every key k in b."""
    for k, v in b.items():
        if a.get(k, 0) < v:
            return False
    return True


def dominates(a: VectorClock, b: VectorClock) -> bool:
    """True iff a is strictly newer than b."""
    return _geq(a, b) and a != b


def concurrent(a: VectorClock, b: VectorClock) -> bool:
    """True iff a and b are concurrent (neither dominates)."""
    return not _geq(a, b) and not _geq(b, a)


def merge(a: VectorClock, b: VectorClock) -> VectorClock:
    """Component-wise maximum."""
    result = dict(a)
    for k, v in b.items():
        result[k] = max(result.get(k, 0), v)
    return result


def bump(vec: VectorClock, key: str) -> VectorClock:
    """Return a new vector with key's component incremented."""
    result = dict(vec)
    result[key] = result.get(key, 0) + 1
    return result
