"""The job tracker is pure: feed it polls, check the events."""
import pytest

from custom_components.duet3d.events import JobTracker


def poll(status, *, tool=0, extruded=None, name="0:/gcodes/benchy.gcode", box=None, **job):
    """One poll's ``status`` tree, as the coordinator holds it."""
    in_job = status not in ("idle", "off", "halted")
    return {
        "state": {"status": status, "currentTool": tool, "messageBox": box},
        "job": {
            "duration": 60 if in_job else None,
            "rawExtrusion": extruded,
            "file": {"fileName": name if in_job else None, "filament": [1500.0, 20.0] if in_job else []},
            **job,
        },
        "tools": [{"number": 0, "extruders": [0]}, {"number": 1, "extruders": [1]}],
    }


def types(events):
    return [event["type"] for event in events]


def run(*polls):
    """The events of each poll, in order, from one fresh tracker."""
    tracker = JobTracker()
    return [types(tracker.update(p)) for p in polls]


def test_a_normal_print():
    assert run(
        poll("idle"),
        poll("processing", extruded=0),
        poll("processing", extruded=900),
        poll("idle", lastDuration=3600),
    ) == [[], ["job_started"], [], ["job_finished"]]


def test_the_first_poll_emits_nothing_whatever_the_state():
    for status in ("idle", "processing", "paused", "cancelling", "halted", "simulating"):
        assert JobTracker().update(poll(status)) == []


def test_restart_in_the_middle_of_a_job_adopts_it_silently_then_reports_its_end():
    tracker = JobTracker()
    assert tracker.update(poll("processing", extruded=400)) == []
    assert tracker.update(poll("processing", extruded=800)) == []
    (event,) = tracker.update(poll("idle"))
    assert event["type"] == "job_finished"
    assert event["extruded_mm"] == 800  # the whole job, as the firmware counts it


def test_pause_and_resume():
    assert run(
        poll("idle"),
        poll("processing"),
        poll("pausing"),
        poll("paused"),
        poll("resuming"),
        poll("processing"),
        poll("idle"),
    ) == [[], ["job_started"], ["job_paused"], [], ["job_resumed"], [], ["job_finished"]]


def test_pause_resumed_between_polls_is_still_reported_once():
    assert run(poll("processing"), poll("paused"), poll("processing")) == [
        [],
        ["job_paused"],
        ["job_resumed"],
    ]


def test_cancelling_state_makes_the_end_a_cancel():
    assert run(
        poll("idle"), poll("processing"), poll("cancelling"), poll("idle")
    ) == [[], ["job_started"], [], ["job_cancelled"]]


def test_cancel_shorter_than_one_poll_is_recognised_by_the_last_file_flag():
    assert run(
        poll("idle", lastFileCancelled=False),
        poll("processing", lastFileCancelled=False),
        poll("idle", lastFileCancelled=True),
    ) == [[], ["job_started"], ["job_cancelled"]]


def test_a_stale_cancelled_flag_from_the_last_job_does_not_cancel_the_next():
    assert run(
        poll("idle", lastFileCancelled=True),
        poll("processing", lastFileCancelled=True),
        poll("idle", lastFileCancelled=True),
    ) == [[], ["job_started"], ["job_finished"]]


def test_without_the_last_file_flags_a_vanished_job_is_finished():
    """Not every firmware has job.lastFileCancelled/lastFileAborted."""
    assert run(poll("idle"), poll("processing"), poll("idle")) == [[], ["job_started"], ["job_finished"]]


def test_aborted_job_is_a_failure():
    assert run(
        poll("idle"), poll("processing"), poll("idle", lastFileAborted=True)
    ) == [[], ["job_started"], ["job_failed"]]


def test_emergency_stop_mid_job_halts_and_fails_the_job():
    assert run(poll("processing"), poll("processing"), poll("halted")) == [[], [], ["printer_halted", "job_failed"]]


def test_halted_while_idle_is_reported_once():
    assert run(poll("idle"), poll("halted"), poll("halted"), poll("idle")) == [[], ["printer_halted"], [], []]


def test_power_lost_mid_job_fails_it():
    assert run(poll("processing"), poll("off")) == [[], ["job_failed"]]


def test_a_job_seen_only_after_it_paused_still_starts_then_pauses():
    assert run(poll("idle"), poll("paused")) == [[], ["job_started", "job_paused"]]


@pytest.mark.parametrize("other", ["busy", "changingTool", "starting", "updating"])
def test_states_that_say_nothing_about_a_job_do_not_end_or_start_one(other):
    assert run(poll("idle"), poll(other), poll("idle")) == [[], [], []]
    assert run(poll("processing"), poll(other), poll("processing"), poll("idle")) == [
        [], [], [], ["job_finished"],
    ]


