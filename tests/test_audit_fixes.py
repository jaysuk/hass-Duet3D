"""Regression tests for the findings of the 0.4.0 audit (see AUDIT_FIX_PLAN.md).

The fake printer refuses what the firmware would (``server.refused``), so a test that
sends a code the real board would ignore fails by itself.
"""
import base64
import io
import json

import pytest
import voluptuous as vol
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.camera import async_get_image
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.duet3d import controls
from custom_components.duet3d.detect import InvalidAuth, detect_standalone
from custom_components.duet3d.events import extruded_reading
from custom_components.duet3d.hardware import build_fans
from custom_components.duet3d.jobinfo import pick_thumbnail, progress_percent
from custom_components.duet3d.update import DuetFirmwareUpdate
from fake_duet import (
    DOMAIN,
    by_unique_id_prefix,
    collect_events,
    entry_for,
    has_entity,
    job_poll,
    refresh,
    setup_entry,
    setup_sbc_entry,
    state_of,
    step,
)


def extruded_now(hass, entry):
    """The Filament Extruded reading as a number (an int and a float render differently)."""
    return float(state_of(hass, entry, "Filament Extruded").state)


def coordinator_of(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


def status(server, value):
    server.model["state"]["status"] = value


async def call(hass, service, data=None):
    await hass.services.async_call(DOMAIN, service, data or {}, blocking=True)


async def press(hass, entry, unique_id):
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{unique_id}-{entry.entry_id}")
    assert entity_id
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


@pytest.fixture
def fast_cancel(monkeypatch):
    """No waiting between polls while a cancel waits for the pause."""
    monkeypatch.setattr(controls, "CANCEL_POLL_SECONDS", 0)


# --- A1: cancel only works on a paused print -----------------------------------


async def test_cancel_a_running_job_pauses_first(hass, fake_duet, fast_cancel):
    fake_duet.emulate_job_control = True
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)
    await step(hass, entry, fake_duet, "processing", extruded=10)
    await call(hass, "cancel")
    await refresh(hass, entry)
    assert fake_duet.gcodes == ["M25", "M0"]
    assert fake_duet.refused == []
    assert fake_duet.model["state"]["status"] == "idle"
    assert [event["type"] for event in events] == ["job_started", "job_paused", "job_cancelled"]


async def test_cancel_a_simulation_pauses_first_and_fires_no_event(hass, fake_duet, fast_cancel):
    fake_duet.emulate_job_control = True
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)
    await step(hass, entry, fake_duet, "simulating", extruded=10)
    await call(hass, "cancel")
    await refresh(hass, entry)
    assert fake_duet.gcodes == ["M25", "M0"]
    assert fake_duet.refused == []
    assert events == []


async def test_cancel_when_idle_is_refused_with_nothing_sent(hass, fake_duet, fast_cancel):
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="idle"):
        await call(hass, "cancel")
    assert fake_duet.gcodes == []


async def test_cancel_waits_for_a_pause_in_progress(hass, fake_duet, fast_cancel):
    """``pausing`` has nothing to send yet; it turns to ``paused`` on the next poll."""
    fake_duet.emulate_job_control = True
    await setup_entry(hass, fake_duet)
    status(fake_duet, "pausing")
    await call(hass, "cancel")
    assert fake_duet.gcodes == ["M0"]
    assert fake_duet.refused == []


