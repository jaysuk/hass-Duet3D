from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_AXES,
    ATTR_CANCEL,
    ATTR_GCODE,
    DOMAIN,
    SERVICE_ACKNOWLEDGE_MESSAGE,
    SERVICE_CANCEL,
    SERVICE_HOME,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_SEND_GCODE,
)
from .hardware import message_box

_LOGGER = logging.getLogger(__name__)

_SERVICES = (
    SERVICE_SEND_GCODE,
    SERVICE_HOME,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_CANCEL,
    SERVICE_ACKNOWLEDGE_MESSAGE,
)

# ``state.status`` values in which each action makes sense. Sending G-code through
# the web API while a job runs interleaves with it, so e.g. homing mid-print would
# crash the toolhead; refuse rather than trust the automation that called us.
_PRINTING = {"processing", "simulating"}
_HOME_STATES = {"idle"}
_PAUSE_STATES = _PRINTING
_RESUME_STATES = {"paused"}
_CANCEL_STATES = _PRINTING | {"paused", "pausing", "resuming"}

_TARGET = {
    vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("entity_id"): cv.entity_ids,
}
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


async def _fresh_status(coordinator) -> str | None:
    """The printer's state right now, not as of the last poll."""
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise HomeAssistantError(f"{coordinator.config_entry.title} is not reachable")
    return coordinator.get_sensor_state("status.state.status")


async def _require(coordinator, allowed: set[str], action: str) -> None:
    status = await _fresh_status(coordinator)
    if status not in allowed:
        raise ServiceValidationError(
            f"Cannot {action} while {coordinator.config_entry.title} is {status}"
        )


async def _send(coordinator, gcode: str) -> None:
    try:
        await coordinator.send_gcode(gcode)
    except Exception as error:
        raise HomeAssistantError(
            f"Error communicating with {coordinator.config_entry.title}: {error}"
        ) from error


def async_register_services(hass: HomeAssistant) -> None:
    """Register the printer services once, however many printers are set up."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_GCODE):
        return

    async def send_code(call: ServiceCall):
        """Send any G-code. No state checks: this is the power-user escape hatch."""
        for coordinator in _coordinators(hass, call):
            await _send(coordinator, call.data[ATTR_GCODE])

    async def home(call: ServiceCall):
        axes = call.data.get(ATTR_AXES, [])
        gcode = " ".join(["G28", *axes])
        for coordinator in _coordinators(hass, call):
            await _require(coordinator, _HOME_STATES, "home")
            await _send(coordinator, gcode)

    def simple(gcode: str, allowed: set[str], action: str):
        async def handler(call: ServiceCall):
            for coordinator in _coordinators(hass, call):
                await _require(coordinator, allowed, action)
                await _send(coordinator, gcode)

        return handler

    async def acknowledge_message(call: ServiceCall):
        # M292 closes the message box; P1 is the box's Cancel button.
        gcode = "M292 P1" if call.data.get(ATTR_CANCEL) else "M292"
        for coordinator in _coordinators(hass, call):
            await _fresh_status(coordinator)
            if message_box(coordinator.get_sensor_state("status.state")) is None:
                raise ServiceValidationError(
                    f"{coordinator.config_entry.title} has no message waiting"
                )
            await _send(coordinator, gcode)

    handlers = {
        SERVICE_SEND_GCODE: (send_code, {vol.Required(ATTR_GCODE): cv.string}),
        SERVICE_HOME: (home, {vol.Optional(ATTR_AXES): vol.All(cv.ensure_list, [_AXIS_LETTER])}),
        SERVICE_PAUSE: (simple("M25", _PAUSE_STATES, "pause"), {}),
        SERVICE_RESUME: (simple("M24", _RESUME_STATES, "resume"), {}),
        SERVICE_CANCEL: (simple("M0", _CANCEL_STATES, "cancel"), {}),
        SERVICE_ACKNOWLEDGE_MESSAGE: (acknowledge_message, {vol.Optional(ATTR_CANCEL): cv.boolean}),
    }
    for name, (handler, fields) in handlers.items():
        hass.services.async_register(
            DOMAIN, name, handler, schema=vol.Schema({**_TARGET, **fields})
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the services when the last printer is unloaded."""
    for name in _SERVICES:
        hass.services.async_remove(DOMAIN, name)
