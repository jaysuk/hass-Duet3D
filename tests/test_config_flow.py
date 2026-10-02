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
    assert {"name", "host", "port", "password", "update_interval"} <= fields
    # tools, bed and chamber come from the object model, the mode is detected, and
    # the LED strip is configured in the options
    assert not {"number_of_tools", "bed", "standalone"} & fields
    assert not {"light", "led_strip_index", "led_count"} & fields


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
    fields = {field["name"] for field in result["data_schema"]}
    assert not {"number_of_tools", "bed", "standalone"} & fields
    # LED strips come from the object model, so there is nothing to ask about them
    assert fields == {"update_interval", "printing_interval", "webcam_url"}


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
            {"update_interval": 20},
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry", result
    assert entry.data["update_interval"] == 20
    # the detected mode is not an option and is left alone
    assert entry.data["standalone"] is True


async def test_printing_interval_defaults_to_5_and_round_trips_through_the_options(hass):
    from unittest.mock import patch

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "duet", "host": "192.168.69.1", "port": 80, "password": "", "ssl": False,
            "update_interval": 30, "light": False, "standalone": True,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.duet3d.async_setup_entry", return_value=True):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {
            str(key): key.default() for key in flow["data_schema"].schema if callable(key.default)
        }
        assert defaults["printing_interval"] == 5
        # left alone, the default is saved
        await hass.config_entries.options.async_configure(flow["flow_id"], {"update_interval": 30})
        assert entry.data["printing_interval"] == 5

        flow = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"update_interval": 30, "printing_interval": 2}
        )
        await hass.async_block_till_done()
    assert entry.data["printing_interval"] == 2

    # the form offers what was saved
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = {str(key): key.default() for key in flow["data_schema"].schema if callable(key.default)}
    assert defaults["printing_interval"] == 2


async def test_printing_interval_must_be_at_least_one_second(hass):
    import pytest
    from homeassistant.data_entry_flow import InvalidData

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "duet", "host": "h", "port": 80, "password": "", "ssl": False,
              "update_interval": 30, "light": False, "standalone": True},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"update_interval": 30, "printing_interval": 0}
        )


async def _submit(hass, server, **extra):
    from unittest.mock import patch

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
                **extra,
            },
        )
        await hass.async_block_till_done()
    return result


async def test_submitting_the_form_for_a_standalone_board_detects_standalone(hass, fake_duet):
    result = await _submit(hass, fake_duet)

    assert result["type"] == "create_entry", result
    assert result["data"]["host"] == fake_duet.host
    assert result["data"]["update_interval"] == 10
    assert result["data"]["standalone"] is True


async def test_submitting_the_form_for_an_sbc_board_detects_sbc(hass):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def status(request):
        return web.json_response({"state": {"status": "idle"}})

    app = web.Application()
    app.router.add_get("/machine/status", status)
    server = TestServer(app)
    await server.start_server()
    try:
        result = await _submit(hass, server)
    finally:
        await server.close()

    assert result["type"] == "create_entry", result
    assert result["data"]["standalone"] is False


async def test_a_wrong_password_is_reported_on_the_password_field(hass, fake_duet):
    fake_duet.password = "secret"
    result = await _submit(hass, fake_duet, password="wrong")

    assert result["type"] == "form"
    assert result["errors"] == {"password": "invalid_auth"}


async def test_something_that_is_not_a_duet_is_reported_on_the_host_field(hass):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def web_page(request):
        return web.Response(text="<html></html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", web_page)
    server = TestServer(app)
    await server.start_server()
    try:
        result = await _submit(hass, server)
    finally:
        await server.close()

    assert result["type"] == "form"
    assert result["errors"] == {"host": "not_a_duet"}