async def test_cancel_gives_up_if_the_pause_never_finishes(hass, fake_duet, monkeypatch):
    monkeypatch.setattr(controls, "CANCEL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(controls, "CANCEL_PAUSE_TIMEOUT", 0.05)
    await setup_entry(hass, fake_duet)
    status(fake_duet, "pausing")  # the fake does not finish pausing unless asked to
    with pytest.raises(HomeAssistantError, match="did not finish pausing"):
        await call(hass, "cancel")
    assert fake_duet.gcodes == []


async def test_cancel_sends_the_pause_once_when_it_does_not_take_effect(hass, fake_duet, monkeypatch):
    monkeypatch.setattr(controls, "CANCEL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(controls, "CANCEL_PAUSE_TIMEOUT", 0.05)
    await setup_entry(hass, fake_duet)
    status(fake_duet, "processing")
    with pytest.raises(HomeAssistantError, match="did not finish pausing"):
        await call(hass, "cancel")
    assert fake_duet.gcodes == ["M25"]


async def test_the_cancel_button_sends_what_the_service_sends(hass, fake_duet, fast_cancel):
    fake_duet.emulate_job_control = True
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing", extruded=10)
    await press(hass, entry, "button-cancel")
    assert fake_duet.gcodes == ["M25", "M0"]
    assert fake_duet.refused == []


async def test_the_cancel_button_keeps_its_unique_id_and_name(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"button-cancel-{entry.entry_id}")
    assert entity_id == "button.voron_cancel"


# --- A2: firmware refusals -------------------------------------------------------


async def load(hass, tool, name):
    await hass.services.async_call(DOMAIN, "load_filament", {"tool": tool, "filament": name}, blocking=True)


async def test_a_filament_name_with_a_comma_is_refused(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 0
    await setup_entry(hass, fake_duet)
    with pytest.raises(vol.Invalid):
        await load(hass, 0, "PLA,red")
    assert fake_duet.gcodes == []


async def test_loading_over_another_filament_is_refused(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 1  # holds PETG
    fake_duet.files["0:/filaments"] = [{"type": "d", "name": "PLA"}]
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="Unload PETG first"):
        await load(hass, 1, "PLA")
    assert fake_duet.gcodes == []


async def test_loading_the_filament_that_is_loaded_is_refused_whatever_the_case(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 1
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="already loaded"):
        await load(hass, 1, "petg")
    assert fake_duet.gcodes == []


async def test_loading_a_filament_with_no_directory_is_refused_and_lists_the_ones_there_are(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 0
    fake_duet.files["0:/filaments"] = [{"type": "d", "name": "ABS"}, {"type": "f", "name": "stray.txt"}]
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match=r"no filament PLA.*filaments: ABS\)"):
        await load(hass, 0, "PLA")
    assert fake_duet.gcodes == []


async def test_a_failed_listing_is_an_error_not_a_pass(hass, fake_duet, monkeypatch):
    fake_duet.model["state"]["currentTool"] = 0
    entry = await setup_entry(hass, fake_duet)

    async def broken(path, kind):
        raise OSError("unreachable")

    monkeypatch.setattr(coordinator_of(hass, entry), "list_directory", broken)
    with pytest.raises(HomeAssistantError, match="Could not list"):
        await load(hass, 0, "PLA")
    assert fake_duet.gcodes == []


async def test_a_tool_with_no_extruder_cannot_load_or_unload(hass, fake_duet):
    fake_duet.model["tools"][0]["extruders"] = []
    fake_duet.model["state"]["currentTool"] = 0
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="no extruder"):
        await load(hass, 0, "PLA")
    with pytest.raises(ServiceValidationError, match="no extruder"):
        await call(hass, "unload_filament", {"tool": 0})
    assert fake_duet.gcodes == []


async def test_unloading_with_nothing_loaded_is_refused(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 0  # extruder 0 holds nothing
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="No filament is loaded"):
        await call(hass, "unload_filament", {"tool": 0})
    assert fake_duet.gcodes == []


async def test_a_valid_load_sends_m701(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 0
    fake_duet.files["0:/filaments"] = [{"type": "d", "name": "PLA"}]
    await setup_entry(hass, fake_duet)
    await load(hass, 0, "PLA")
    assert fake_duet.gcodes == ['M701 S"PLA"']


async def test_sbc_reads_the_reply_of_a_quick_code_and_raises_its_error(hass, fake_dsf):
    entry = await setup_sbc_entry(hass, fake_dsf)
    coordinator = coordinator_of(hass, entry)
    fake_dsf.code_replies["M104 T5 S100"] = "Error: Heater 5 does not exist"
    with pytest.raises(HomeAssistantError, match="Heater 5 does not exist"):
        await controls.send(coordinator, "M104 T5 S100", wait=True)
    await controls.send(coordinator, "M104 T0 S100", wait=True)
    assert fake_dsf.codes == [("M104 T5 S100", None), ("M104 T0 S100", None)]


async def test_sbc_posts_a_code_that_may_take_long_without_waiting(hass, fake_dsf):
    entry = await setup_sbc_entry(hass, fake_dsf)
    await controls.send(coordinator_of(hass, entry), "G28")
    assert fake_dsf.codes == [("G28", "true")]


async def test_sbc_load_filament_checks_the_filaments_directory(hass, fake_dsf):
    fake_dsf.model["state"]["currentTool"] = 0
    fake_dsf.files["0:/filaments"] = [{"type": "d", "name": "PLA"}]
    await setup_sbc_entry(hass, fake_dsf)
    with pytest.raises(ServiceValidationError, match="no filament ABS"):
        await load(hass, 0, "ABS")
    await load(hass, 0, "PLA")
    assert fake_dsf.codes == [('M701 S"PLA"', "true")]


# --- A3 / A4: Filament Extruded ----------------------------------------------------


def test_extruded_reading_follows_a_job():
    assert extruded_reading(0.0, 100, True, False) == 100
    assert extruded_reading(100, 250.456, True, False) == 250.46


@pytest.mark.parametrize("raw", [None, "", True, "12"])
def test_extruded_reading_holds_the_last_number_when_a_poll_has_none(raw):
    assert extruded_reading(250, raw, True, False) == 250


def test_extruded_reading_holds_the_final_value_on_the_end_poll():
    assert extruded_reading(250, None, False, True) == 250


def test_extruded_reading_is_zero_after_the_end_poll_and_without_a_job():
    assert extruded_reading(250, None, False, False) == 0
    assert extruded_reading(250, 999, False, False) == 0  # a simulation reports a number too


def test_extruded_reading_follows_a_job_adopted_at_start_up():
    assert extruded_reading(0.0, 300, True, False) == 300


async def test_the_meter_counts_every_job_from_its_start(hass, fake_duet):
    """A utility_meter ignores a drop, so Filament Extruded must not hold the last job's total."""
    entry = await setup_entry(hass, fake_duet)
    source = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"Filament Extruded-{entry.entry_id}")
    assert await async_setup_component(hass, "utility_meter", {"utility_meter": {"extruded": {"source": source}}})
    await hass.async_block_till_done()
    events = collect_events(hass)

    readings = []
    for state, extruded in (("processing", 100), ("processing", 250), ("idle", None)):
        await step(hass, entry, fake_duet, state, extruded=extruded)
        readings.append(extruded_now(hass, entry))
    assert readings == [100, 250, 250]  # the end poll still shows the whole job
    assert [event["type"] for event in events] == ["job_started", "job_finished"]
    assert events[-1]["extruded_mm"] == 250

    await step(hass, entry, fake_duet, "idle")
    assert extruded_now(hass, entry) == 0
    await step(hass, entry, fake_duet, "processing", extruded=40)
    assert extruded_now(hass, entry) == 40
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.extruded").state) == 250 + 40


async def test_a_simulation_does_not_raise_filament_extruded(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    for extruded in (0, 100, 5000):
        await step(hass, entry, fake_duet, "simulating", extruded=extruded)
        assert extruded_now(hass, entry) == 0
    await step(hass, entry, fake_duet, "idle")
    assert extruded_now(hass, entry) == 0


async def test_a_missed_poll_leaves_filament_extruded_alone(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing", extruded=70)
    fake_duet.fail = True
    await refresh(hass, entry)
    assert extruded_now(hass, entry) == 70
    assert coordinator_of(hass, entry).extruded_mm == 70


# --- A5: the firmware entity ---------------------------------------------------------


async def test_the_firmware_entity_polls_for_the_latest_release(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert DuetFirmwareUpdate(coordinator_of(hass, entry), "firmware").should_poll is True


async def test_a_firmware_update_shows_in_the_entity_and_the_device(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    coordinator = coordinator_of(hass, entry)
    entity = DuetFirmwareUpdate(coordinator, "firmware")
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.sw_version == entity.installed_version == "3.5.4"

    fake_duet.model["boards"][0]["firmwareVersion"] = "3.6.0"
    coordinator._slow_fetched_at = None  # the boards are read on the slow poll
    await refresh(hass, entry)
    assert entity.installed_version == "3.6.0"
    assert coordinator.firmware_version == "3.6.0"
    assert dr.async_get(hass).async_get(device.id).sw_version == "3.6.0"


# --- A6: the LED light ------------------------------------------------------------------


def light_id(hass):
    (entity,) = by_unique_id_prefix(hass, "LED-")
    return entity.entity_id


async def test_the_light_sends_the_rgb_colour_and_brightness(hass, fake_duet):
    await setup_entry(hass, fake_duet, entry_for(fake_duet, light=True))
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": light_id(hass), "rgb_color": [255, 0, 0], "brightness": 128}, blocking=True
    )
    assert fake_duet.gcodes == ["M150 E0 R255 U0 B0 P128 S1"]
    state = hass.states.get(light_id(hass))
    assert state.state == "on" and state.attributes["rgb_color"] == (255, 0, 0)


async def test_the_light_turns_off(hass, fake_duet):
    await setup_entry(hass, fake_duet, entry_for(fake_duet, light=True))
    await hass.services.async_call("light", "turn_off", {"entity_id": light_id(hass)}, blocking=True)
    assert fake_duet.gcodes == ["M150 E0 R0 U0 B0 P0 S1"]
    assert hass.states.get(light_id(hass)).state == "off"


async def test_a_failed_light_command_raises_and_changes_nothing(hass, fake_duet, monkeypatch):
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, light=True))

    async def broken(gcode, wait=False):
        raise OSError("down")

    monkeypatch.setattr(coordinator_of(hass, entry), "send_gcode", broken)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": light_id(hass), "rgb_color": [0, 255, 0]}, blocking=True
        )
    assert hass.states.get(light_id(hass)).state == "off"


# --- L1: webcam from DWC -----------------------------------------------------------------


async def test_a_snapshot_webcam_in_dwc_is_not_offered_as_a_stream(hass, fake_duet):
    fake_duet.downloads["0:/sys/dwc-settings.json"] = {
        "webcam": {
            "enabled": True,
            "url": f"http://[HOSTNAME]:{fake_duet.port}/webcam/snapshot",
            "liveUrl": f"http://[HOSTNAME]:{fake_duet.port}/webcam/stream",
            "updateInterval": 5000,
        }
    }
    entry = await setup_entry(hass, fake_duet)
    coordinator = coordinator_of(hass, entry)
    assert coordinator.webcam_url == f"http://{fake_duet.host}:{fake_duet.port}/webcam/snapshot"
    assert coordinator.webcam_live_url is None


# --- L2: a poll interval of 0 ---------------------------------------------------------------


async def test_the_user_form_rejects_a_zero_interval(hass, hass_client):
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/flow", json={"handler": DOMAIN, "show_advanced_options": False}
    )
    flow_id = (await response.json())["flow_id"]
    response = await client.post(
        f"/api/config/config_entries/flow/{flow_id}",
        json={"name": "x", "ssl": False, "host": "h", "password": "", "port": 80, "update_interval": 0},
    )
    assert response.status == 400


async def test_the_options_form_rejects_a_zero_interval(hass, hass_client):
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "d", "host": "h", "port": 80, "password": "", "ssl": False,
              "update_interval": 10, "light": False, "standalone": True},
    )
    entry.add_to_hass(hass)
    client = await hass_client()
    response = await client.post("/api/config/config_entries/options/flow", json={"handler": entry.entry_id})
    flow_id = (await response.json())["flow_id"]
    response = await client.post(
        f"/api/config/config_entries/options/flow/{flow_id}",
        json={"update_interval": 0, "printing_interval": 5, "light": False},
    )
    assert response.status == 400


