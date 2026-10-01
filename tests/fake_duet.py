"""A fake standalone Duet for tests: serves ``rr_model`` from a dict tests can mutate."""
import copy

from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er

DOMAIN = "duet3d"

MODEL = {
    "boards": [
        {
            "canAddress": 0,
            "firmwareVersion": "3.5.4",
            "name": "Duet 3 Mini 5+",
            "shortName": "Mini5plus",
            "mcuTemp": {"current": 40.0},
            "vIn": {"current": 24.1},
            "v12": None,
            "freeRam": 50000,
        },
        {
            "canAddress": 124,
            "firmwareVersion": "3.5.4",
            "name": "Duet 3 Expansion SB2040",
            "shortName": "SB2040",
            "state": "running",
            "mcuTemp": {"current": 44.0},
            "vIn": {"current": 23.9},
            "v12": None,
            "freeRam": 200000,
        },
    ],
    # Heater numbers deliberately do not follow tool numbers: tool 0 is heater 2.
    "heat": {
        "bedHeaterMapping": [[0]],
        "chamberHeaterMapping": [[]],
        "heaters": [
            {"current": 60.0, "active": 65.0, "standby": 0.0, "state": "active", "avgPwm": 0.25},
            {"current": 201.0, "active": 211.0, "standby": 151.0, "state": "standby", "avgPwm": 0.0},
            {"current": 200.0, "active": 210.0, "standby": 150.0, "state": "active", "avgPwm": 0.5},
        ],
    },
    "state": {
        "status": "idle",
        "currentTool": -1,
        "displayMessage": "",
        "messageBox": None,
        "startupError": None,
    },
    "job": {
        "rawExtrusion": 0.0,
        "file": {
            "filament": [1000.0],
            "fileName": "0:/gcodes/benchy.gcode",
            "numLayers": 10,
            "thumbnails": [],
            "layerHeight": 0.2,
            "height": 48.0,
            "generatedBy": "PrusaSlicer 2.8",
        },
        "timesLeft": {"file": 100, "slicer": 100, "filament": 90},
        "duration": 5,
        "layer": 1,
        "warmUpDuration": 120,
        "lastFileName": "0:/gcodes/last/benchy_old.gcode",
    },
    "move": {
        "axes": [
            {"letter": "X", "machinePosition": 1.0, "homed": True, "visible": True},
            {"letter": "Y", "machinePosition": 2.0, "homed": True, "visible": True},
        ],
        "speedFactor": 1.25,
        "extruders": [
            {"filament": "", "filamentDiameter": 1.75, "position": 0.0, "factor": 1.0},
            {"filament": "PETG", "filamentDiameter": 1.75, "position": 0.0, "factor": 0.95},
        ],
    },
    "tools": [
        {"number": 0, "extruders": [0], "heaters": [2], "active": [210.0], "standby": [150.0]},
        {"number": 1, "extruders": [1], "heaters": [1], "active": [211.0], "standby": [151.0]},
    ],
    "fans": [
        {"name": "Part Cooling Fan", "actualValue": 0.5, "requestedValue": 0.6, "rpm": -1},
        None,
        {"name": "", "actualValue": 1.0, "requestedValue": 1.0, "rpm": 3000},
    ],
    "network": {"interfaces": [{"type": "wifi", "actualIP": "192.168.1.5", "signal": -55}]},
    "volumes": [
        {"mounted": True, "freeSpace": 3_000_000_000, "capacity": 4_000_000_000},
        {"mounted": False, "freeSpace": None, "capacity": None},
    ],
    "sensors": {
        "filamentMonitors": [
            {"enableMode": 1, "status": "ok", "type": "simple", "filamentPresent": True},
            None,
        ]
    },
}


def resolve_key(model, key):
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


async def start_server(model=None):
    """Start a fake Duet. ``server.model`` is live; ``requests``/``gcodes`` record traffic."""
    model = copy.deepcopy(MODEL) if model is None else model
    requests = []
    gcodes = []

    async def rr_model(request):
        requests.append((request.query.get("key"), request.query.get("flags")))
        try:
            result = resolve_key(model, request.query["key"])
        except (KeyError, TypeError):
            return web.json_response({"key": request.query["key"], "flags": "", "result": None})
        return web.json_response({"key": request.query["key"], "flags": "", "result": result})

    async def rr_gcode(request):
        gcodes.append(request.query["gcode"])
        return web.json_response({"buff": 100})

    app = web.Application()
    app.router.add_get("/rr_model", rr_model)
    app.router.add_get("/rr_gcode", rr_gcode)
    server = TestServer(app)
    await server.start_server()
    server.model = model
    server.requests = requests
    server.gcodes = gcodes
    return server


def entry_for(server, **extra):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{server.host}:{server.port}",
        title="Voron",
        data={
            "name": "Voron",
            "host": server.host,
            "port": server.port,
            "ssl": False,
            "password": "",
            "update_interval": 30,
            "light": False,
            "standalone": True,
            **extra,
        },
    )


async def setup_entry(hass, server, entry=None):
    entry = entry or entry_for(server)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def state_of(hass, entry, unique_id_prefix, domain="sensor"):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{unique_id_prefix}-{entry.entry_id}")
    assert entity_id, f"no {domain} {unique_id_prefix}"
    return hass.states.get(entity_id)


def has_entity(hass, entry, unique_id_prefix, domain="sensor"):
    registry = er.async_get(hass)
    return registry.async_get_entity_id(domain, DOMAIN, f"{unique_id_prefix}-{entry.entry_id}") is not None


async def refresh(hass, entry):
    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    await hass.async_block_till_done()


def by_unique_id_prefix(hass, prefix):
    registry = er.async_get(hass)
    return [e for e in registry.entities.values() if e.platform == DOMAIN and e.unique_id.startswith(prefix)]
