"""Heater discovery from the object model, across firmware versions.

``fixtures/rrf_3_7_0_rc2_standalone.json`` was captured (read-only) from a real
BTT Kraken running 3.7.0-rc.2. There it is tool 0 that uses heater 1, which is
what broke the integration when tool numbers were taken from the config.

The 3.6 shape below is NOT captured from a board: it is written from the 3.6-dev
firmware source, where ``heat.bedHeaters``/``chamberHeaters`` are flat lists of
heater indices with -1 for none and there is no ``…Mapping``.
"""
import json
from pathlib import Path

import pytest

from custom_components.duet3d.model import build_heater_roles, heater_value, resolve

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rc2():
    return json.loads((FIXTURES / "rrf_3_7_0_rc2_standalone.json").read_text())


def _heater(current, active=0.0, standby=0.0):
    return {"current": current, "active": active, "standby": standby, "state": "off"}


def _tool(number, heaters, active, standby, extruders=None):
    return {
        "number": number,
        "heaters": heaters,
        "active": active,
        "standby": standby,
        "extruders": extruders if extruders is not None else [number],
    }


RRF_3_6 = {
    "heat": {
        "bedHeaters": [0, -1, -1, -1],
        "chamberHeaters": [-1, -1, -1, -1],
        "heaters": [_heater(60.0, 60.0), _heater(200.0, 210.0, 150.0), _heater(30.0)],
    },
    "tools": [_tool(0, [1], [210.0], [150.0])],
}


def test_rc2_tool_0_is_heater_1_not_heater_0_or_tool_number_plus_one(rc2):
    roles = build_heater_roles(rc2["heat"], rc2["tools"])
    assert roles["tool-0"]["heater"] == 1
    assert roles["tool-0"]["label"] == "Tool 0"
    assert heater_value(rc2["heat"], rc2["tools"], roles["tool-0"], "current") == (
        rc2["heat"]["heaters"][1]["current"]
    )
    assert heater_value(rc2["heat"], rc2["tools"], roles["tool-0"], "standby") == 175.0


def test_rc2_bed_and_chamber_come_from_the_mapping(rc2):
    assert "bedHeaters" not in rc2["heat"]  # 3.7 only reports the mapping
    roles = build_heater_roles(rc2["heat"], rc2["tools"])
    assert (roles["bed"]["heater"], roles["bed"]["label"]) == (0, "Bed")
    assert (roles["chamber"]["heater"], roles["chamber"]["label"]) == (2, "Chamber")
    assert list(roles) == ["tool-0", "bed", "chamber"]


def test_rc2_bed_and_chamber_have_no_standby_sensor(rc2):
    roles = build_heater_roles(rc2["heat"], rc2["tools"])
    assert roles["bed"]["types"] == ("current", "active")
    assert roles["tool-0"]["types"] == ("current", "active", "standby")


def test_3_6_uses_the_legacy_flat_lists():
    roles = build_heater_roles(RRF_3_6["heat"], RRF_3_6["tools"])
    assert list(roles) == ["tool-0", "bed"]  # chamber is all -1, so no chamber
    assert roles["bed"]["heater"] == 0
    assert roles["tool-0"]["heater"] == 1


def test_mapping_wins_over_legacy_keys_when_both_exist():
    heat = dict(RRF_3_6["heat"], bedHeaterMapping=[[2]])
    assert build_heater_roles(heat, RRF_3_6["tools"])["bed"]["heater"] == 2


def test_role_keys_do_not_depend_on_heater_numbering():
    """Same printer, heaters wired in another order: ids (and so history) are unchanged."""
    swapped = {
        "bedHeaterMapping": [[2]],
        "heaters": [_heater(30.0), _heater(200.0), _heater(60.0)],
    }
    tools = [_tool(0, [0], [210.0], [150.0])]  # was heater 1 in RRF_3_6
    roles = build_heater_roles(swapped, tools)
    assert list(roles) == list(build_heater_roles(RRF_3_6["heat"], RRF_3_6["tools"]))
    assert (roles["tool-0"]["heater"], roles["bed"]["heater"]) == (0, 2)


