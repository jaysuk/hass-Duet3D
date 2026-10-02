"""Estimates and labelled objects, pure."""
from datetime import datetime, timezone

import pytest

from custom_components.duet3d.jobinfo import build_objects, eta, projected_total_minutes, slicer_total

T0 = datetime(2026, 10, 2, 12, 0, 20, 500, tzinfo=timezone.utc)


def test_eta_is_the_finish_time_to_the_nearest_minute():
    assert eta(T0, 3600) == datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
    assert eta(T0, 3600 + 9) == datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
    assert eta(T0, 3600 + 11) == datetime(2026, 10, 2, 13, 1, tzinfo=timezone.utc)


def test_eta_does_not_jitter_with_the_estimate_wobbling_by_seconds():
    assert eta(T0, 1800) == eta(T0, 1805) == eta(T0, 1795)


@pytest.mark.parametrize("left", [None, "", 0, -5, True, "300"])
def test_no_eta_without_a_usable_time_left(left):
    assert eta(T0, left) is None


def test_no_eta_without_a_read_time():
    assert eta(None, 300) is None


def test_projected_total_is_elapsed_plus_left_in_minutes():
    assert projected_total_minutes(600, 1800) == 40.0


@pytest.mark.parametrize("duration, left", [(None, 100), (100, None), (100, 0), ("", ""), (100, True)])
def test_no_projection_when_either_part_is_missing_or_the_job_is_done(duration, left):
    assert projected_total_minutes(duration, left) is None


def test_slicer_total_sums_every_extruder():
    assert slicer_total([1500.5, 20.0]) == 1520.5
    assert slicer_total([]) is None
    assert slicer_total(None) is None
    assert slicer_total("") is None


def test_objects_get_an_index_a_name_and_a_cancelled_flag():
    build = {
        "currentObject": 1,
        "objects": [
            {"name": "cube", "cancelled": False, "x": [0, 1], "y": [0, 1]},
            {"name": "", "cancelled": True},
            "junk",
            {"cancelled": "yes"},
        ],
    }
    assert build_objects(build) == [
        {"index": 0, "name": "cube", "cancelled": False},
        {"index": 1, "name": "Object 1", "cancelled": True},
        {"index": 3, "name": "Object 3", "cancelled": False},
    ]


@pytest.mark.parametrize("build", [None, "", [], {}, {"objects": None}, {"objects": ""}])
def test_no_objects_without_labelling(build):
    assert build_objects(build) == []
