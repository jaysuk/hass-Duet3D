from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_AXES,
    ATTR_CANCEL,
    ATTR_FILAMENT,
    ATTR_GCODE,
    ATTR_OBJECT,
    ATTR_TOOL,
    DOMAIN,
    SERVICE_ACKNOWLEDGE_MESSAGE,
    SERVICE_CANCEL,
    SERVICE_CANCEL_OBJECT,
    SERVICE_EMERGENCY_STOP,
    SERVICE_HOME,
    SERVICE_LOAD_FILAMENT,
    SERVICE_PAUSE,
    SERVICE_RESET_AFTER_EMERGENCY_STOP,
    SERVICE_RESUME,
    SERVICE_SEND_GCODE,
    SERVICE_UNLOAD_FILAMENT,
)
from .controls import (
    CANCEL_STATES,
    HOME_STATES,
    IDLE_STATES,
    PAUSE_STATES,
    PRINTING,
    RESUME_STATES,
    fresh_status,
    require,
    send,
)
from .hardware import message_box
from .jobinfo import build_objects

_LOGGER = logging.getLogger(__name__)

_SERVICES = (
    SERVICE_SEND_GCODE,
    SERVICE_HOME,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_CANCEL,
    SERVICE_ACKNOWLEDGE_MESSAGE,
    SERVICE_EMERGENCY_STOP,
    SERVICE_RESET_AFTER_EMERGENCY_STOP,
    SERVICE_LOAD_FILAMENT,
    SERVICE_UNLOAD_FILAMENT,
    SERVICE_CANCEL_OBJECT,
)

_TARGET = {
    vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("entity_id"): cv.entity_ids,
}
_FILAMENT_NAME = vol.All(cv.string, vol.Match(r'^[^"\r\n;]+$'))
_TOOL = vol.All(vol.Coerce(int), vol.Range(min=0))
_AXIS_LETTER = vol.All(cv.string, vol.Upper, vol.Match(r"^[A-Z]$"))


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list:
    """The printers a call is aimed at.

    With no target, that is the only configured printer; with several the caller
    has to say which, because sending G-code to the wrong printer is not harmless.
    """
    loaded = {
        entry_id: data["coordinator"]
        for entry_id, data in hass.data.get(DOMAIN, {}).items()
    }
    device_ids = call.data.get("device_id", [])
    entity_ids = call.data.get("entity_id", [])
    if not device_ids and not entity_ids:
        if len(loaded) == 1:
            return list(loaded.values())
        raise ServiceValidationError(
            "Choose which Duet printer to send this to"
            if loaded
            else "No Duet3D printer is set up"
        )

    entry_ids: set[str] = set()
    for device_id in device_ids:
        if (device := dr.async_get(hass).async_get(device_id)) is not None:
            entry_ids.update(device.config_entries)
    for entity_id in entity_ids:
        if (entity := er.async_get(hass).async_get(entity_id)) and entity.config_entry_id:
            entry_ids.add(entity.config_entry_id)
    targeted = [loaded[entry_id] for entry_id in entry_ids if entry_id in loaded]
    if not targeted:
        raise ServiceValidationError("The selected target is not a Duet3D printer")
    return targeted


