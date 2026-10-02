"""Fans, boards, storage, network, filament monitors and machine state from the model.

Shapes for fans, boards, volumes, network and axes follow a real board running
3.7.0-rc.2 (see ``fixtures/rrf_3_7_0_rc2_standalone.json``). Filament monitors are
NOT from a board (the test machine has none): they follow the firmware source
(``FilamentMonitor.cpp``), where ``filamentPresent`` is only reported when the
monitor can tell and ``enabled`` is obsolete in favour of ``enableMode``.
"""
import json
from pathlib import Path

import pytest

from custom_components.duet3d.hardware import (
    axes_homed,
    build_boards,
    build_fans,
    build_filament_monitors,
    build_led_strips,
    build_hardware,
    build_interfaces,
    build_volumes,
    message_box,
    startup_error,
)
from custom_components.duet3d.model import (
    build_heater_roles,
    heater_power,
    heater_state,
    set_path,
)

JUNK = [None, "", 0, [], {}, [None], "unknown", [1, "x"]]


def test_fans_skip_null_slots_and_name_unnamed_ones():
    fans = build_fans(
        [
            {"name": "Part Cooling Fan", "actualValue": 0.5, "requestedValue": 0.6, "rpm": -1},
            None,
            {"name": "", "actualValue": 1.0, "requestedValue": 1.0, "rpm": 3000},
        ]
    )
    assert list(fans) == ["fan-0", "fan-2"]
    assert fans["fan-0"] == {
        "index": 0, "label": "Part Cooling Fan", "speed": 50.0, "requested": 60.0, "rpm": None,
        "thermostatic": False,
    }
    assert fans["fan-2"]["label"] == "Fan 2"  # keyed by index, so renaming a fan keeps its entity
    assert fans["fan-2"]["rpm"] == 3000


def test_boards_are_keyed_by_can_address_and_main_board_has_no_state():
    boards = build_boards(
        [
            {"canAddress": 0, "name": "BTT Kraken", "shortName": "kraken", "mcuTemp": {"current": 52.5},
             "vIn": {"current": 24.0}, "v12": None, "freeRam": 53428, "firmwareVersion": "3.7.0-rc.2"},
            {"canAddress": 124, "name": "Duet 3 Expansion SB2040MAX3", "shortName": "SB2040MAX3",
             "state": "running", "mcuTemp": {"current": 44.9}, "vIn": None, "v12": None, "freeRam": 220284},
        ]
    )
    assert list(boards) == ["board-0", "board-124"]
    assert boards["board-0"]["label"] == "Main board"
    assert boards["board-0"]["state"] is None
    assert (boards["board-0"]["mcu_temp"], boards["board-0"]["v_in"], boards["board-0"]["v_12"]) == (52.5, 24.0, None)
    assert boards["board-124"]["label"] == "SB2040MAX3"
    assert boards["board-124"]["state"] == "running"
    assert boards["board-124"]["v_in"] is None  # vIn: null on an expansion board


def test_volumes_skip_unmounted_slots():
    volumes = build_volumes(
        [
            {"mounted": True, "freeSpace": 31486902272, "capacity": 31914983424},
            {"mounted": False, "freeSpace": None, "capacity": None},
            {"mounted": True, "freeSpace": None, "capacity": None},
        ]
    )
    assert list(volumes) == ["volume-0"]
    assert volumes["volume-0"]["free"] == 31486902272


def test_wifi_interface_without_a_signal_still_reports_its_ip():
    interfaces = build_interfaces({"interfaces": [{"type": "wifi", "state": "active", "signal": None, "actualIP": "192.168.69.1"}]})
    assert interfaces["interface-0"] == {
        "index": 0, "label": "Wi-Fi", "type": "wifi", "ip": "192.168.69.1", "signal": None,
    }


