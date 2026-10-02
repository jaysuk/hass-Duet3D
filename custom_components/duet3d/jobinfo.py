"""Pure helpers for the job-related readings: estimates and labelled objects.

No Home Assistant imports, and every input is untrusted (``rr_model`` answers ``""``
for a key with no value, and most job fields are ``null`` when no job is loaded).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .extruders import slicer_filament
from .model import as_list, as_number


def eta(read_at: datetime | None, seconds_left: Any) -> datetime | None:
    """When the job should finish, to the nearest minute.

    Rounded so the value does not change on every poll as the estimate wobbles: a
    timestamp that moves by seconds each time is a state change each time.
    """
    left = as_number(seconds_left)
    if read_at is None or left is None or left <= 0:
        return None
    finish = read_at + timedelta(seconds=left)
    return (finish + timedelta(seconds=30)).replace(second=0, microsecond=0)


def projected_total_minutes(duration: Any, seconds_left: Any) -> float | None:
    """Time elapsed plus time left, in minutes. Only meaningful while a job runs."""
    elapsed, left = as_number(duration), as_number(seconds_left)
    if elapsed is None or left is None or left <= 0:
        return None
    return round((elapsed + left) / 60.0, 1)


def slicer_total(filament: Any) -> float | None:
    """Total filament the slicer expects the job to use, in mm."""
    lengths = slicer_filament(filament)
    return round(sum(lengths), 1) if lengths else None


def build_objects(build: Any) -> list[dict[str, Any]]:
    """The objects of a job file with labelling (``M486``), as ``index``/``name``/``cancelled``."""
    objects = as_list(build.get("objects")) if isinstance(build, dict) else []
    result = []
    for index, item in enumerate(objects):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        result.append(
            {
                "index": index,
                "name": name if isinstance(name, str) and name else f"Object {index}",
                "cancelled": item.get("cancelled") is True,
            }
        )
    return result
