"""Slot diffing rules: decide which slots are new / alertable."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable


def is_new_slot(slot: dict, known: set[tuple]) -> bool:
    """A slot is NEW iff its (profile, location, category, date, time) key
    has never been seen before at all."""
    return key_of(slot) not in known


def key_of(slot: dict) -> tuple:
    return (
        slot["profile"],
        slot["location"],
        slot["category"],
        slot["date"],
        slot["time"],
    )


def derive_new_and_alertable(current: Iterable[dict], existing_keys: set[tuple]) -> list[dict]:
    """From the current poll's slots plus the set of all keys ever recorded,
    return the slots that should trigger an alert.

    - Same slots -> no alert.
    - New slot -> alert.
    - Slot removed -> no alert (nothing to alert on).
    - Re-added removed slot -> alert IF it was never alerted before; the
      UI-level decision of "already alerted" lives with the caller (store).
    """
    result = []
    for slot in current:
        if key_of(slot) not in existing_keys:
            result.append(slot)
    return result


def summarize(keys: Iterable[tuple], current: Iterable[dict]) -> dict:
    current_keys = {key_of(s) for s in current}
    new = [k for k in current_keys if k not in set(keys)]
    return {
        "total": len(current_keys),
        "new": len(new),
    }


def dedupe_alerted(current: Iterable[dict], alerted_keys: set[tuple]) -> list[dict]:
    """Exclude slots already marked alerted (defensive re-broadcast guard)."""
    return [s for s in current if key_of(s) not in alerted_keys]


def sort_by_date(slots: Iterable[dict]) -> list[dict]:
    return sorted(slots, key=lambda s: (s["date"], s["time"] or ""))