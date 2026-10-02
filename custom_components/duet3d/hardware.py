"""Pure helpers for the non-heater hardware in the object model.

Fans, boards, storage, network interfaces, filament monitors and axis state. Like
``model.py`` this has no Home Assistant imports and treats every input as
untrusted: ``rr_model`` answers ``""`` for a key with no value, lists can hold
``null`` for undefined slots, and fields differ between RRF 3.6, 3.7 and DSF.

Each ``build_*`` returns ``{key: details}`` where the key is stable (it becomes
part of an entity's unique id) and does not depend on list position when the
firmware offers something better, such as a board's CAN address.
"""
from __future__ import annotations

from typing import Any

from .model import as_list, as_number


def _percent(fraction: Any) -> float | None:
    value = as_number(fraction)
    return None if value is None else round(value * 100, 1)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def build_fans(fans: Any) -> dict[str, dict[str, Any]]:
    """Fans by index. Undefined fan slots are ``null`` in the model and skipped."""
    result: dict[str, dict[str, Any]] = {}
    for index, fan in enumerate(as_list(fans)):
        if not isinstance(fan, dict):
            continue
        rpm = as_number(fan.get("rpm"))
        thermostatic = fan.get("thermostatic")
        sensors = thermostatic.get("sensors") if isinstance(thermostatic, dict) else None
        result[f"fan-{index}"] = {
            "index": index,
            "label": _text(fan.get("name")) or f"Fan {index}",
            "speed": _percent(fan.get("actualValue")),
            "requested": _percent(fan.get("requestedValue")),
            # -1 means the fan has no tacho
            "rpm": rpm if rpm is not None and rpm >= 0 else None,
            # The firmware drives a thermostatic fan from temperature and ignores M106 S.
            "thermostatic": isinstance(sensors, list) and len(sensors) > 0,
        }
    return result


def build_boards(boards: Any) -> dict[str, dict[str, Any]]:
    """The main board and any CAN expansion boards, keyed by CAN address.

    ``state`` only exists for expansion boards (``running`` when healthy).
    """
    result: dict[str, dict[str, Any]] = {}
    for index, board in enumerate(as_list(boards)):
        if not isinstance(board, dict):
            continue
        can = board.get("canAddress")
        can = can if isinstance(can, int) and not isinstance(can, bool) else index
        mcu = board.get("mcuTemp") if isinstance(board.get("mcuTemp"), dict) else {}
        v_in = board.get("vIn") if isinstance(board.get("vIn"), dict) else {}
        v_12 = board.get("v12") if isinstance(board.get("v12"), dict) else {}
        result[f"board-{can}"] = {
            "can": can,
            "label": "Main board"
            if can == 0
            else _text(board.get("shortName")) or _text(board.get("name")) or f"Board {can}",
            "name": _text(board.get("name")),
            "firmware": _text(board.get("firmwareVersion")),
            "mcu_temp": as_number(mcu.get("current")),
            "v_in": as_number(v_in.get("current")),
            "v_12": as_number(v_12.get("current")),
            "free_ram": as_number(board.get("freeRam")),
            "state": _text(board.get("state")),
        }
    return result


def build_volumes(volumes: Any) -> dict[str, dict[str, Any]]:
    """Mounted storage with a known free space. Empty card slots are skipped."""
    result: dict[str, dict[str, Any]] = {}
    for index, volume in enumerate(as_list(volumes)):
        if not isinstance(volume, dict) or volume.get("mounted") is not True:
            continue
        free = as_number(volume.get("freeSpace"))
        if free is None:
            continue
        result[f"volume-{index}"] = {
            "index": index,
            "label": f"Storage {index}",
            "free": free,
            "capacity": as_number(volume.get("capacity")),
        }
    return result


_INTERFACE_LABELS = {"wifi": "Wi-Fi", "ethernet": "Ethernet"}


def build_interfaces(network: Any) -> dict[str, dict[str, Any]]:
    """Network interfaces. ``signal`` (dBm) is ``null`` unless Wi-Fi is connected."""
    interfaces = network.get("interfaces") if isinstance(network, dict) else None
    result: dict[str, dict[str, Any]] = {}
    for index, interface in enumerate(as_list(interfaces)):
        if not isinstance(interface, dict):
            continue
        kind = _text(interface.get("type"))
        result[f"interface-{index}"] = {
            "index": index,
            "label": _INTERFACE_LABELS.get(kind or "", f"Interface {index}"),
            "type": kind,
            "ip": _text(interface.get("actualIP")),
            "signal": as_number(interface.get("signal")),
        }
    return result


def build_filament_monitors(monitors: Any) -> dict[str, dict[str, Any]]:
    """Filament monitors by extruder number (the list is indexed by extruder).

    ``status`` is ``ok`` when healthy; other values (``noFilament``,
    ``tooLittleMovement``, ``noDataReceived`` ...) are passed through untouched as
    the set differs between monitor types and firmware versions. ``present`` is
    only a bool when the monitor can tell, which the firmware reports by omitting
    ``filamentPresent`` otherwise. ``enabled`` is obsolete in RRF 3.7; ``enableMode``
    replaces it.
    """
    result: dict[str, dict[str, Any]] = {}
    for extruder, monitor in enumerate(as_list(monitors)):
        if not isinstance(monitor, dict):
            continue
        present = monitor.get("filamentPresent")
        mode = monitor.get("enableMode")
        result[f"monitor-{extruder}"] = {
            "extruder": extruder,
            "status": _text(monitor.get("status")),
            "type": _text(monitor.get("type")),
            "present": present if isinstance(present, bool) else None,
            "enable_mode": mode if isinstance(mode, int) and not isinstance(mode, bool) else None,
        }
    return result


def axes_homed(axes: Any) -> dict[str, Any] | None:
    """Whether every visible axis is homed, or ``None`` if there are no axes."""
    homed: dict[str, bool] = {}
    for axis in as_list(axes):
        if not isinstance(axis, dict) or axis.get("visible") is False:
            continue
        letter = _text(axis.get("letter"))
        if letter and isinstance(axis.get("homed"), bool):
            homed[letter] = axis["homed"]
    if not homed:
        return None
    return {"all": all(homed.values()), "axes": homed}


def message_box(state: Any) -> dict[str, Any] | None:
    """The open M291 message box, or ``None``. Extra fields vary by mode."""
    box = state.get("messageBox") if isinstance(state, dict) else None
    if not isinstance(box, dict) or not _text(box.get("message")):
        return None
    return {
        "message": box["message"],
        **{
            key: box[key]
            for key in ("title", "mode", "seq", "timeout", "cancelButton", "axisControls")
            if box.get(key) is not None
        },
    }


def startup_error(state: Any) -> dict[str, Any] | None:
    """The error from ``config.g`` (or another startup file), or ``None``."""
    error = state.get("startupError") if isinstance(state, dict) else None
    if not isinstance(error, dict) or not _text(error.get("message")):
        return None
    return {key: error.get(key) for key in ("message", "file", "line")}


def build_hardware(status: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Everything above for one poll of the whole object model."""
    sensors = status.get("sensors") if isinstance(status, dict) else None
    status = status if isinstance(status, dict) else {}
    return {
        "fans": build_fans(status.get("fans")),
        "boards": build_boards(status.get("boards")),
        "volumes": build_volumes(status.get("volumes")),
        "interfaces": build_interfaces(status.get("network")),
        "monitors": build_filament_monitors(
            sensors.get("filamentMonitors") if isinstance(sensors, dict) else None
        ),
    }
