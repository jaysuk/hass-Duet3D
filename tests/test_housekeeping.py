"""Diagnostics and re-authentication."""
import json

from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH, SOURCE_RECONFIGURE
from homeassistant.components.diagnostics import REDACTED  # noqa: F401  (documents the marker)

from fake_duet import entry_for, refresh, setup_entry, start_server


# --- diagnostics -------------------------------------------------------------


async def get_diagnostics(hass, entry):
    from custom_components.duet3d.diagnostics import async_get_config_entry_diagnostics

    return await async_get_config_entry_diagnostics(hass, entry)


async def test_diagnostics_hold_neither_the_password_nor_the_address(hass, fake_duet):
    fake_duet.password = "hunter2"
    fake_duet.model["network"] = {
        "name": "mybigprinter",
        "hostname": "mybigprinter",
        "interfaces": [
            {"type": "wifi", "actualIP": "192.168.1.5", "configuredIP": "192.168.1.5",
             "gateway": "192.168.1.1", "dnsServer": "192.168.1.1", "mac": "AA:BB:CC:DD:EE:FF",
             "ssid": "HomeWifi", "signal": -55},
        ],
    }
    fake_duet.model["boards"][0]["uniqueId"] = "SECRETBOARDID"
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, password="hunter2"))
    dump = json.dumps(await get_diagnostics(hass, entry), default=str)

    for secret in (
        "hunter2", fake_duet.host, "192.168.1", "mybigprinter", "AA:BB:CC", "HomeWifi",
        "SECRETBOARDID", "benchy", "benchy_old",
    ):
        assert secret not in dump, secret


async def test_diagnostics_keep_what_is_needed_to_debug(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    dump = await get_diagnostics(hass, entry)
    assert dump["mode"] == "standalone"
    assert dump["firmware"] == "3.5.4"
    assert dump["poll"]["failed_polls_in_a_row"] == 0
    assert dump["poll"]["last_update_success"] is True
    assert "tool-0" in dump["heater_roles"]
    assert "fan-0" in dump["hardware"]["fans"]
    assert dump["status"]["state"]["status"] == "idle"
    assert dump["status"]["move"]["extruders"][1]["filament"] == "PETG"
    # the signal strength is useful and harmless
    assert dump["status"]["network"]["interfaces"][0]["signal"] == -55


async def test_diagnostics_are_registered_with_home_assistant(hass, hass_client, fake_duet):
    from homeassistant.setup import async_setup_component

    entry = await setup_entry(hass, fake_duet)
    assert await async_setup_component(hass, "diagnostics", {})
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == 200, await response.text()
    body = await response.json()
    assert body["data"]["mode"] == "standalone"


# --- reauth ------------------------------------------------------------------


async def test_a_rejected_password_starts_reauth_and_the_entry_is_not_set_up(hass, fake_duet):
    fake_duet.password = "right"
    entry = entry_for(fake_duet, password="wrong")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler("duet3d")
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_a_password_changed_while_running_starts_reauth(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, password="pw"))
    fake_duet.password = "changed"  # the board's password changed under a live session
    coordinator = hass.data["duet3d"][entry.entry_id]["coordinator"]
    coordinator._authenticated = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler("duet3d")
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_reauth_with_the_right_password_reloads_the_entry(hass, fake_duet):
    fake_duet.password = "right"
    entry = entry_for(fake_duet, password="wrong")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (flow,) = hass.config_entries.flow.async_progress_by_handler("duet3d")
    assert flow["step_id"] == "reauth_confirm"

    bad = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "still wrong"})
    assert bad["type"] == "form" and bad["errors"] == {"password": "invalid_auth"}

    done = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "right"})
    await hass.async_block_till_done()
    assert done["type"] == "abort" and done["reason"] == "reauth_successful"
    assert entry.data["password"] == "right"
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_form_is_served_over_http(hass, hass_client, fake_duet):
    from homeassistant.setup import async_setup_component

    fake_duet.password = "right"
    entry = entry_for(fake_duet, password="wrong")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, "config", {})
    (flow,) = hass.config_entries.flow.async_progress_by_handler("duet3d")
    client = await hass_client()
    response = await client.get(f"/api/config/config_entries/flow/{flow['flow_id']}")
    assert response.status == 200, await response.text()


async def test_the_password_is_sent_as_a_parameter_not_pasted_into_the_url(hass, fake_duet):
    """A password with & or # must not change what the board is asked."""
    fake_duet.password = "a&b#c d"
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, password="a&b#c d"))
    assert entry.state is ConfigEntryState.LOADED


# --- reconfigure -------------------------------------------------------------


async def test_reconfigure_moves_the_printer_without_changing_its_identity(hass, fake_duet):
    second = await start_server()
    try:
        entry = await setup_entry(hass, fake_duet)
        unique_id = entry.unique_id
        result = await entry.start_reconfigure_flow(hass)
        assert result["step_id"] == "reconfigure"
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": second.host, "port": second.port, "ssl": False, "password": ""},
        )
        await hass.async_block_till_done()
        assert done["type"] == "abort" and done["reason"] == "reconfigure_successful"
        assert entry.data["port"] == second.port
        assert entry.unique_id == unique_id
        assert entry.state is ConfigEntryState.LOADED
        assert entry.data["base_url"] == f"http://{second.host}:{second.port}"
    finally:
        await second.close()


async def test_reconfigure_reports_an_unreachable_address(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    result = await entry.start_reconfigure_flow(hass)
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": 1, "ssl": False, "password": ""}
    )
    assert bad["type"] == "form" and bad["errors"] == {"host": "cannot_connect"}
