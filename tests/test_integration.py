"""End-to-end: the real integration polling a fake standalone Duet."""
import copy

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er

DOMAIN = "duet3d"

MODEL = {
    "boards": [{"firmwareVersion": "3.5.4", "name": "Duet 3 Mini 5+"}],
    "heat": {
        "heaters": [
            {"current": 60.0, "active": 60.0},
            {"current": 200.0, "active": 210.0, "standby": 150.0},
            {"current": 201.0, "active": 210.0, "standby": 150.0},
        ]
    },
    "state": {"status": "idle", "currentTool": -1},
    "job": {
        "rawExtrusion": 0.0,
        "file": {
            "filament": [1000.0],
            "fileName": "0:/gcodes/benchy.gcode",
            "numLayers": 10,
            "thumbnails": [],
        },
        "timesLeft": {"file": 100, "slicer": 100},
        "duration": 5,
        "layer": 1,
    },
    "move": {
        "axes": [{"letter": "X", "machinePosition": 1.0}],
        "extruders": [
            {"filament": "", "filamentDiameter": 1.75, "position": 0.0},
            {"filament": "PETG", "filamentDiameter": 1.75, "position": 0.0},
        ],
    },
    "tools": [
        {"number": 0, "extruders": [0]},
        {"number": 1, "extruders": [1]},
    ],
}


def _resolve(model, key):
    """Resolve an rr_model key: ``a.b``, ``a[1].b`` (one item), ``a[].b`` (every item)."""
    values = [model]
    many = False
    for part in key.split("."):
        name, _, index = part.partition("[")
        next_values = []
        for value in values:
            item = value[name]
            if not index:
                next_values.append(item)
            elif index == "]":
                many = True
                next_values.extend(item)
            else:
                next_values.append(item[int(index.rstrip("]"))])
        values = next_values
    return values if many else values[0]


@pytest.fixture
async def fake_duet():
    """A standalone-mode Duet whose model tests can mutate."""
    model = copy.deepcopy(MODEL)
    requests = []

    async def rr_model(request):
        requests.append((request.query.get("key"), request.query.get("flags")))
        try:
            result = _resolve(model, request.query["key"])
        except (KeyError, TypeError):
            return web.json_response({"key": request.query["key"], "flags": "", "result": None})
        return web.json_response({"key": request.query["key"], "flags": "", "result": result})

    app = web.Application()
    app.router.add_get("/rr_model", rr_model)
    server = TestServer(app)
    await server.start_server()
    server.model = model
    server.requests = requests
    yield server
    await server.close()


async def _setup(hass, server):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="127.0.0.1",
        data={
            "name": "Voron",
            "host": server.host,
            "port": server.port,
            "ssl": False,
            "password": "",
            "update_interval": 30,
            "number_of_tools": 2,
            "bed": True,
            "light": False,
            "standalone": True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _by_unique_id_prefix(hass, prefix):
    registry = er.async_get(hass)
    return [e for e in registry.entities.values() if e.platform == DOMAIN and e.unique_id.startswith(prefix)]


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
    fake_duet.model["job"]["rawExtrusion"] = 1234.567
    fake_duet.model["state"]["currentTool"] = 0
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


async def test_nested_keys_are_requested_with_full_depth(hass, fake_duet):
    """rr_model truncates nested objects unless asked for depth; extruders are nested."""
    await _setup(hass, fake_duet)
    flags = {key: f for key, f in fake_duet.requests}
    assert flags["move.extruders"] == "d99vn"
    assert flags["tools"] == "d99vn"
    # existing keys are untouched
    assert flags["state.status"] is None


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


async def test_extruder_appearing_later_gets_a_sensor(hass, fake_duet):
    """The firmware can gain an extruder (M584 in config-override, hot-plugged expansion)."""
    entry = await _setup(hass, fake_duet)
    fake_duet.model["move"]["extruders"].append({"filament": "ASA", "filamentDiameter": 1.75})
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"extruder-2-{entry.entry_id}")
