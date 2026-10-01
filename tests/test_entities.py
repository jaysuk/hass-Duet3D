"""The fans, boards, storage, network, filament monitor, job and message entities."""
import pytest

from homeassistant.helpers import entity_registry as er

from fake_duet import DOMAIN, by_unique_id_prefix, has_entity as has, refresh, setup_entry, state_of as state


async def _slow_refresh(hass, entry):
    """Boards, network and storage are only polled once a minute; make them due."""
    hass.data[DOMAIN][entry.entry_id]["coordinator"]._slow_fetched_at = None
    await refresh(hass, entry)


# --- heaters ---------------------------------------------------------------


async def test_heater_state_and_power_follow_the_heater_each_role_uses(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "tool-0-state").state == "active"  # heater 2
    assert state(hass, entry, "tool-1-state").state == "standby"  # heater 1
    assert state(hass, entry, "tool-0-power").state == "50.0"
    assert state(hass, entry, "bed-power").state == "25.0"
    assert state(hass, entry, "bed-state").state == "active"


async def test_heater_fault_is_visible_as_a_state(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fake_duet.model["heat"]["heaters"][2]["state"] = "fault"
    await refresh(hass, entry)
    assert state(hass, entry, "tool-0-state").state == "fault"


# --- fans and flow ---------------------------------------------------------


async def test_fans_are_percent_with_rpm_only_when_there_is_a_tacho(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    part = state(hass, entry, "fan-0")
    assert part.state == "50.0"
    assert part.attributes["unit_of_measurement"] == "%"
    assert part.attributes["requested"] == 60.0
    assert part.attributes["rpm"] is None  # rpm -1 = no tacho
    assert part.attributes["friendly_name"] == "Voron Part Cooling Fan speed"
    unnamed = state(hass, entry, "fan-2")
    assert unnamed.attributes["friendly_name"] == "Voron Fan 2 speed"
    assert unnamed.state == "100.0"
    assert unnamed.attributes["rpm"] == 3000
    assert not has(hass, entry, "fan-1")  # undefined slot


async def test_fan_speed_follows_the_printer(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fake_duet.model["fans"][0]["actualValue"] = 0.0
    await refresh(hass, entry)
    assert state(hass, entry, "fan-0").state == "0.0"


async def test_extruder_flow_is_the_m221_factor_per_extruder(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "flow-0").state == "100.0"
    assert state(hass, entry, "flow-1").state == "95.0"


# --- boards ----------------------------------------------------------------


async def test_board_sensors_for_main_and_expansion_boards(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "board-0-mcu_temp").state == "40.0"
    assert state(hass, entry, "board-0-mcu_temp").attributes["friendly_name"] == "Voron Main board MCU temperature"
    assert state(hass, entry, "board-124-mcu_temp").state == "44.0"
    assert state(hass, entry, "board-124-v_in").state == "23.9"
    assert state(hass, entry, "board-124-mcu_temp").attributes["friendly_name"] == "Voron SB2040 MCU temperature"
    # null on both boards, so no sensor
    assert not has(hass, entry, "board-0-v_12")
    assert not has(hass, entry, "board-124-v_12")


async def test_free_ram_exists_but_is_off_until_enabled(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"board-0-free_ram-{entry.entry_id}")
    assert entity_id
    assert registry.async_get(entity_id).disabled_by is not None


async def test_expansion_board_connected_binary_sensor(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "board-124-connected", "binary_sensor").state == "on"
    assert not has(hass, entry, "board-0-connected", "binary_sensor")  # main board has no state

    fake_duet.model["boards"][1]["state"] = "unknown"
    await _slow_refresh(hass, entry)
    lost = state(hass, entry, "board-124-connected", "binary_sensor")
    assert lost.state == "off"
    assert lost.attributes["state"] == "unknown"


async def test_board_sensors_appear_when_an_expansion_board_is_added_later(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fake_duet.model["boards"].append(
        {"canAddress": 121, "name": "Duet 3 Tool board", "shortName": "TOOL1LC", "state": "running",
         "mcuTemp": {"current": 30.0}, "vIn": {"current": 24.0}, "v12": None, "freeRam": 1}
    )
    await _slow_refresh(hass, entry)
    assert state(hass, entry, "board-121-mcu_temp").state == "30.0"


# --- network and storage ---------------------------------------------------


async def test_ip_and_wifi_signal(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "interface-0-ip").state == "192.168.1.5"
    signal = state(hass, entry, "interface-0-signal")
    assert signal.state == "-55"
    assert signal.attributes["unit_of_measurement"] == "dBm"


async def test_no_signal_sensor_until_wifi_reports_a_signal(hass, fake_duet):
    """The test board's Wi-Fi interface reports ``signal: null``."""
    fake_duet.model["network"]["interfaces"][0]["signal"] = None
    entry = await setup_entry(hass, fake_duet)
    assert has(hass, entry, "interface-0-ip")
    assert not has(hass, entry, "interface-0-signal")

    fake_duet.model["network"]["interfaces"][0]["signal"] = -70
    await _slow_refresh(hass, entry)
    assert state(hass, entry, "interface-0-signal").state == "-70"


async def test_storage_free_space_for_mounted_volumes_only(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    free = state(hass, entry, "volume-0-free")
    assert free.attributes["unit_of_measurement"] == "GB"
    assert float(free.state) == pytest.approx(3.0)
    assert free.attributes["capacity"] == 4_000_000_000
    assert not has(hass, entry, "volume-1-free")  # empty slot


async def test_storage_goes_unavailable_when_the_card_is_removed(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fake_duet.model["volumes"][0]["mounted"] = False
    await _slow_refresh(hass, entry)
    assert state(hass, entry, "volume-0-free").state == "unavailable"

    fake_duet.model["volumes"][0].update(mounted=True, freeSpace=1_000_000_000)
    await _slow_refresh(hass, entry)
    assert float(state(hass, entry, "volume-0-free").state) == pytest.approx(1.0)


# --- filament monitors -----------------------------------------------------


async def test_filament_monitor_status_and_presence(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    status = state(hass, entry, "monitor-0-status")
    assert status.state == "ok"
    assert status.attributes["type"] == "simple"
    assert status.attributes["filament_present"] is True
    assert state(hass, entry, "monitor-0-present", "binary_sensor").state == "on"
    # extruder 1 has no monitor
    assert not has(hass, entry, "monitor-1-status")
    assert not has(hass, entry, "monitor-1-present", "binary_sensor")


async def test_runout_turns_presence_off_and_reports_the_status(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fake_duet.model["sensors"]["filamentMonitors"][0].update(filamentPresent=False, status="noFilament")
    await refresh(hass, entry)
    assert state(hass, entry, "monitor-0-present", "binary_sensor").state == "off"
    assert state(hass, entry, "monitor-0-status").state == "noFilament"


async def test_monitor_that_cannot_tell_presence_gets_status_only(hass, fake_duet):
    """e.g. a laser monitor: ``filamentPresent`` is omitted when unknown."""
    fake_duet.model["sensors"]["filamentMonitors"][1] = {"enableMode": 1, "status": "ok", "type": "laser"}
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "monitor-1-status").attributes["type"] == "laser"
    assert not has(hass, entry, "monitor-1-present", "binary_sensor")


async def test_printer_without_filament_monitors_gets_none(hass, fake_duet):
    fake_duet.model["sensors"]["filamentMonitors"] = []
    entry = await setup_entry(hass, fake_duet)
    assert not by_unique_id_prefix(hass, "monitor-")


async def test_monitor_configured_later_gets_entities(hass, fake_duet):
    fake_duet.model["sensors"]["filamentMonitors"] = []
    entry = await setup_entry(hass, fake_duet)
    fake_duet.model["sensors"]["filamentMonitors"] = [
        {"enableMode": 1, "status": "ok", "type": "simple", "filamentPresent": True}
    ]
    await refresh(hass, entry)
    assert state(hass, entry, "monitor-0-status").state == "ok"
    assert state(hass, entry, "monitor-0-present", "binary_sensor").state == "on"


# --- job, messages and machine state --------------------------------------


async def test_job_and_override_sensors(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "Speed Factor").state == "125.0"
    assert state(hass, entry, "Filament Time Remaining").state == "1.5"
    assert state(hass, entry, "Warm-up Duration").state == "2.0"
    assert state(hass, entry, "Last File Name").state == "benchy_old"
    assert state(hass, entry, "Layer Height").state == "0.2"
    assert state(hass, entry, "Object Height").state == "48.0"
    assert state(hass, entry, "Generated By").state == "PrusaSlicer 2.8"


async def test_idle_printer_values_are_unknown_not_zero(hass, fake_duet):
    """As on the real board when idle: nulls and a layer height of 0."""
    job = fake_duet.model["job"]
    job["timesLeft"]["filament"] = None
    job["warmUpDuration"] = None
    job["lastFileName"] = None
    job["file"].update(layerHeight=0, generatedBy=None)
    entry = await setup_entry(hass, fake_duet)
    for name in ("Filament Time Remaining", "Warm-up Duration", "Last File Name", "Layer Height", "Generated By"):
        assert state(hass, entry, name).state == "unknown", name


async def test_display_message_and_message_box(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "Display Message").state == "unknown"
    assert state(hass, entry, "Message Box").state == "unknown"

    fake_duet.model["state"]["displayMessage"] = "Heating"
    fake_duet.model["state"]["messageBox"] = {"message": "Change filament", "title": "Filament", "mode": 1, "seq": 3}
    await refresh(hass, entry)
    assert state(hass, entry, "Display Message").state == "Heating"
    box = state(hass, entry, "Message Box")
    assert box.state == "Change filament"
    assert box.attributes["title"] == "Filament"
    assert box.attributes["seq"] == 3

    fake_duet.model["state"]["messageBox"] = None
    await refresh(hass, entry)
    assert state(hass, entry, "Message Box").state == "unknown"


async def test_homed_is_on_only_when_every_axis_is(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    homed = state(hass, entry, "Homed", "binary_sensor")
    assert homed.state == "on"
    assert homed.attributes["X"] is True

    fake_duet.model["move"]["axes"][1]["homed"] = False  # e.g. after M18/motor off
    await refresh(hass, entry)
    after = state(hass, entry, "Homed", "binary_sensor")
    assert after.state == "off"
    assert after.attributes["Y"] is False


async def test_startup_error_is_a_problem_with_the_message(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state(hass, entry, "Startup error", "binary_sensor").state == "off"

    fake_duet.model["state"]["startupError"] = {
        "file": "config.g", "line": 115, "message": "in file config.g line 115: M955: parameter 'P' too high",
    }
    await refresh(hass, entry)
    error = state(hass, entry, "Startup error", "binary_sensor")
    assert error.state == "on"
    assert error.attributes["line"] == 115
    assert "M955" in error.attributes["message"]


async def test_unique_ids_of_existing_sensors_are_unchanged(hass, fake_duet):
    """History and dashboards survive: nothing existing was renamed."""
    entry = await setup_entry(hass, fake_duet)
    for prefix, domain in (
        ("Progress", "sensor"), ("Time Remaining", "sensor"), ("Current Tool", "sensor"),
        ("Filament Extruded", "sensor"), ("Printing", "binary_sensor"), ("extruder-0", "sensor"),
        ("bed-current", "sensor"), ("bed-active", "sensor"),
    ):
        assert has(hass, entry, prefix, domain), prefix