async def test_an_entry_saved_with_a_zero_interval_does_not_poll_non_stop(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, update_interval=0))
    assert coordinator_of(hass, entry).update_interval.total_seconds() >= 1


# --- L3: stale state ---------------------------------------------------------------------------


async def test_one_missed_poll_is_tolerated_by_entities_but_not_by_a_safety_check(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fake_duet.fail = True
    with pytest.raises(HomeAssistantError, match="not reachable"):
        await call(hass, "home")
    assert fake_duet.gcodes == []
    coordinator = coordinator_of(hass, entry)
    assert coordinator.last_update_success and not coordinator.last_poll_ok
    assert state_of(hass, entry, "Current State").state == "idle"  # entities keep the last data

    fake_duet.fail = False
    await call(hass, "home")
    assert fake_duet.gcodes == ["G28"]


# --- L4: progress --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extruded, filament, expected",
    [
        (50, [0], 0),
        (50, [], 0),
        (50, None, 0),
        (50, "", 0),
        (None, [100], 0),
        (50, [100], 50),
        (50, [100, 100], 25),
        (300, [100, 100], 100),
    ],
)
def test_progress_percent(extruded, filament, expected):
    assert progress_percent(extruded, filament) == expected


async def test_progress_sensor_with_a_zero_slicer_total_is_zero(hass, fake_duet):
    fake_duet.model["job"]["file"]["filament"] = [0]
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing", extruded=10)
    fake_duet.model["job"]["file"]["filament"] = [0]
    await refresh(hass, entry)
    assert state_of(hass, entry, "Progress").state == "0"