def async_register_services(hass: HomeAssistant) -> None:
    """Register the printer services once, however many printers are set up."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_GCODE):
        return

    async def send_code(call: ServiceCall):
        """Send any G-code. No state checks: this is the power-user escape hatch."""
        for coordinator in _coordinators(hass, call):
            await send(coordinator, call.data[ATTR_GCODE])

    async def home(call: ServiceCall):
        axes = call.data.get(ATTR_AXES, [])
        gcode = " ".join(["G28", *axes])
        for coordinator in _coordinators(hass, call):
            await require(coordinator, HOME_STATES, "home")
            await send(coordinator, gcode)

    def simple(gcode: str, allowed: set[str] | None, action: str):
        async def handler(call: ServiceCall):
            for coordinator in _coordinators(hass, call):
                if allowed is not None:
                    await require(coordinator, allowed, action)
                await send(coordinator, gcode)

        return handler

    async def acknowledge_message(call: ServiceCall):
        # M292 closes the message box; P1 is the box's Cancel button.
        gcode = "M292 P1" if call.data.get(ATTR_CANCEL) else "M292"
        for coordinator in _coordinators(hass, call):
            await fresh_status(coordinator)
            if message_box(coordinator.get_sensor_state("status.state")) is None:
                raise ServiceValidationError(
                    f"{coordinator.config_entry.title} has no message waiting"
                )
            await send(coordinator, gcode)

    async def selected_tool_gcode(call: ServiceCall, gcode: str, action: str):
        """M701/M702 act on the selected tool and have no tool parameter, so the tool
        asked for must already be the selected one. Selecting it here would run the
        tool change macros, which is a decision for the caller."""
        tool = call.data[ATTR_TOOL]
        for coordinator in _coordinators(hass, call):
            await require(coordinator, IDLE_STATES, action)
            selected = coordinator.get_sensor_state("status.state.currentTool")
            if selected != tool:
                now = "none is" if selected in (None, -1) else f"tool {selected} is"
                raise ServiceValidationError(
                    f"Cannot {action}: tool {tool} is not selected ({now}). "
                    f"Select it first, for example with send_code T{tool}"
                )
            await send(coordinator, gcode)
            await coordinator.async_request_refresh()

    async def cancel_object(call: ServiceCall):
        index = call.data[ATTR_OBJECT]
        for coordinator in _coordinators(hass, call):
            await require(coordinator, PRINTING, "cancel an object")
            found = build_objects(coordinator.get_sensor_state("status.job.build"))
            if not any(o["index"] == index for o in found):
                known = ", ".join(str(o["index"]) for o in found) or "none"
                raise ServiceValidationError(
                    f"{coordinator.config_entry.title} has no object {index} (objects: {known})"
                )
            await send(coordinator, f"M486 P{index}")
            await coordinator.async_request_refresh()

    async def load_filament(call: ServiceCall):
        await selected_tool_gcode(call, f'M701 S"{call.data[ATTR_FILAMENT]}"', "load filament")

    async def unload_filament(call: ServiceCall):
        await selected_tool_gcode(call, "M702", "unload filament")

    handlers = {
        SERVICE_SEND_GCODE: (send_code, {vol.Required(ATTR_GCODE): cv.string}),
        SERVICE_HOME: (home, {vol.Optional(ATTR_AXES): vol.All(cv.ensure_list, [_AXIS_LETTER])}),
        SERVICE_PAUSE: (simple("M25", PAUSE_STATES, "pause"), {}),
        SERVICE_RESUME: (simple("M24", RESUME_STATES, "resume"), {}),
        SERVICE_CANCEL: (simple("M0", CANCEL_STATES, "cancel"), {}),
        SERVICE_ACKNOWLEDGE_MESSAGE: (acknowledge_message, {vol.Optional(ATTR_CANCEL): cv.boolean}),
        # Never refused: stopping must work in any state. The board then needs a reset.
        SERVICE_EMERGENCY_STOP: (simple("M112", None, "emergency stop"), {}),
        SERVICE_RESET_AFTER_EMERGENCY_STOP: (simple("M999", {"halted"}, "reset"), {}),
        SERVICE_LOAD_FILAMENT: (
            load_filament,
            {vol.Required(ATTR_TOOL): _TOOL, vol.Required(ATTR_FILAMENT): _FILAMENT_NAME},
        ),
        SERVICE_UNLOAD_FILAMENT: (unload_filament, {vol.Required(ATTR_TOOL): _TOOL}),
        SERVICE_CANCEL_OBJECT: (cancel_object, {vol.Required(ATTR_OBJECT): _TOOL}),
    }
    for name, (handler, fields) in handlers.items():
        hass.services.async_register(
            DOMAIN, name, handler, schema=vol.Schema({**_TARGET, **fields})
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the services when the last printer is unloaded."""
    for name in _SERVICES:
        hass.services.async_remove(DOMAIN, name)
