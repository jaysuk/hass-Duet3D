"""Pure helpers for turning the RepRapFirmware object model into extruder data.

Deliberately free of Home Assistant imports so the logic can be unit tested in
isolation. Inputs are the raw values of ``move.extruders``, ``tools`` and
``state.currentTool`` as returned by ``rr_model`` (standalone) or the DSF
``/machine/status`` endpoint (SBC); both use the same object model.
"""
from __future__ import annotations

from typing import Any


def _as_list(value: Any) -> list:
    """Return value if it is a list, otherwise an empty list.

    ``rr_model`` answers with an empty string when a key has no value and the
    coordinator stores that verbatim, so every input must be treated as untrusted.
    """
    return value if isinstance(value, list) else []


def tools_by_extruder(tools: Any) -> dict[int, list[int]]:
    """Map extruder index -> tool numbers that drive it.

    ``tools`` is indexed by tool number and may contain ``None`` for tool numbers
    that were never defined. A tool may drive several extruders (mixing tools) and
    several tools may share one extruder.
    """
    mapping: dict[int, list[int]] = {}
    for position, tool in enumerate(_as_list(tools)):
        if not isinstance(tool, dict):
            continue
        number = tool.get("number", position)
        for extruder in _as_list(tool.get("extruders")):
            if isinstance(extruder, int):
                mapping.setdefault(extruder, []).append(number)
    return mapping


def active_extruders(tools: Any, current_tool: Any) -> set[int]:
    """Return the extruder indices driven by the currently selected tool."""
    if isinstance(current_tool, bool) or not isinstance(current_tool, int) or current_tool < 0:
        return set()
    active: set[int] = set()
    for extruder, tool_numbers in tools_by_extruder(tools).items():
        if current_tool in tool_numbers:
            active.add(extruder)
    return active


def tool_filament(extruders: Any, tools: Any, tool: Any) -> str | None:
    """The filament loaded in the first extruder of ``tool``: ``""`` if none, ``None`` if the
    tool drives no extruder. This is the extruder ``M701``/``M702`` act on."""
    driven = sorted(active_extruders(tools, tool))
    if not driven:
        return None
    items = _as_list(extruders)
    if driven[0] >= len(items) or not isinstance(items[driven[0]], dict):
        return ""
    filament = items[driven[0]].get("filament")
    return filament if isinstance(filament, str) else ""


def build_extruders(extruders: Any, tools: Any, current_tool: Any) -> list[dict[str, Any]]:
    """Describe every extruder in a form ready to expose as entity attributes.

    ``filament`` is the name set by ``M701``/``M702`` (empty when nothing is
    loaded or after a firmware reset). Nothing else in RRF carries a material or
    colour, so those are left to the consumer.
    """
    tool_map = tools_by_extruder(tools)
    active = active_extruders(tools, current_tool)
    result: list[dict[str, Any]] = []
    for index, extruder in enumerate(_as_list(extruders)):
        if not isinstance(extruder, dict):
            continue
        filament = extruder.get("filament")
        result.append(
            {
                "extruder": index,
                "filament": filament if isinstance(filament, str) else "",
                "filament_diameter": extruder.get("filamentDiameter"),
                "position": extruder.get("position"),
                "tools": tool_map.get(index, []),
                "active": index in active,
            }
        )
    return result


def slicer_filament(filament: Any) -> list[float]:
    """Filament length in mm the slicer estimated for each extruder of the job file.

    ``job.file.filament`` is a list indexed by extruder; anything else (the value is
    ``null`` with no job, or an empty string over ``rr_model``) gives an empty list.
    """
    return [
        float(length)
        for length in _as_list(filament)
        if isinstance(length, (int, float)) and not isinstance(length, bool)
    ]
