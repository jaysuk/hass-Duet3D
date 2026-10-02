"""End-to-end: the real integration polling a fake standalone Duet."""
import pytest  # noqa: F401
from homeassistant.helpers import entity_registry as er

from fake_duet import (
    DOMAIN,
    by_unique_id_prefix as _by_unique_id_prefix,
    has_entity as _has,
    entry_for as _entry,
    refresh as _refresh,
    setup_entry as _setup,
    state_of as _state,
)


async def test_one_sensor_per_extruder_with_stable_unique_ids(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    extruders = _by_unique_id_prefix(hass, "extruder-")
    assert sorted(e.unique_id for e in extruders) == [
        f"extruder-0-{entry.entry_id}",
        f"extruder-1-{entry.entry_id}",
    ]


async def test_loaded_filament_is_the_state_and_attributes_describe_the_extruder(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"extruder-1-{entry.entry_id}")
    state = hass.states.get(entity_id)
    assert state.state == "PETG"
    assert state.attributes["name"] == "PETG"
    assert state.attributes["type"] == "PETG"
    assert state.attributes["extruder"] == 1
    assert state.attributes["tools"] == [1]
    assert state.attributes["filament_diameter"] == 1.75
    assert state.attributes["active"] is False


async def test_nothing_loaded_has_empty_name_and_unknown_state(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"extruder-0-{entry.entry_id}")
    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert state.attributes["name"] == ""


async def test_selected_tool_marks_only_its_extruder_active(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 1
    entry = await _setup(hass, fake_duet)
    registry = er.async_get(hass)
    active = {
        n: hass.states.get(
            registry.async_get_entity_id("sensor", DOMAIN, f"extruder-{n}-{entry.entry_id}")
        ).attributes["active"]
        for n in (0, 1)
    }
    assert active == {0: False, 1: True}


async def test_filament_extruded_and_current_tool_sensors(hass, fake_duet):
    # a job already running when Home Assistant starts is followed from its first reading
    fake_duet.model["job"]["rawExtrusion"] = 1234.567
    fake_duet.model["state"]["currentTool"] = 0
    fake_duet.model["state"]["status"] = "processing"
    entry = await _setup(hass, fake_duet)
    registry = er.async_get(hass)
    extruded = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"Filament Extruded-{entry.entry_id}")
    )
    assert extruded.state == "1234.57"
    assert extruded.attributes["unit_of_measurement"] == "mm"
    tool = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"Current Tool-{entry.entry_id}")
    )
    assert tool.state == "0"


async def test_a_poll_is_one_full_depth_request_per_top_level_key(hass, fake_duet):
    """rr_model truncates nested objects unless asked for depth, and a standalone
    board is slow, so poll a few whole objects rather than one request per sensor."""
    fast = [
        ("state", "d99vn"),
        ("job", "d99vn"),
        ("heat", "d99vn"),
        ("tools", "d99vn"),
        ("move", "d99vn"),
        ("fans", "d99vn"),
        # not all of ``sensors``: only filament monitors need to be prompt
        ("sensors.filamentMonitors", "d99vn"),
    ]
    slow = [("boards", "d99vn"), ("network", "d99vn"), ("volumes", "d99vn"), ("ledStrips", "d99vn")]

    entry = await _setup(hass, fake_duet)
    assert fake_duet.requests == fast + slow  # first poll gets everything

    # boards, network and storage barely change, so the next polls skip them...
    fake_duet.requests.clear()
    await _refresh(hass, entry)
    assert fake_duet.requests == fast

    # ...until they are due again
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator._slow_fetched_at = None
    fake_duet.requests.clear()
    await _refresh(hass, entry)
    assert fake_duet.requests == fast + slow


