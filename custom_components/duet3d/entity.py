"""Helpers shared by the entity platforms."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity


def add_dynamic(
    coordinator,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discover: Callable[[], Iterable[tuple[Any, Callable[[], Any]]]],
) -> None:
    """Add entities now and whenever the printer later reports more.

    ``discover`` returns ``(key, factory)`` pairs for everything the object model
    currently describes. A factory is only called the first time its key is seen and
    returns one entity or a list of them. Entities for things that later disappear are not removed: they report
    unavailable through their own ``available`` check, and come back if the thing
    does.
    """
    known: set[Any] = set()

    @callback
    def _add() -> None:
        new = []
        for key, factory in discover():
            if key in known:
                continue
            known.add(key)
            made = factory()
            new.extend(made if isinstance(made, list) else [made])
        if new:
            async_add_entities(new)

    config_entry.async_on_unload(coordinator.async_add_listener(_add))
    _add()


class DuetEntity(CoordinatorEntity):
    """Base of the entities that are not sensors: named like the sensors, on the printer's device.

    ``name`` is appended to the printer's name (as the sensors do, so entity ids
    read ``<platform>.<printer>_<name>``) and ``unique_id`` must stay stable.
    """

    def __init__(self, coordinator, name: str, unique_id: str) -> None:
        super().__init__(coordinator)
        self._attr_name = f"{coordinator.device_info['name']} {name}"
        self._attr_unique_id = unique_id

    @property
    def device_info(self):
        return self.coordinator.device_info

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success
