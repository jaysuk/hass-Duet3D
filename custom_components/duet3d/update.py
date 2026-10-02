"""Firmware version: what the board runs and the newest stable release. Informational only.

Installing is not offered: updating firmware is done with Duet Web Control or the
board's own tools, and a failed flash from a home automation is not a risk worth
taking. The entity is off by default because it asks GitHub for the latest release
(at most every 6 hours); nothing is requested unless it is enabled.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp

from homeassistant.components.update import UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuetDataUpdateCoordinator
from .const import DOMAIN, FIRMWARE_RELEASES_URL
from .entity import DuetEntity

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=6)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    async_add_entities([DuetFirmwareUpdate(coordinator, f"firmware-{config_entry.entry_id}")])


class DuetFirmwareUpdate(DuetEntity, UpdateEntity):
    """The board's firmware against the newest stable RepRapFirmware release."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_title = "RepRapFirmware"

    def __init__(self, coordinator: DuetDataUpdateCoordinator, unique_id: str) -> None:
        super().__init__(coordinator, "Firmware", unique_id)
        self._latest: str | None = None
        self._release_url: str | None = None

    async def async_added_to_hass(self) -> None:
        """First reading, only once the entity is enabled and added.

        ``update_before_add`` would read before the registry says the entity is
        disabled, which is a request nobody asked for.
        """
        await super().async_added_to_hass()
        self.async_schedule_update_ha_state(True)

    @property
    def should_poll(self) -> bool:
        # CoordinatorEntity makes this a property that returns False, which overrides
        # ``_attr_should_poll``. The latest release is read by ``async_update``.
        return True

    @property
    def installed_version(self) -> str | None:
        version = self.coordinator.get_sensor_state("status.boards[0].firmwareVersion")
        return version if isinstance(version, str) else self.coordinator.firmware_version

    @property
    def latest_version(self) -> str | None:
        # Without a reading of the latest release, claim to be up to date rather than
        # unknown: there is nothing to act on either way.
        return self._latest or self.installed_version

    @property
    def release_url(self) -> str | None:
        return self._release_url

    async def async_update(self) -> None:
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(15):
                async with session.get(
                    FIRMWARE_RELEASES_URL, headers={"Accept": "application/vnd.github+json"}
                ) as response:
                    response.raise_for_status()
                    release = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            _LOGGER.debug("Could not read the latest firmware release: %s", exc)
            return
        tag = release.get("tag_name") if isinstance(release, dict) else None
        if isinstance(tag, str) and tag:
            self._latest = tag.lstrip("v")
            self._release_url = release.get("html_url")
