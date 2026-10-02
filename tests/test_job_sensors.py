"""ETA, timing, slicer estimate, print speed and object cancellation, end to end."""
from datetime import datetime

import pytest

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from fake_duet import DOMAIN, job_poll, refresh, setup_entry, state_of, step

OBJECTS = {
    "currentObject": 1,
    "m486Names": True,
    "m486Numbers": False,
    "objects": [
        {"name": "left", "cancelled": False, "x": [0, 10], "y": [0, 10]},
        {"name": "right", "cancelled": True, "x": [20, 30], "y": [0, 10]},
    ],
}


def coordinator_of(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


async def test_idle_printer_has_no_eta_or_projection_or_objects(hass, fake_duet):
    fake_duet.model["job"]["timesLeft"] = {"file": None, "slicer": None, "filament": None}
    fake_duet.model["job"]["duration"] = None
    fake_duet.model["job"]["file"]["filament"] = []
    entry = await setup_entry(hass, fake_duet)
    for name in ("Print ETA", "Projected total duration", "Slicer filament length", "Print objects",
                 "Print start time", "Print end time", "Print speed"):
        assert state_of(hass, entry, name).state == "unknown", name


async def test_eta_and_projected_total_while_printing(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing", extruded=10, timesLeft={"file": 3600, "slicer": 3600, "filament": 3600})
    read = coordinator_of(hass, entry).data["last_read_time"]
    eta = dt_util.parse_datetime(state_of(hass, entry, "Print ETA").state)
    assert abs((eta - read).total_seconds() - 3600) <= 30
    assert eta.second == 0
    assert state_of(hass, entry, "Print ETA").attributes["device_class"] == "timestamp"
    # duration is 60 s in job_poll
    assert state_of(hass, entry, "Projected total duration").state == "61.0"


async def test_start_and_end_time_come_from_the_job_events(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "processing", extruded=0)
    started = dt_util.parse_datetime(state_of(hass, entry, "Print start time").state)
    # a timestamp state keeps whole seconds
    assert started == coordinator_of(hass, entry).data["last_read_time"].replace(microsecond=0)
    assert state_of(hass, entry, "Print end time").state == "unknown"

    await step(hass, entry, fake_duet, "idle")
    ended = dt_util.parse_datetime(state_of(hass, entry, "Print end time").state)
    assert ended >= started
    # the start of the job that just ended is kept until the next one starts
    assert dt_util.parse_datetime(state_of(hass, entry, "Print start time").state) == started

    await step(hass, entry, fake_duet, "processing", extruded=0)
    assert state_of(hass, entry, "Print end time").state == "unknown"


async def test_a_job_already_running_gets_a_start_time_from_its_duration(hass, fake_duet):
    job_poll(fake_duet, "processing", extruded=100)
    fake_duet.model["job"]["duration"] = 600
    entry = await setup_entry(hass, fake_duet)
    read = coordinator_of(hass, entry).data["last_read_time"]
    started = dt_util.parse_datetime(state_of(hass, entry, "Print start time").state)
    assert (read - started).total_seconds() == pytest.approx(600, abs=1)


async def test_a_simulation_has_no_start_time(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    await step(hass, entry, fake_duet, "simulating", extruded=10)
    assert state_of(hass, entry, "Print start time").state == "unknown"


async def test_slicer_filament_length_with_per_extruder_attribute(hass, fake_duet):
    fake_duet.model["job"]["file"]["filament"] = [1500.25, 20.0]
    entry = await setup_entry(hass, fake_duet)
    state = state_of(hass, entry, "Slicer filament length")
    assert state.state == "1520.2"  # one decimal
    assert state.attributes["extruders"] == [1500.25, 20.0]
    assert state.attributes["unit_of_measurement"] == "mm"


async def test_print_speed_reads_the_current_move(hass, fake_duet):
    fake_duet.model["move"]["currentMove"] = {"requestedSpeed": 60.0, "topSpeed": 58.2, "extrusionRate": 3.1}
    entry = await setup_entry(hass, fake_duet)
    assert state_of(hass, entry, "Print speed").state == "60.0"


async def test_print_objects_sensor_lists_them(hass, fake_duet):
    fake_duet.model["job"]["build"] = OBJECTS
    entry = await setup_entry(hass, fake_duet)
    state = state_of(hass, entry, "Print objects")
    assert state.state == "2"
    assert state.attributes["cancelled"] == 1
    assert state.attributes["current"] == 1
    assert state.attributes["objects"] == [
        {"index": 0, "name": "left", "cancelled": False},
        {"index": 1, "name": "right", "cancelled": True},
    ]


# --- cancel_object -----------------------------------------------------------


async def cancel(hass, number):
    await hass.services.async_call(DOMAIN, "cancel_object", {"object": number}, blocking=True)


async def test_cancel_object_sends_m486_while_printing(hass, fake_duet):
    fake_duet.model["job"]["build"] = OBJECTS
    await setup_entry(hass, fake_duet)
    fake_duet.model["state"]["status"] = "processing"
    await cancel(hass, 0)
    assert fake_duet.gcodes == ["M486 P0"]


@pytest.mark.parametrize("state", ["idle", "paused", "halted"])
async def test_cancel_object_is_refused_unless_a_job_is_running(hass, fake_duet, state):
    fake_duet.model["job"]["build"] = OBJECTS
    await setup_entry(hass, fake_duet)
    fake_duet.model["state"]["status"] = state
    with pytest.raises(ServiceValidationError, match=state):
        await cancel(hass, 0)
    assert fake_duet.gcodes == []


async def test_cancel_object_refuses_an_object_the_job_does_not_have(hass, fake_duet):
    fake_duet.model["job"]["build"] = OBJECTS
    await setup_entry(hass, fake_duet)
    fake_duet.model["state"]["status"] = "processing"
    with pytest.raises(ServiceValidationError, match="no object 5"):
        await cancel(hass, 5)
    assert fake_duet.gcodes == []


async def test_cancel_object_refuses_when_the_job_has_no_labelled_objects(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    fake_duet.model["state"]["status"] = "processing"
    with pytest.raises(ServiceValidationError, match="none"):
        await cancel(hass, 0)