def test_filament_monitors_by_extruder_with_unknown_presence():
    monitors = build_filament_monitors(
        [
            {"enableMode": 1, "status": "ok", "type": "simple", "filamentPresent": False},
            None,
            {"enableMode": 1, "status": "noDataReceived", "type": "laser"},  # no filamentPresent
        ]
    )
    assert list(monitors) == ["monitor-0", "monitor-2"]
    assert monitors["monitor-0"]["present"] is False  # False is a real answer, not "unknown"
    assert monitors["monitor-2"]["present"] is None
    assert monitors["monitor-2"]["status"] == "noDataReceived"  # passed through untouched
    assert monitors["monitor-2"]["extruder"] == 2


def test_homed_only_counts_visible_axes():
    assert axes_homed([{"letter": "X", "homed": True}, {"letter": "Y", "homed": True}])["all"] is True
    partly = axes_homed([{"letter": "X", "homed": True}, {"letter": "Y", "homed": False}])
    assert partly == {"all": False, "axes": {"X": True, "Y": False}}
    hidden = axes_homed([{"letter": "X", "homed": True}, {"letter": "U", "homed": False, "visible": False}])
    assert hidden["all"] is True
    assert axes_homed([]) is None


def test_message_box_and_startup_error_come_from_state():
    assert message_box({"messageBox": None}) is None
    assert message_box({"messageBox": {"message": "", "title": "x"}}) is None
    box = message_box({"messageBox": {"message": "Change filament", "title": "Filament", "mode": 1, "seq": 3, "timeout": None}})
    assert box == {"message": "Change filament", "title": "Filament", "mode": 1, "seq": 3}

    assert startup_error({"startupError": None}) is None
    error = startup_error(
        {"startupError": {"file": "config.g", "line": 115, "message": "M955: parameter 'P' too high"}}
    )
    assert error == {"message": "M955: parameter 'P' too high", "file": "config.g", "line": 115}


def test_heater_state_and_power():
    heat = {"heaters": [{"current": 25.0, "state": "fault", "avgPwm": 0.1234}, {"state": ""}]}
    roles = build_heater_roles({**heat, "bedHeaterMapping": [[0, 1]]}, [])
    assert heater_state(heat, roles["bed"]) == "fault"
    assert heater_power(heat, roles["bed"]) == 12.3
    assert heater_state(heat, roles["bed-0-1"]) is None  # empty string is not a state
    assert heater_power(heat, roles["bed-0-1"]) is None


def test_set_path_rebuilds_nesting_for_dotted_keys():
    status = {}
    set_path(status, "sensors.filamentMonitors", [1])
    set_path(status, "fans", [2])
    assert status == {"sensors": {"filamentMonitors": [1]}, "fans": [2]}
    set_path(status, "sensors.other", 3)  # does not clobber siblings
    assert status["sensors"] == {"filamentMonitors": [1], "other": 3}
    status["x"] = ""  # rr_model answers "" for an empty key
    set_path(status, "x.y", 1)
    assert status["x"] == {"y": 1}


@pytest.mark.parametrize("junk", JUNK)
def test_malformed_input_never_raises(junk):
    for build in (build_fans, build_boards, build_volumes, build_interfaces, build_filament_monitors,
                  build_led_strips):
        build(junk)
    axes_homed(junk)
    message_box(junk)
    startup_error(junk)
    build_hardware(junk)
    assert build_hardware(junk) == {
        "fans": {}, "boards": {}, "volumes": {}, "interfaces": {}, "led_strips": {}, "monitors": {},
    }


def test_real_rc2_model_has_the_expected_hardware():
    """Network, volumes and fans from the board the fixture was captured from."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "rrf_3_7_0_rc2_standalone.json").read_text()
    )
    # the fixture only holds state/job/heat/tools/move, so nothing else to find in it
    assert build_hardware(fixture)["fans"] == {}
    assert axes_homed(fixture["move"]["axes"]) == {
        "all": False, "axes": {"X": False, "Y": False, "Z": False},
    }
    assert startup_error(fixture["state"])["file"] == "config.g"
