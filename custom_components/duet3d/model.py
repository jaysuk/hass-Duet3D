"""Pure helpers for navigating the RepRapFirmware object model.

Deliberately free of Home Assistant imports so the logic can be unit tested in
isolation. Inputs are raw values as returned by ``rr_model`` (standalone) or the
DSF ``/machine/status`` endpoint (SBC); both use the same object model, so every
input is treated as untrusted and a missing or malformed value yields ``None``.

Heaters are never identified by position. A tool names its heaters in
``tools[n].heaters``, and the bed and chamber are found through
``heat.bedHeaterMapping`` / ``heat.chamberHeaterMapping`` (RRF 3.7) or the older
``heat.bedHeaters`` / ``heat.chamberHeaters`` (RRF 3.6 and earlier, still returned
by DSF). Heater 0 is not necessarily the bed and tool 0 is not necessarily
``heaters[0]``.
"""
from __future__ import annotations

import re
from typing import Any

_PATH_PART = re.compile(r"^(?P<name>[^\[\]]+)(?:\[(?P<index>\d+)\])?$")

# RRF reports a failed temperature sensor as a value at or below absolute zero.
_SENSOR_FAULT_BELOW = -273.0

TOOL_TEMPERATURE_TYPES = ("current", "active", "standby")
BED_TEMPERATURE_TYPES = ("current", "active")


def resolve(data: Any, path: str) -> Any:
    """Follow ``a.b[2].c`` through nested dicts and lists, or return ``None``."""
    value = data
    for part in path.split("."):
        match = _PATH_PART.match(part)
        if match is None or not isinstance(value, dict):
            return None
        value = value.get(match["name"])
        if match["index"] is not None:
            index = int(match["index"])
            if not isinstance(value, list) or index >= len(value):
                return None
            value = value[index]
    return value


def set_path(data: dict, path: str, value: Any) -> None:
    """Store ``value`` at the dotted ``path`` (``a.b``), creating dicts on the way.

    Used to rebuild the nested object model from per-key ``rr_model`` responses, so
    a key such as ``sensors.filamentMonitors`` lands where DSF would put it.
    """
    *parents, leaf = path.split(".")
    for part in parents:
        child = data.get(part)
        if not isinstance(child, dict):
            child = data[part] = {}
        data = child
    data[leaf] = value


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def as_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _heater_groups(heat: dict, mapping_key: str, legacy_key: str) -> list[tuple[int, list[int]]]:
    """Return ``(bed or chamber index, heater indices)`` pairs, skipping empty ones.

    ``…Mapping`` is a list (one per bed) of lists of heaters. The legacy key is a
    flat list with one heater per bed and ``-1`` for none.
    """
    mapping = heat.get(mapping_key)
    if isinstance(mapping, list):
        groups = []
        for index, entry in enumerate(mapping):
            ids = [h for h in (entry if isinstance(entry, list) else [entry]) if isinstance(h, int) and h >= 0]
            if ids:
                groups.append((index, ids))
        return groups
    return [
        (index, [heater])
        for index, heater in enumerate(as_list(heat.get(legacy_key)))
        if isinstance(heater, int) and not isinstance(heater, bool) and heater >= 0
    ]