def test_two_tools_each_read_their_own_heater():
    heat = {
        "bedHeaterMapping": [[0]],
        "heaters": [_heater(60.0), _heater(111.0), _heater(222.0)],
    }
    tools = [_tool(0, [2], [200.0], [150.0]), _tool(1, [1], [210.0], [160.0])]
    roles = build_heater_roles(heat, tools)
    assert heater_value(heat, tools, roles["tool-0"], "current") == 222.0
    assert heater_value(heat, tools, roles["tool-1"], "current") == 111.0
    assert heater_value(heat, tools, roles["tool-1"], "active") == 210.0


def test_tool_sharing_a_heater_reports_its_own_targets():
    heat = {"heaters": [_heater(50.0, active=999.0)]}
    tools = [_tool(0, [0], [200.0], [150.0]), _tool(1, [0], [230.0], [170.0])]
    roles = build_heater_roles(heat, tools)
    assert heater_value(heat, tools, roles["tool-0"], "active") == 200.0
    assert heater_value(heat, tools, roles["tool-1"], "active") == 230.0


def test_tool_with_several_heaters_gets_one_role_per_heater():
    heat = {"heaters": [_heater(10.0), _heater(20.0)]}
    tools = [_tool(0, [0, 1], [200.0, 205.0], [150.0, 155.0])]
    roles = build_heater_roles(heat, tools)
    assert list(roles) == ["tool-0", "tool-0-h1"]
    assert roles["tool-0-h1"]["label"] == "Tool 0 heater 1"
    assert heater_value(heat, tools, roles["tool-0-h1"], "current") == 20.0
    assert heater_value(heat, tools, roles["tool-0-h1"], "standby") == 155.0


def test_several_beds_and_heaters_per_bed():
    heat = {
        "bedHeaterMapping": [[0, 1], [], [2]],
        "heaters": [_heater(1.0), _heater(2.0), _heater(3.0)],
    }
    roles = build_heater_roles(heat, [])
    assert list(roles) == ["bed", "bed-0-1", "bed-2-0"]
    assert roles["bed-2-0"]["label"] == "Bed 2"


def test_tool_numbers_need_not_be_contiguous_and_gaps_are_skipped():
    heat = {"heaters": [_heater(1.0), _heater(2.0)]}
    tools = [None, _tool(1, [1], [0.0], [0.0])]
    assert list(build_heater_roles(heat, tools)) == ["tool-1"]


def test_unused_heaters_and_dangling_references_get_no_role():
    heat = {"heaters": [_heater(1.0), _heater(2.0), _heater(3.0)], "bedHeaterMapping": [[7]]}
    tools = [_tool(0, [1, 9, -1], [0.0], [0.0])]
    assert list(build_heater_roles(heat, tools)) == ["tool-0"]


def test_sensor_fault_is_unknown_not_minus_273():
    heat = {"heaters": [_heater(-273.1)]}
    tools = [_tool(0, [0], [0.0], [0.0])]
    role = build_heater_roles(heat, tools)["tool-0"]
    assert heater_value(heat, tools, role, "current") is None


def test_active_falls_back_to_the_heater_when_the_tool_has_no_target():
    heat = {"heaters": [_heater(25.0, active=190.0)]}
    tools = [{"number": 0, "heaters": [0]}]
    role = build_heater_roles(heat, tools)["tool-0"]
    assert heater_value(heat, tools, role, "active") == 190.0


@pytest.mark.parametrize("junk", [None, "", 0, [], {}, [None], "unknown"])
def test_malformed_model_yields_nothing_rather_than_raising(junk):
    # rr_model answers "" for a key with no value
    assert build_heater_roles(junk, junk) == {}
    assert build_heater_roles({"heaters": junk}, junk) == {}
    assert build_heater_roles({"heaters": [_heater(1.0)]}, [{"heaters": junk}]) == {}


def test_resolve_follows_dicts_and_indices_and_never_raises():
    data = {"status": {"heat": {"heaters": [{"current": 1.5}]}, "state": {"status": "idle"}}}
    assert resolve(data, "status.state.status") == "idle"
    assert resolve(data, "status.heat.heaters[0].current") == 1.5
    assert resolve(data, "status.heat.heaters[1].current") is None
    assert resolve(data, "status.nope.deeper") is None
    assert resolve(data, "status.state.status.deeper") is None
    assert resolve(None, "status.state") is None
    assert resolve({"status": ""}, "status.state") is None
