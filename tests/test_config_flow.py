"""The config and options forms must survive Home Assistant's JSON serialisation.

The UI fetches a form over HTTP, and HA serialises its schema field by field. A
schema wrapped in a nested ``vol.Schema`` cannot be serialised, which surfaced as
"Config flow could not be loaded: 500 Internal Server Error" and made the
integration impossible to add.
"""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.setup import async_setup_component

DOMAIN = "duet3d"


async def test_user_form_is_served_over_http(hass, hass_client):
    """The first step of the config flow renders through the real HTTP API."""
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()

    response = await client.post(
        "/api/config/config_entries/flow",
        json={"handler": DOMAIN, "show_advanced_options": False},
    )

    assert response.status == 200, await response.text()
    result = await response.json()
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    fields = {field["name"] for field in result["data_schema"]}
    assert {"name", "host", "port", "update_interval", "standalone"} <= fields
    # tools, bed and chamber come from the object model
    assert not {"number_of_tools", "bed"} & fields


async def test_options_form_is_served_over_http(hass, hass_client):
    """The options form of an existing entry renders through the real HTTP API."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "duet",
            "host": "192.168.69.1",
            "port": 80,
            "password": "",
            "ssl": False,
            "update_interval": 10,
            "number_of_tools": 1,
            "bed": True,
            "light": False,
            "led_strip_index": 0,
            "led_count": 1,
            "standalone": True,
        },
    )
    entry.add_to_hass(hass)
    client = await hass_client()

    response = await client.post(
        "/api/config/config_entries/options/flow",
        json={"handler": entry.entry_id},
    )

    assert response.status == 200, await response.text()
    result = await response.json()
    assert result["type"] == "form"
    assert result["step_id"] == "init"
    # the entry above still carries the retired number_of_tools/bed keys
    assert not {"number_of_tools", "bed"} & {field["name"] for field in result["data_schema"]}


async def test_saving_options_keeps_working_for_an_entry_with_retired_keys(hass):
    from unittest.mock import patch

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "duet",
            "host": "192.168.69.1",
            "port": 80,
            "password": "",
            "ssl": False,
            "update_interval": 10,
            "number_of_tools": 1,
            "bed": True,
            "light": False,
            "standalone": True,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.duet3d.async_setup_entry", return_value=True):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"],
            {"update_interval": 20, "light": False, "standalone": True},
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry", result
    assert entry.data["update_interval"] == 20


async def test_submitting_the_form_creates_an_entry(hass):
    """A standalone printer that answers rr_connect can be added."""
    from unittest.mock import patch

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def rr_connect(request):
        return web.json_response({"err": 0})

    app = web.Application()
    app.router.add_get("/rr_connect", rr_connect)
    server = TestServer(app)
    await server.start_server()
    try:
        started = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert started["type"] == "form"

        with patch("custom_components.duet3d.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                started["flow_id"],
                {
                    "name": "duet",
                    "ssl": False,
                    "host": server.host,
                    "password": "",
                    "port": server.port,
                    "update_interval": 10,
                    "standalone": True,
                },
            )
            await hass.async_block_till_done()
    finally:
        await server.close()

    assert result["type"] == "create_entry", result
    assert result["data"]["host"] == server.host
    assert result["data"]["update_interval"] == 10
    assert result["data"]["standalone"] is True
