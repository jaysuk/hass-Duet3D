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


def pick_thumbnail(thumbnails: Any, inline: bool) -> dict[str, Any] | None:
    """The largest thumbnail of the job file that can be shown.

    DSF (SBC mode) embeds the image as base64 in ``data``; a standalone board lists
    only ``offset`` and ``size``, and the image is fetched with ``rr_thumbnail``.
    ``inline`` says which of the two to look for.
    """
    usable = []
    for thumbnail in as_list(thumbnails):
        if not isinstance(thumbnail, dict):
            continue
        if inline:
            if not isinstance(thumbnail.get("data"), str):
                continue
        else:
            offset = thumbnail.get("offset")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset <= 0:
                continue
        usable.append(thumbnail)

    def area(thumbnail: dict[str, Any]) -> float:
        width, height = as_number(thumbnail.get("width")), as_number(thumbnail.get("height"))
        return (width or 0) * (height or 0)

    return max(usable, key=area, default=None)


def progress_percent(extruded: Any, filament: Any) -> float:
    """Share of the slicer's filament total already extruded, 0 to 100.

    0 when there is no total (no job, or a file without filament data). The raw
    extrusion leaves out extrusion inside macros, so it can slightly exceed the
    slicer's total; the result is capped at 100.
    """
    total = slicer_total(filament)
    if not total or total <= 0:
        return 0
    if isinstance(extruded, bool) or not isinstance(extruded, (int, float)):
        return 0
    return min(100, round(extruded / total * 100, 2))


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
