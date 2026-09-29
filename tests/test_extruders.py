"""Tests for the extruder helpers.

The module is loaded by path: importing it as ``custom_components.duet3d.extruders``
would run the package ``__init__``, which needs Home Assistant installed.
"""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "custom_components" / "duet3d" / "extruders.py"
_spec = importlib.util.spec_from_file_location("duet3d_extruders", _PATH)
extruders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extruders)


def test_single_extruder_active():
    result = extruders.build_extruders(
        [{"filament": "PLA", "filamentDiameter": 1.75, "position": 12.5}],
        [{"number": 0, "extruders": [0]}],
        0,
    )
    assert result == [
        {
            "extruder": 0,
            "filament": "PLA",
            "filament_diameter": 1.75,
            "position": 12.5,
            "tools": [0],
            "active": True,
        }
    ]


def test_no_tool_selected_means_no_active_extruder():
    result = extruders.build_extruders(
        [{"filament": "PLA"}], [{"number": 0, "extruders": [0]}], -1
    )
    assert result[0]["active"] is False


def test_toolchanger_only_selected_tool_is_active():
    tools = [
        {"number": 0, "extruders": [0]},
        {"number": 1, "extruders": [1]},
    ]
    result = extruders.build_extruders(
        [{"filament": "PLA"}, {"filament": "PETG"}], tools, 1
    )
    assert [e["active"] for e in result] == [False, True]


def test_sparse_tools_array_is_tolerated():
    tools = [None, {"number": 1, "extruders": [0]}]
    assert extruders.tools_by_extruder(tools) == {0: [1]}
    assert extruders.active_extruders(tools, 1) == {0}


def test_mixing_tool_activates_all_its_extruders():
    tools = [{"number": 0, "extruders": [0, 1]}]
    assert extruders.active_extruders(tools, 0) == {0, 1}


def test_extruder_shared_by_two_tools():
    tools = [{"number": 0, "extruders": [0]}, {"number": 1, "extruders": [0]}]
    assert extruders.tools_by_extruder(tools) == {0: [0, 1]}
    assert extruders.active_extruders(tools, 1) == {0}


def test_missing_filament_becomes_empty_string():
    result = extruders.build_extruders([{}, {"filament": None}], [], -1)
    assert [e["filament"] for e in result] == ["", ""]


def test_untrusted_inputs_do_not_raise():
    # rr_model returns "" for keys with no value; the coordinator stores it as-is
    assert extruders.build_extruders("", "", "") == []
    assert extruders.build_extruders(None, None, None) == []
    assert extruders.active_extruders("", True) == set()
