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
        if server.fail:
            return web.Response(status=503)
        if server.emulate_job_control and request.query.get("key") == "state":
            # A pause takes one poll to finish, as a real one does.
            if model["state"]["status"] == "pausing":
                model["state"]["status"] = "paused"
        try:
            result = resolve_key(model, request.query["key"])
        except (KeyError, TypeError):
            return web.json_response({"key": request.query["key"], "flags": "", "result": None})
        return web.json_response({"key": request.query["key"], "flags": "", "result": result})

    async def rr_gcode(request):
        apply_gcode(request.query["gcode"])
        return web.json_response({"buff": 100})

    def apply_gcode(code):
        """Record ``code``, and refuse or emulate what the firmware would (see GCodes2.cpp)."""
        gcodes.append(code)
        state = model["state"]
        if code == "M0" and state["status"] != "paused":
            # RRF replies with this text and does nothing; over HTTP there is no error.
            server.refused.append(("M0", "Pause the print before attempting to cancel it"))
        if not server.emulate_job_control:
            return
        if code == "M25" and state["status"] in ("processing", "simulating"):
            state["status"] = "pausing"
        elif code == "M0" and state["status"] == "paused":
            state["status"] = "idle"
            model["job"]["lastFileCancelled"] = True
        elif code == "M24" and state["status"] == "paused":
            state["status"] = "processing"

    async def rr_thumbnail(request):
        key = (request.query.get("name", ""), int(request.query.get("offset", 0)))
        server.thumbnail_requests.append(key)
        if key not in server.thumbnails:
            return web.json_response({"err": 1})
        data, more = server.thumbnails[key]
        return web.json_response(
            {"fileName": key[0], "offset": key[1], "data": data, "next": more, "err": 0}
        )

    async def rr_connect(request):
        if server.password and request.query.get("password") != server.password:
            return web.json_response({"err": 1})
        return web.json_response({"err": 0, "sessionTimeout": 8000, "apiLevel": 2})

    async def rr_filelist(request):
        directory = request.query.get("dir", "0:/")
        first = int(request.query.get("first", 0))
        everything = server.files.get(directory)
        if everything is None:
            return web.json_response({"err": 1, "dir": directory, "first": first, "files": []})
        page = everything[first : first + server.page_size]
        more = first + server.page_size
        return web.json_response(
            {
                "dir": directory,
                "first": first,
                "files": page,
                "next": more if more < len(everything) else 0,
            }
        )

    async def rr_download(request):
        name = request.query.get("name", "")
        if name not in server.downloads:
            return web.Response(status=404)
        return web.json_response(server.downloads[name])

    async def webcam_snapshot(request):
        return web.Response(body=server.webcam_frame, content_type="image/jpeg")

    async def webcam_stream(request):
        # An MJPEG stream: parts forever; the test closes the connection after a frame.
        response = web.StreamResponse(
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        )
        await response.prepare(request)
        for _ in range(3):
            await response.write(
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + server.webcam_frame + b"\r\n"
            )
        return response

    async def web_page(request):
        # A real board answers any unknown URL with its web UI and status 200.
        return web.Response(text="<html>Duet Web Control</html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/rr_model", rr_model)
    app.router.add_get("/rr_gcode", rr_gcode)
    app.router.add_get("/rr_connect", rr_connect)
    app.router.add_get("/rr_filelist", rr_filelist)
    app.router.add_get("/rr_download", rr_download)
    app.router.add_get("/rr_thumbnail", rr_thumbnail)
    app.router.add_get("/webcam/snapshot", webcam_snapshot)
    app.router.add_get("/webcam/stream", webcam_stream)
    app.router.add_get("/{tail:.*}", web_page)
    server = TestServer(app)
    await server.start_server()
    server.model = model
    server.requests = requests
    server.gcodes = gcodes
    server.fail = False  # True: rr_model answers 503, as a busy board does
    server.password = ""
    # Every (code, reply) the firmware would have refused or ignored.
    server.refused = []
    # True: M25/M0/M24 move ``state.status`` as the firmware does. Off, tests set it by hand.
    server.emulate_job_control = False
    # rr_thumbnail: (file name, offset) -> (base64 text, next offset or 0)
    server.thumbnails = {}
    server.thumbnail_requests = []
    # rr_filelist: directory -> entries, paged ``page_size`` at a time with ``next``
    server.files = {"0:/macros": [], "0:/macros/": []}
    server.page_size = 2**31
    # rr_download: file name -> JSON document
    server.downloads = {}
    server.webcam_frame = b"\xff\xd8\xff\xe0JFIF-fake-image\xff\xd9"
    return server


