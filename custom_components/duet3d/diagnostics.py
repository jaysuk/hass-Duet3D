"""Diagnostics download: enough to debug a report, nothing that identifies the network."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Keys redacted wherever they appear, in the config entry and in the object model.
TO_REDACT = {
    # credentials and addresses
    "password",
    "host",
    "base_url",
    "actualIP",
    "configuredIP",
    "gateway",
    "subnet",
    "dnsServer",
    "ip",
    "mac",
    "ssid",
    # names that identify the board or the network
    "uniqueId",
    "hostname",
    "title",
    # file names say what is being printed
    "fileName",
    "lastFileName",
    "file_name",
    "thumbnails",
}
# ``network.name`` is the board's hostname, but ``name`` elsewhere is a fan or tool
# label, so that one is redacted by position, below.


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    data = coordinator.data or {}
    status = data.get("status")

    dump: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "mode": "standalone" if coordinator.config_entry.data.get("standalone") else "sbc",
        "firmware": coordinator.firmware_version,
        "board": coordinator.board_model,
        "poll": {
            "idle_interval_s": coordinator.interval,
            "printing_interval_s": coordinator.printing_interval,
            "current_interval_s": (
                coordinator.update_interval.total_seconds() if coordinator.update_interval else None
            ),
            "failed_polls_in_a_row": coordinator._failed_polls,
            "last_update_success": coordinator.last_update_success,
            "last_read_time": (
                data["last_read_time"].isoformat() if data.get("last_read_time") else None
            ),
            "printer_online": coordinator.printer_online,
        },
        "heater_roles": coordinator.heater_roles,
        "hardware": coordinator.hardware,
        "macro_count": len(coordinator.macros),
        "status": status,
    }
    dump = async_redact_data(dump, TO_REDACT)

    # redact the hostname by position, then everything that is the entry's own address
    network = dump["status"].get("network") if isinstance(dump.get("status"), dict) else None
    if isinstance(network, dict) and "name" in network:
        network["name"] = "**REDACTED**"
    if entry.unique_id:
        dump["entry"]["unique_id"] = "**REDACTED**"
    return dump