def test_a_cancel_with_no_job_is_not_a_job():
    """M0 with nothing printing runs stop.g, and may show ``cancelling``."""
    assert run(poll("idle"), poll("cancelling"), poll("idle")) == [[], [], []]


# --- simulations ----------------------------------------------------------


def test_a_simulation_emits_no_job_events():
    assert run(poll("idle"), poll("simulating"), poll("simulating"), poll("idle")) == [[], [], [], []]


def test_a_paused_or_cancelled_simulation_is_still_a_simulation():
    assert run(
        poll("idle"),
        poll("simulating"),
        poll("paused"),
        poll("processing"),
        poll("cancelling"),
        poll("idle"),
    ) == [[], [], [], [], [], []]


def test_a_real_print_straight_after_a_simulation_is_a_job():
    assert run(poll("simulating"), poll("idle"), poll("processing"), poll("idle")) == [
        [], [], ["job_started"], ["job_finished"],
    ]


def test_simulation_running_into_a_job_between_two_polls_is_a_job():
    assert run(poll("idle"), poll("simulating"), poll("processing")) == [[], [], ["job_started"]]


# --- payload ----------------------------------------------------------------


def test_payload_describes_the_job_that_ended_even_though_idle_has_no_job_data():
    tracker = JobTracker()
    tracker.update(poll("idle"))
    (started,) = tracker.update(poll("processing", tool=1, extruded=0))
    assert started["file_name"] == "0:/gcodes/benchy.gcode"
    assert started["tool"] == 1 and started["extruders"] == [1]
    assert started["slicer_filament_mm"] == [1500.0, 20.0]

    tracker.update(poll("processing", tool=1, extruded=1234.5))
    tracker.update(poll("processing", tool=1, extruded=None))  # a poll with no reading keeps the last
    (finished,) = tracker.update(poll("idle", tool=-1, lastDuration=3600.0))
    assert finished["type"] == "job_finished"
    assert finished["file_name"] == "0:/gcodes/benchy.gcode"
    assert finished["extruded_mm"] == 1234.5
    assert finished["duration"] == 3600.0  # lastDuration wins over the last live reading
    assert finished["tool"] == 1 and finished["extruders"] == [1]
    assert finished["slicer_filament_mm"] == [1500.0, 20.0]


def test_a_second_job_does_not_inherit_the_first_jobs_numbers():
    tracker = JobTracker()
    tracker.update(poll("idle"))
    tracker.update(poll("processing", extruded=500))
    tracker.update(poll("idle"))
    (started,) = tracker.update(poll("processing", name="0:/gcodes/other.gcode", extruded=None))
    assert started["extruded_mm"] == 0.0
    assert started["file_name"] == "0:/gcodes/other.gcode"


def test_no_tool_selected_gives_no_extruders():
    tracker = JobTracker()
    tracker.update(poll("idle"))
    (started,) = tracker.update(poll("processing", tool=-1))
    assert started["tool"] is None and started["extruders"] == []


# --- message box ------------------------------------------------------------


def test_message_box_opening_is_reported_once_per_box():
    box = {"message": "Change filament", "title": "Filament", "mode": 2, "seq": 4}
    tracker = JobTracker()
    tracker.update(poll("processing"))
    (opened,) = tracker.update(poll("processing", box=box))
    assert opened == {"type": "message_box_opened", "message": "Change filament", "title": "Filament", "mode": 2}
    assert tracker.update(poll("processing", box=box)) == []  # still the same box
    assert tracker.update(poll("processing")) == []  # closed
    assert types(tracker.update(poll("processing", box={**box, "seq": 5}))) == ["message_box_opened"]


def test_a_box_already_open_at_startup_is_not_reported():
    box = {"message": "Hello", "seq": 1}
    assert JobTracker().update(poll("idle", box=box)) == []


def test_a_new_box_replacing_an_open_one_is_reported():
    tracker = JobTracker()
    tracker.update(poll("idle"))
    tracker.update(poll("idle", box={"message": "One", "seq": 1}))
    assert types(tracker.update(poll("idle", box={"message": "Two", "seq": 2}))) == ["message_box_opened"]


# --- garbage in -------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", [], {}, {"state": ""}, {"state": {"status": None}}])
def test_malformed_polls_are_ignored(bad):
    tracker = JobTracker()
    assert tracker.update(bad) == []
    tracker.update(poll("processing"))
    assert tracker.update(bad) == []
    assert types(tracker.update(poll("idle"))) == ["job_finished"]