# --- L5: SBC mode -----------------------------------------------------------------------------------


async def test_sbc_with_no_boards_sets_up(hass, fake_dsf):
    fake_dsf.model["boards"] = []
    entry = await setup_sbc_entry(hass, fake_dsf)
    coordinator = coordinator_of(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert coordinator.firmware_version is None and coordinator.board_model is None


async def test_sbc_reads_firmware_and_the_short_board_name(hass, fake_dsf):
    entry = await setup_sbc_entry(hass, fake_dsf)
    coordinator = coordinator_of(hass, entry)
    assert (coordinator.firmware_version, coordinator.board_model) == ("3.5.4", "Mini5plus")


async def test_standalone_reads_firmware_and_the_board_name(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    coordinator = coordinator_of(hass, entry)
    assert (coordinator.firmware_version, coordinator.board_model) == ("3.5.4", "Duet 3 Mini 5+")


async def test_sbc_logs_in_with_the_password(hass, fake_dsf):
    fake_dsf.password = "pw"
    entry = await setup_sbc_entry(hass, fake_dsf, password="pw")
    assert entry.state is ConfigEntryState.LOADED
    assert fake_dsf.connects == 1
    assert state_of(hass, entry, "Current State").state == "idle"


async def test_sbc_with_the_wrong_password_starts_reauth(hass, fake_dsf):
    fake_dsf.password = "right"
    entry = entry_for(fake_dsf, standalone=False, password="wrong")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_sbc_that_needs_a_password_the_entry_lacks_starts_reauth(hass, fake_dsf):
    fake_dsf.password = "right"
    entry = entry_for(fake_dsf, standalone=False, password="")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_sbc_logs_in_again_once_when_the_session_is_rejected(hass, fake_dsf):
    fake_dsf.password = "pw"
    entry = await setup_sbc_entry(hass, fake_dsf, password="pw")
    fake_dsf.session_key = "rotated"
    await refresh(hass, entry)
    coordinator = coordinator_of(hass, entry)
    assert coordinator.last_update_success
    assert fake_dsf.connects == 2


async def test_detect_finds_a_protected_dsf_and_checks_its_password(hass, fake_dsf):
    url = f"http://{fake_dsf.host}:{fake_dsf.port}"
    fake_dsf.password = "pw"
    assert await detect_standalone(url, "pw") is False
    with pytest.raises(InvalidAuth):
        await detect_standalone(url, "nope")


# --- L6: thermostatic fans ----------------------------------------------------------------------------


def test_build_fans_flags_a_thermostatic_fan():
    fans = build_fans(
        [
            {"name": "a", "thermostatic": {"sensors": [1]}},
            {"name": "b", "thermostatic": {"sensors": []}},
            {"name": "c"},
            {"name": "d", "thermostatic": None},
        ]
    )
    assert [fans[f"fan-{i}"]["thermostatic"] for i in range(4)] == [True, False, False, False]


async def test_a_thermostatic_fan_gets_no_control_but_keeps_its_speed_sensor(hass, fake_duet):
    fake_duet.model["fans"][0]["thermostatic"] = {"sensors": [1], "lowTemperature": 40, "highTemperature": 70}
    entry = await setup_entry(hass, fake_duet)
    assert not has_entity(hass, entry, "fan-control-0", "fan")
    assert has_entity(hass, entry, "fan-control-2", "fan")
    assert [e for e in by_unique_id_prefix(hass, "fan-0") if e.domain == "sensor"]


async def test_the_control_of_a_fan_that_is_thermostatic_now_is_removed(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert has_entity(hass, entry, "fan-control-0", "fan")
    fake_duet.model["fans"][0]["thermostatic"] = {"sensors": [1]}
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not has_entity(hass, entry, "fan-control-0", "fan")
    assert has_entity(hass, entry, "fan-control-2", "fan")


# --- L7: thumbnail camera -------------------------------------------------------------------------------


def png_bytes(text=None):
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    if text:
        info.add_text("note", text)
    with io.BytesIO() as out:
        Image.new("RGB", (2, 2), (200, 10, 10)).save(out, format="PNG", pnginfo=info)
        return out.getvalue()


def qoi_pixel():
    """A 1x1 red QOI image."""
    return b"qoif" + (1).to_bytes(4, "big") * 2 + bytes([3, 0]) + b"\xfe\xff\x00\x00" + b"\x00" * 7 + b"\x01"


def thumbnail_camera(hass, entry):
    return er.async_get(hass).async_get_entity_id("camera", DOMAIN, entry.entry_id)


NAME = "0:/gcodes/benchy.gcode"


def serve(server, data, split=7, offset=500):
    """Serve ``data`` as the firmware does: base64 text in chunks that need not align."""
    text = base64.b64encode(data).decode()
    server.model["job"]["file"]["thumbnails"] = [
        {"width": 16, "height": 16, "format": "png", "offset": 100, "size": 50},
        {"width": 300, "height": 300, "format": "png", "offset": offset, "size": len(text)},
    ]
    server.thumbnails[(NAME, offset)] = (text[:split], offset + 400)
    server.thumbnails[(NAME, offset + 400)] = (text[split:], 0)


async def test_standalone_thumbnail_is_fetched_in_chunks(hass, fake_duet):
    png = png_bytes()
    serve(fake_duet, png)
    entry = await setup_entry(hass, fake_duet)
    image = await async_get_image(hass, thumbnail_camera(hass, entry))
    assert image.content == png
    assert fake_duet.thumbnail_requests == [(NAME, 500), (NAME, 900)]  # the largest, then the rest


async def test_the_thumbnail_is_fetched_once_per_file(hass, fake_duet):
    serve(fake_duet, png_bytes())
    entry = await setup_entry(hass, fake_duet)
    for _ in range(3):
        await async_get_image(hass, thumbnail_camera(hass, entry))
    assert len(fake_duet.thumbnail_requests) == 2


async def test_a_qoi_thumbnail_is_converted_to_jpeg(hass, fake_duet):
    serve(fake_duet, qoi_pixel())
    entry = await setup_entry(hass, fake_duet)
    image = await async_get_image(hass, thumbnail_camera(hass, entry))
    assert image.content.startswith(b"\xff\xd8")


async def test_a_png_that_contains_the_letters_qoi_is_not_converted(hass, fake_duet):
    png = png_bytes("a qoi inside")
    assert b"qoi" in png
    serve(fake_duet, png)
    entry = await setup_entry(hass, fake_duet)
    assert (await async_get_image(hass, thumbnail_camera(hass, entry))).content == png


async def test_standalone_thumbnail_is_unavailable_without_one_to_fetch(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert hass.states.get(thumbnail_camera(hass, entry)).state == "unavailable"
    serve(fake_duet, png_bytes())
    await refresh(hass, entry)
    assert hass.states.get(thumbnail_camera(hass, entry)).state != "unavailable"


async def test_sbc_thumbnail_is_the_inline_data(hass, fake_dsf):
    png = png_bytes()
    fake_dsf.model["job"]["file"]["thumbnails"] = [
        {"width": 300, "height": 300, "format": "png", "offset": 10, "size": 1,
         "data": base64.b64encode(png).decode()},
    ]
    entry = await setup_sbc_entry(hass, fake_dsf)
    assert (await async_get_image(hass, thumbnail_camera(hass, entry))).content == png


def test_pick_thumbnail_prefers_the_largest_usable_one():
    thumbs = [
        {"width": 16, "height": 16, "offset": 5},
        {"width": 300, "height": 300, "offset": 0},  # no usable offset
        {"width": 100, "height": 100, "offset": 9},
        "junk",
    ]
    assert pick_thumbnail(thumbs, inline=False)["offset"] == 9
    assert pick_thumbnail(thumbs, inline=True) is None
    assert pick_thumbnail(None, inline=False) is None


# --- L8: diagnostics --------------------------------------------------------------------------------------


async def test_diagnostics_do_not_leak_the_webcam_address(hass, fake_duet):
    from custom_components.duet3d.diagnostics import async_get_config_entry_diagnostics

    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, webcam_url="http://u:p@cam/stream"))
    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert "u:p@cam" not in json.dumps(dump, default=str)
    assert dump["extruded_mm"] == 0


# --- known limitation: documented, so a change to it is noticed -------------------------------------------


async def test_a_job_that_starts_within_one_poll_of_the_last_one_ending_fires_no_finish(hass, fake_duet):
    """The tracker never sees ``idle`` between two back-to-back jobs (see CLAUDE.md)."""
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)
    await step(hass, entry, fake_duet, "processing", extruded=10)
    job_poll(fake_duet, "processing", extruded=1)
    fake_duet.model["job"]["file"]["fileName"] = "0:/gcodes/second.gcode"
    await refresh(hass, entry)
    assert [event["type"] for event in events] == ["job_started"]