def build_heater_roles(heat: Any, tools: Any) -> dict[str, dict[str, Any]]:
    """Describe every heater that something is attached to, keyed by a stable id.

    A role is a heater plus what it is used for, so a sensor can be named after the
    tool, bed or chamber rather than after an index that means nothing to the user.
    Heaters nothing refers to (spare outputs) get no role.

    Keys are stable across firmware versions: ``tool-<n>`` for the tool's first
    heater (``tool-<n>-h<slot>`` for further ones), ``bed``/``chamber`` for the
    first of each (``bed-<i>-<slot>`` for others).
    """
    if not isinstance(heat, dict):
        return {}
    heaters = as_list(heat.get("heaters"))

    def exists(index: Any) -> bool:
        return (
            isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < len(heaters)
            and isinstance(heaters[index], dict)
        )

    roles: dict[str, dict[str, Any]] = {}

    for position, tool in enumerate(as_list(tools)):
        if not isinstance(tool, dict):
            continue
        number = tool.get("number", position)
        for slot, heater in enumerate(as_list(tool.get("heaters"))):
            if not exists(heater):
                continue
            key = f"tool-{number}" if slot == 0 else f"tool-{number}-h{slot}"
            roles[key] = {
                "key": key,
                "kind": "tool",
                "label": f"Tool {number}" if slot == 0 else f"Tool {number} heater {slot}",
                "heater": heater,
                "tool": number,
                "tool_position": position,
                "slot": slot,
                "types": TOOL_TEMPERATURE_TYPES,
            }

    for kind, label, mapping_key, legacy_key in (
        ("bed", "Bed", "bedHeaterMapping", "bedHeaters"),
        ("chamber", "Chamber", "chamberHeaterMapping", "chamberHeaters"),
    ):
        for index, ids in _heater_groups(heat, mapping_key, legacy_key):
            for slot, heater in enumerate(ids):
                if not exists(heater):
                    continue
                key = kind if index == 0 and slot == 0 else f"{kind}-{index}-{slot}"
                name = label if index == 0 else f"{label} {index}"
                roles[key] = {
                    "key": key,
                    "kind": kind,
                    "label": name if slot == 0 else f"{name} heater {slot}",
                    "heater": heater,
                    "index": index,
                    "slot": slot,
                    "types": BED_TEMPERATURE_TYPES,
                }
    return roles


def _heater(heat: Any, role: dict[str, Any]) -> dict | None:
    heaters = as_list(heat.get("heaters")) if isinstance(heat, dict) else []
    heater = heaters[role["heater"]] if role["heater"] < len(heaters) else None
    return heater if isinstance(heater, dict) else None


def heater_state(heat: Any, role: dict[str, Any]) -> str | None:
    """``off``, ``standby``, ``active``, ``fault``, ``tuning`` or ``offline``."""
    heater = _heater(heat, role)
    state = heater.get("state") if heater else None
    return state if isinstance(state, str) and state else None


def heater_power(heat: Any, role: dict[str, Any]) -> float | None:
    """Average PWM as a percentage (RRF reports 0..1)."""
    heater = _heater(heat, role)
    pwm = as_number(heater.get("avgPwm")) if heater else None
    return None if pwm is None else round(pwm * 100, 1)


def heater_value(heat: Any, tools: Any, role: dict[str, Any], sensor_type: str) -> float | int | None:
    """Temperature for one role: ``current``, ``active`` or ``standby``.

    A tool's own target is preferred for ``active``/``standby`` because two tools
    can share a heater. ``current`` is ``None`` while the temperature sensor is
    faulted so Home Assistant shows "unknown" rather than -273.
    """
    heater = _heater(heat, role)
    if heater is None:
        return None

    if sensor_type == "current":
        value = as_number(heater.get("current"))
        return None if value is None or value <= _SENSOR_FAULT_BELOW else value

    if role["kind"] == "tool":
        tool_list = as_list(tools)
        position = role["tool_position"]
        tool = tool_list[position] if position < len(tool_list) else None
        if isinstance(tool, dict):
            targets = as_list(tool.get(sensor_type))
            if role["slot"] < len(targets) and as_number(targets[role["slot"]]) is not None:
                return targets[role["slot"]]
    return as_number(heater.get(sensor_type))


def heater_limit(heat: Any, role: dict[str, Any]) -> float | None:
    """The heater's maximum temperature (``heaters[n].max``), if the firmware reports it."""
    heater = _heater(heat, role)
    limit = as_number(heater.get("max")) if heater else None
    return limit if limit is not None and limit > 0 else None
