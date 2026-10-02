"""Job events on the HA bus, device triggers, adaptive polling and the extrusion latch,
all through the real integration against a fake printer."""
from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.device_automation import DeviceAutomationType, async_get_device_automations
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from fake_duet import (
    DOMAIN,
    collect_events,
    entry_for,
    job_poll,
    setup_entry,
    state_of,
    step,
)


def coordinator_of(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


def types(events):
    return [event["type"] for event in events]


# --- adaptive polling --------------------------------------------------------


async def test_polls_every_idle_interval_when_idle_and_printing_interval_during_a_job(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, update_interval=30))
    coordinator = coordinator_of(hass, entry)
    assert coordinator.update_interval == timedelta(seconds=30)

    await step(hass, entry, fake_duet, "processing", extruded=0)
    assert coordinator.update_interval == timedelta(seconds=5)

    await step(hass, entry, fake_duet, "paused", extruded=10)
    assert coordinator.update_interval == timedelta(seconds=30)  # nothing to track while paused

    await step(hass, entry, fake_duet, "idle")
    assert coordinator.update_interval == timedelta(seconds=30)


@pytest.mark.parametrize("state", ["processing", "pausing", "resuming", "cancelling", "changingTool"])
async def test_every_state_in_which_a_job_runs_is_polled_fast(hass, fake_duet, state):
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, state)
    assert coordinator_of(hass, entry).update_interval == timedelta(seconds=5)


async def test_the_printing_interval_is_configurable(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, printing_interval=2))
    await step(hass, entry, fake_duet, "processing")
    assert coordinator_of(hass, entry).update_interval == timedelta(seconds=2)


async def test_a_job_is_never_polled_slower_than_an_idle_printer(hass, fake_duet):
    entry = await setup_entry(
        hass, fake_duet, entry_for(fake_duet, update_interval=3, printing_interval=10)
    )
    await step(hass, entry, fake_duet, "processing")
    assert coordinator_of(hass, entry).update_interval == timedelta(seconds=3)


async def test_a_failed_poll_leaves_the_interval_alone(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing")
    fake_duet.fail = True
    await coordinator_of(hass, entry).async_refresh()
    assert coordinator_of(hass, entry).update_interval == timedelta(seconds=5)


# --- bus events --------------------------------------------------------------


async def test_a_print_fires_started_and_finished_with_the_job_details(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)

    await step(hass, entry, fake_duet, "processing", extruded=0)
    await step(hass, entry, fake_duet, "processing", extruded=1234.5)
    await step(hass, entry, fake_duet, "idle", lastDuration=3600)
    await hass.async_block_till_done()

    assert types(events) == ["job_started", "job_finished"]
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    finished = events[1]
    assert finished["device_id"] == device.id
    assert finished["name"] == "Voron"
    assert finished["file_name"] == "0:/gcodes/benchy.gcode"
    assert finished["extruded_mm"] == 1234.5
    assert finished["duration"] == 3600
    assert finished["tool"] == 0 and finished["extruders"] == [0]
    assert finished["slicer_filament_mm"] == [1000.0]


async def test_full_lifecycle_with_pause_and_cancel(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)
    for status in ("processing", "pausing", "paused", "resuming", "processing", "cancelling", "idle"):
        await step(hass, entry, fake_duet, status, extruded=100)
    await hass.async_block_till_done()
    assert types(events) == ["job_started", "job_paused", "job_resumed", "job_cancelled"]


async def test_a_simulation_fires_nothing(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)
    for status in ("simulating", "simulating", "idle"):
        await step(hass, entry, fake_duet, status, extruded=50)
    await hass.async_block_till_done()
    assert events == []


async def test_no_event_when_homeassistant_starts_in_the_middle_of_a_job(hass, fake_duet):
    job_poll(fake_duet, "processing", extruded=300)
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)
    await step(hass, entry, fake_duet, "processing", extruded=600)
    assert events == []
    await step(hass, entry, fake_duet, "idle")
    await hass.async_block_till_done()
    assert types(events) == ["job_finished"]
    assert events[0]["extruded_mm"] == 600


async def test_events_are_fired_after_the_entities_show_the_poll(hass, fake_duet):
    """An automation reacting to job_finished reads entity states; they must be current."""
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing", extruded=0)
    seen = []
    hass.bus.async_listen(
        "duet3d_event",
        lambda event: seen.append(
            (
                event.data["type"],
                state_of(hass, entry, "Current State").state,
                float(state_of(hass, entry, "Filament Extruded").state),
            )
        ),
    )
    await step(hass, entry, fake_duet, "processing", extruded=800)
    await step(hass, entry, fake_duet, "idle")
    await hass.async_block_till_done()
    assert seen == [("job_finished", "idle", 800)]


async def test_message_box_and_halt_events(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    events = collect_events(hass)
    fake_duet.model["state"]["messageBox"] = {"message": "Change filament", "mode": 2, "seq": 1}
    await step(hass, entry, fake_duet, "idle")
    await step(hass, entry, fake_duet, "halted")
    await hass.async_block_till_done()
    assert types(events) == ["message_box_opened", "printer_halted"]
    assert events[0]["message"] == "Change filament"


# --- Filament Extruded -------------------------------------------------------


async def test_filament_extruded_holds_the_total_for_the_end_poll_only(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing", extruded=0)
    await step(hass, entry, fake_duet, "processing", extruded=2500.25)
    assert float(state_of(hass, entry, "Filament Extruded").state) == 2500.25

    # the firmware reports null as soon as the job ends, but the print-end automation
    # reads the total on the poll that fires job_finished
    await step(hass, entry, fake_duet, "idle")
    assert float(state_of(hass, entry, "Filament Extruded").state) == 2500.25
    # after that it is 0, so that the next job counts from 0 in a utility meter
    await step(hass, entry, fake_duet, "idle")
    assert float(state_of(hass, entry, "Filament Extruded").state) == 0


async def test_filament_extruded_is_zero_before_any_job(hass, fake_duet):
    fake_duet.model["job"]["rawExtrusion"] = None
    entry = await setup_entry(hass, fake_duet)
    assert float(state_of(hass, entry, "Filament Extruded").state) == 0


# --- device triggers ---------------------------------------------------------


async def test_the_device_offers_every_job_event_as_a_trigger(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, [device.id])
    offered = {trigger["type"] for trigger in triggers[device.id] if trigger["domain"] == DOMAIN}
    assert offered == {
        "job_started", "job_paused", "job_resumed", "job_finished", "job_cancelled",
        "job_failed", "message_box_opened", "printer_halted",
    }


async def test_a_device_trigger_runs_its_automation_for_that_event_only(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {
                    "platform": "device", "domain": DOMAIN, "device_id": device.id, "type": "job_finished",
                },
                "action": {
                    "service": "test.automation",
                    "data_template": {"file": "{{ trigger.event.data.file_name }}"},
                },
            }
        },
    )
    await step(hass, entry, fake_duet, "processing", extruded=0)
    await hass.async_block_till_done()
    assert calls == []  # job_started is not job_finished
    await step(hass, entry, fake_duet, "processing", extruded=10)
    await step(hass, entry, fake_duet, "idle")
    await hass.async_block_till_done()
    assert [call.data["file"] for call in calls] == ["0:/gcodes/benchy.gcode"]


async def test_triggers_are_not_offered_for_other_devices(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("other", "x")}
    )
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, [other.id])
    assert not [t for t in triggers.get(other.id, []) if t["domain"] == DOMAIN]
