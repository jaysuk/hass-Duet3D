"""Buttons: job control, homing, the message box, emergency stop and macros.

Every press goes through ``controls``, which reads the printer's state fresh and
refuses (``ServiceValidationError``) what the state does not allow. The buttons stay
available meanwhile, so they do not flap with the job.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuetDataUpdateCoordinator
from .const import DOMAIN, MACRO_DIRECTORY
from .controls import (
    CANCEL_STATES,
    HOME_STATES,
    PAUSE_STATES,
    RESUME_STATES,
    fresh_status,
    send,
    send_checked,
)
from .entity import DuetEntity, add_dynamic
from .hardware import message_box
from .model import as_list

# key, name, G-code, states it is allowed in (None: any), enabled by default, icon
_FIXED = (
    ("pause", "Pause", "M25", PAUSE_STATES, True, "mdi:pause"),
    ("resume", "Resume", "M24", RESUME_STATES, True, "mdi:play"),
    ("cancel", "Cancel", "M0", CANCEL_STATES, True, "mdi:stop"),
    ("home-all", "Home all", "G28", HOME_STATES, True, "mdi:home"),
    # Anything may be needed after an emergency stop, whatever state it left behind,
    # and the stop itself must never be refused. Both are off until asked for.
    ("emergency-stop", "Emergency stop", "M112", None, False, "mdi:alert-octagon"),
    ("reset-after-emergency-stop", "Reset after emergency stop", "M999", {"halted"}, False, "mdi:restart-alert"),
)
_HOMEABLE_AXES = ("X", "Y", "Z")


def quotable(name: str) -> bool:
    """Whether ``name`` can sit inside a G-code string: no quote, no line break."""
    return bool(name) and not any(c in name for c in '"\r\n;')


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entry_id = config_entry.entry_id

    def add(discover):
        add_dynamic(coordinator, config_entry, async_add_entities, discover)

    def fixed():
        for key, name, gcode, allowed, enabled, icon in _FIXED:
            yield key, lambda k=key, n=name, g=gcode, a=allowed, e=enabled, i=icon: DuetGcodeButton(
                coordinator, n, f"button-{k}-{entry_id}", g, a, e, i
            )

    def axes():
        for axis in as_list(coordinator.get_sensor_state("status.move.axes")):
            letter = axis.get("letter") if isinstance(axis, dict) else None
            if letter in _HOMEABLE_AXES:
                yield f"home-{letter}", lambda l=letter: DuetGcodeButton(
                    coordinator, f"Home {l}", f"button-home-{l.lower()}-{entry_id}",
                    f"G28 {l}", HOME_STATES, True, "mdi:home",
                )

    def message():
        yield "acknowledge", lambda: DuetAcknowledgeButton(
            coordinator, "Acknowledge message", f"button-acknowledge-{entry_id}"
        )

    def macros():
        for name in coordinator.macros:
            if quotable(name):
                yield name, lambda n=name: DuetMacroButton(
                    coordinator, n, f"macro-{n}-{entry_id}"
                )

    for discover in (fixed, axes, message, macros):
        add(discover)


class DuetGcodeButton(DuetEntity, ButtonEntity):
    """Sends one fixed G-code, when the printer's state allows it."""

    def __init__(self, coordinator, name, unique_id, gcode, allowed, enabled, icon) -> None:
        super().__init__(coordinator, name, unique_id)
        self._gcode = gcode
        self._allowed = allowed
        self._action = name.lower()
        self._attr_entity_registry_enabled_default = enabled
        self._attr_icon = icon

    async def async_press(self) -> None:
        await send_checked(self.coordinator, self._gcode, self._allowed, self._action)


class DuetAcknowledgeButton(DuetEntity, ButtonEntity):
    """Closes the M291 message box, when there is one."""

    _attr_icon = "mdi:message-check"

    async def async_press(self) -> None:
        await fresh_status(self.coordinator)
        if message_box(self.coordinator.get_sensor_state("status.state")) is None:
            raise ServiceValidationError(
                f"{self.coordinator.config_entry.title} has no message waiting"
            )
        await send(self.coordinator, "M292")
        await self.coordinator.async_request_refresh()


class DuetMacroButton(DuetEntity, ButtonEntity):
    """Runs one macro from the top level of ``0:/macros``. Off by default: there are many."""

    _attr_icon = "mdi:script-text-play"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, file_name: str, unique_id: str) -> None:
        stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        super().__init__(coordinator, f"Macro {stem}", unique_id)
        self._file_name = file_name

    @property
    def available(self) -> bool:
        return super().available and self._file_name in self.coordinator.macros

    async def async_press(self) -> None:
        await send_checked(
            self.coordinator,
            f'M98 P"{MACRO_DIRECTORY}/{self._file_name}"',
            HOME_STATES,
            f"run {self._file_name}",
        )