async def start_sbc_server(model=None):
    """A fake DSF: ``/machine/status``, ``/machine/code``, ``/machine/directory``, ``/machine/file``.

    ``server.codes`` records ``(code, async query value)``; ``server.code_replies`` maps a
    code to its reply text. With ``server.password`` set, every request needs the
    ``X-Session-Key`` that ``/machine/connect`` hands out for the right password.
    """
    model = copy.deepcopy(MODEL) if model is None else model
    codes = []

    def authorised(request):
        return not server.password or request.headers.get("X-Session-Key") == server.session_key

    async def connect(request):
        server.connects += 1
        if server.password and request.query.get("password", "") != server.password:
            return web.Response(status=403)
        return web.json_response({"sessionKey": server.session_key})

    async def status(request):
        if server.fail:
            return web.Response(status=503)
        if not authorised(request):
            return web.Response(status=401)
        return web.json_response(model)

    async def code(request):
        if not authorised(request):
            return web.Response(status=401)
        body = (await request.read()).decode()
        codes.append((body, request.query.get("async")))
        return web.Response(text=server.code_replies.get(body, ""), content_type="text/plain")

    async def directory(request):
        if not authorised(request):
            return web.Response(status=401)
        path = request.match_info["path"]
        entries = server.files.get(path)
        if entries is None:
            return web.Response(status=404)
        return web.json_response(entries)

    async def file(request):
        if not authorised(request):
            return web.Response(status=401)
        path = request.match_info["path"]
        if path not in server.downloads:
            return web.Response(status=404)
        return web.json_response(server.downloads[path])

    app = web.Application()
    app.router.add_get("/machine/connect", connect)
    app.router.add_get("/machine/status", status)
    app.router.add_post("/machine/code", code)
    app.router.add_get("/machine/directory/{path:.*}", directory)
    app.router.add_get("/machine/file/{path:.*}", file)
    server = TestServer(app)
    await server.start_server()
    server.model = model
    server.codes = codes
    server.code_replies = {}
    server.fail = False
    server.password = ""
    server.session_key = "fake-session-key"
    server.connects = 0
    server.files = {"0:/macros": []}
    server.downloads = {}
    return server


def macro_file(name, kind="f"):
    return {"type": kind, "name": name, "size": 100, "date": "2026-01-01T00:00:00"}


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


async def setup_sbc_entry(hass, server, entry=None, **extra):
    """Set up the integration against a fake DSF (``standalone: False``)."""
    entry = entry or entry_for(server, standalone=False, **extra)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


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


def job_poll(server, status, *, extruded=None, tool=0, **job):
    """Put the fake printer in ``status`` as it would look during a job.

    Outside a job (``idle``/``off``/``halted``) the firmware reports no file, no
    duration and a null ``rawExtrusion``, which is what the consumers must cope with.
    """
    in_job = status not in ("idle", "off", "halted")
    model = server.model
    model["state"]["status"] = status
    model["state"]["currentTool"] = tool
    model["job"]["rawExtrusion"] = extruded
    model["job"]["duration"] = 60 if in_job else None
    model["job"]["file"]["fileName"] = "0:/gcodes/benchy.gcode" if in_job else None
    model["job"].update(job)


async def step(hass, entry, server, status, **kwargs):
    """``job_poll`` then one coordinator poll."""
    job_poll(server, status, **kwargs)
    await refresh(hass, entry)


def collect_events(hass):
    """Every ``duet3d_event`` fired from now on, in order."""
    events = []
    hass.bus.async_listen("duet3d_event", lambda event: events.append(dict(event.data)))
    return events
