"""Helpers shared by the entity platforms."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback


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