async def test_device_info_has_scalar_firmware_and_board_not_lists(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert coordinator.firmware_version == "3.5.4"
    assert coordinator.board_model == "Duet 3 Mini 5+"


async def test_temperature_sensors_follow_the_tools_heaters_not_the_tool_number(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    # tool 0 is heater 2 and tool 1 is heater 1 in MODEL
    assert _state(hass, entry, "tool-0-current").state == "200.0"
    assert _state(hass, entry, "tool-0-active").state == "210.0"
    assert _state(hass, entry, "tool-0-standby").state == "150.0"
    assert _state(hass, entry, "tool-1-current").state == "201.0"
    assert _state(hass, entry, "tool-0-current").attributes["heater"] == 2
    assert _state(hass, entry, "tool-0-current").attributes["tool"] == 0


async def test_bed_comes_from_the_mapping_and_keeps_its_old_unique_id(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    assert _state(hass, entry, "bed-current").state == "60.0"
    assert _state(hass, entry, "bed-active").state == "65.0"
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"bed-standby-{entry.entry_id}") is None


async def test_nothing_is_created_for_a_heater_that_nothing_uses(hass, fake_duet):
    """No bed configured = no bed sensors; no chamber mapped = no chamber sensors."""
    fake_duet.model["heat"]["bedHeaterMapping"] = [[]]
    entry = await _setup(hass, fake_duet)
    assert not _by_unique_id_prefix(hass, "bed")
    assert not _by_unique_id_prefix(hass, "chamber")
    assert _by_unique_id_prefix(hass, "tool-0")


async def test_config_that_still_has_number_of_tools_and_bed_is_ignored(hass, fake_duet):
    """Entries created before tools came from the model carry these keys. They must
    not limit or add sensors."""
    entry = await _setup(hass, fake_duet, _entry(fake_duet, number_of_tools=5, bed=False))
    assert _state(hass, entry, "bed-current").state == "60.0"
    assert not _by_unique_id_prefix(hass, "tool-5")
    assert not _by_unique_id_prefix(hass, "tool-2")


async def test_chamber_is_exposed_when_the_model_maps_one(hass, fake_duet):
    fake_duet.model["heat"]["chamberHeaterMapping"] = [[1]]
    entry = await _setup(hass, fake_duet)
    assert _state(hass, entry, "chamber-current").state == "201.0"


async def test_3_6_style_bed_key_still_works(hass, fake_duet):
    del fake_duet.model["heat"]["bedHeaterMapping"]
    del fake_duet.model["heat"]["chamberHeaterMapping"]
    fake_duet.model["heat"]["bedHeaters"] = [0, -1, -1, -1]
    fake_duet.model["heat"]["chamberHeaters"] = [-1, -1, -1, -1]
    entry = await _setup(hass, fake_duet)
    assert _state(hass, entry, "bed-current").state == "60.0"
    assert not _by_unique_id_prefix(hass, "chamber")


async def test_legacy_numbered_temperature_sensors_are_removed_but_bed_is_kept(hass, fake_duet):
    entry = _entry(fake_duet)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = [
        registry.async_get_or_create("sensor", DOMAIN, f"{n}-{t}-{entry.entry_id}", config_entry=entry)
        for n in (1, 2)
        for t in ("current", "active", "standby")
    ]
    bed = registry.async_get_or_create("sensor", DOMAIN, f"bed-current-{entry.entry_id}", config_entry=entry)
    other = registry.async_get_or_create("sensor", DOMAIN, f"Progress-{entry.entry_id}", config_entry=entry)

    await _setup(hass, fake_duet, entry)

    assert all(registry.async_get(e.entity_id) is None for e in legacy)
    assert registry.async_get(bed.entity_id) is not None
    assert registry.async_get(other.entity_id) is not None


async def test_tool_that_appears_later_gets_sensors_and_a_vanished_one_goes_unavailable(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    fake_duet.model["heat"]["heaters"].append({"current": 33.0, "active": 0.0, "standby": 0.0})
    fake_duet.model["tools"].append(
        {"number": 2, "extruders": [], "heaters": [3], "active": [0.0], "standby": [0.0]}
    )
    await _refresh(hass, entry)
    assert _state(hass, entry, "tool-2-current").state == "33.0"

    fake_duet.model["tools"].pop()  # M563 P2 D-1 etc.
    await _refresh(hass, entry)
    assert _state(hass, entry, "tool-2-current").state == "unavailable"


async def test_temperature_follows_the_printer_on_the_next_poll(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    fake_duet.model["heat"]["heaters"][2]["current"] = 215.5
    await _refresh(hass, entry)
    assert _state(hass, entry, "tool-0-current").state == "215.5"


async def test_faulted_temperature_sensor_is_unknown(hass, fake_duet):
    fake_duet.model["heat"]["heaters"][2]["current"] = -273.1
    entry = await _setup(hass, fake_duet)
    assert _state(hass, entry, "tool-0-current").state == "unknown"


async def test_real_3_7_0_rc2_model_end_to_end(hass, fake_duet):
    """The model captured from a BTT Kraken: tool 0 on heater 1, bed 0, chamber 2."""
    import json
    from pathlib import Path

    fake_duet.model.clear()
    fake_duet.model.update(
        json.loads((Path(__file__).parent / "fixtures" / "rrf_3_7_0_rc2_standalone.json").read_text())
    )
    fake_duet.model["boards"] = [{"firmwareVersion": "3.7.0-rc.2", "name": "BTT Kraken V1 STM32H723"}]
    entry = await _setup(hass, fake_duet)

    heaters = fake_duet.model["heat"]["heaters"]
    assert float(_state(hass, entry, "tool-0-current").state) == heaters[1]["current"]
    assert float(_state(hass, entry, "tool-0-standby").state) == 175.0
    assert float(_state(hass, entry, "bed-current").state) == heaters[0]["current"]
    assert float(_state(hass, entry, "chamber-current").state) == heaters[2]["current"]
    assert _state(hass, entry, "extruder-0").state == "unknown"
    assert _state(hass, entry, "Current Tool").state == "0"
    assert float(_state(hass, entry, "Filament Extruded").state) == 0
    # nothing leftover from the old numbering
    assert not _by_unique_id_prefix(hass, "1-")
    assert not _by_unique_id_prefix(hass, "2-")


async def test_state_follows_the_printer_on_the_next_poll(hass, fake_duet):
    entry = await _setup(hass, fake_duet)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"extruder-0-{entry.entry_id}")
    assert hass.states.get(entity_id).state == "unknown"

    # M701 S"PLA" on the printer, then the coordinator's next poll
    fake_duet.model["move"]["extruders"][0]["filament"] = "PLA"
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "PLA"


def _watch_state_changes(hass):
    from homeassistant.const import EVENT_STATE_CHANGED

    changes = []
    hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        lambda event: changes.append((event.data["entity_id"], event.data["new_state"].state)),
    )
    return changes


async def test_a_missed_poll_changes_no_entity(hass, fake_duet):
    """A Wi-Fi board drops the odd request. Every entity going unavailable and coming
    back is one Activity entry per entity, so a single failed poll must be invisible."""
    entry = await _setup(hass, fake_duet)
    changes = _watch_state_changes(hass)

    fake_duet.model["move"]["extruders"][0]["filament"] = "PLA"
    fake_duet.fail = True
    await _refresh(hass, entry)  # one failed poll: nothing changes yet...
    assert changes == []
    assert _state(hass, entry, "extruder-0").state == "unknown"

    fake_duet.fail = False
    await _refresh(hass, entry)  # ...and the next good poll is just an ordinary update
    assert changes == [("sensor.voron_extruder_0", "PLA")]


async def test_a_printer_that_stays_unreachable_goes_unavailable_and_recovers(hass, fake_duet):
    from custom_components.duet3d.const import TOLERATED_FAILED_POLLS

    entry = await _setup(hass, fake_duet)
    fake_duet.fail = True
    for _ in range(TOLERATED_FAILED_POLLS):
        await _refresh(hass, entry)
        assert _state(hass, entry, "Current Tool").state == "-1"

    await _refresh(hass, entry)
    assert _state(hass, entry, "Current Tool").state == "unavailable"

    fake_duet.fail = False
    await _refresh(hass, entry)
    assert _state(hass, entry, "Current Tool").state == "-1"


async def test_failures_do_not_accumulate_across_good_polls(hass, fake_duet):
    from custom_components.duet3d.const import TOLERATED_FAILED_POLLS

    entry = await _setup(hass, fake_duet)
    for _ in range(3):  # more failures in total than tolerated, but never in a row
        fake_duet.fail = True
        for _ in range(TOLERATED_FAILED_POLLS):
            await _refresh(hass, entry)
        fake_duet.fail = False
        await _refresh(hass, entry)
        assert _state(hass, entry, "Current Tool").state == "-1"


async def test_setup_retries_later_when_the_printer_is_down(hass, fake_duet):
    from homeassistant.config_entries import ConfigEntryState

    fake_duet.fail = True
    entry = _entry(fake_duet)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_extruder_appearing_later_gets_a_sensor(hass, fake_duet):
    """The firmware can gain an extruder (M584 in config-override, hot-plugged expansion)."""
    entry = await _setup(hass, fake_duet)
    fake_duet.model["move"]["extruders"].append({"filament": "ASA", "filamentDiameter": 1.75})
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"extruder-2-{entry.entry_id}")
