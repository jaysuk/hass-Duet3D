"""Fans: one entity per defined fan that takes a speed, set with ``M106``.

A thermostatic fan gets no control: the firmware ignores ``M106 S`` for it and drives
it from temperature (LocalFan.cpp), so a control would only pretend. Its speed sensor
stays.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuetDataUpdateCoordinator
from .const import DOMAIN
from .controls import send_checked
from .entity import DuetEntity, add_dynamic

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    _remove_thermostatic_controls(hass, config_entry, coordinator)

    def fans():
        for key, fan in coordinator.hardware["fans"].items():
            if fan["thermostatic"]:
                continue
            yield key, lambda k=key, f=fan: DuetFan(
                coordinator, k, f["index"], f["label"], config_entry.entry_id
            )

    add_dynamic(coordinator, config_entry, async_add_entities, fans)


def _remove_thermostatic_controls(
    hass: HomeAssistant, config_entry: ConfigEntry, coordinator: DuetDataUpdateCoordinator
) -> None:
    """Delete controls that earlier versions made for fans that turned out to be thermostatic."""
    registry = er.async_get(hass)
    for fan in coordinator.hardware["fans"].values():
        if not fan["thermostatic"]:
            continue
        unique_id = f"fan-control-{fan['index']}-{config_entry.entry_id}"
        if entity_id := registry.async_get_entity_id("fan", DOMAIN, unique_id):
            _LOGGER.info("Removing %s: fan %s is thermostatic, so it ignores a set speed", entity_id, fan["index"])
            registry.async_remove(entity_id)


class DuetFan(DuetEntity, FanEntity):
    """A fan. The percentage shown is what was asked for, as in Duet Web Control."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 100
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator, key: str, index: int, label: str, entry_id: str) -> None:
        super().__init__(coordinator, f"{label} control", f"fan-control-{index}-{entry_id}")
        self._key = key
        self._index = index

    def _fan(self) -> dict | None:
        return self.coordinator.hardware["fans"].get(self._key)

    @property
    def available(self) -> bool:
        fan = self._fan()
        return super().available and fan is not None and not fan["thermostatic"]

    @property
    def percentage(self) -> int | None:
        fan = self._fan()
        requested = fan["requested"] if fan else None
        return None if requested is None else round(requested)

    @property
    def is_on(self) -> bool | None:
        percentage = self.percentage
        return None if percentage is None else percentage > 0

    async def _set(self, percentage: int) -> None:
        await send_checked(
            self.coordinator, f"M106 P{self._index} S{percentage / 100:.2f}", None, "set fan",
            wait=True,
        )

    async def async_set_percentage(self, percentage: int) -> None:
        await self._set(percentage)

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        await self._set(100 if percentage is None else percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(0)
