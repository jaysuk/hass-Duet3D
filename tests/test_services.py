"""The printer actions. They send G-code, so what they refuse matters as much as what they send."""
from pathlib import Path

import pytest
import voluptuous as vol

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from fake_duet import DOMAIN, entry_for, setup_entry, start_server


async def call(hass, service, data=None):
    await hass.services.async_call(DOMAIN, service, data or {}, blocking=True)


def status(server, value):
    server.model["state"]["status"] = value


async def test_send_code_sends_exactly_what_it_is_given(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    await call(hass, "send_code", {"gcode": "M115"})
    assert fake_duet.gcodes == ["M115"]


async def test_send_code_has_no_state_check(hass, fake_duet):
    """It is the escape hatch: it works mid-print, which the named actions refuse."""
    await setup_entry(hass, fake_duet)
    status(fake_duet, "processing")
    await call(hass, "send_code", {"gcode": "M220 S80"})
    assert fake_duet.gcodes == ["M220 S80"]


# --- home ------------------------------------------------------------------


async def test_home_all_axes_when_idle(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    await call(hass, "home")
    assert fake_duet.gcodes == ["G28"]


async def test_home_chosen_axes_normalises_case(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    await call(hass, "home", {"axes": ["x", "Y"]})
    assert fake_duet.gcodes == ["G28 X Y"]
    await call(hass, "home", {"axes": "z"})
    assert fake_duet.gcodes[-1] == "G28 Z"


@pytest.mark.parametrize("bad", ["XY", "1", "X; M112", "", "X Y"])
async def test_home_rejects_anything_but_single_axis_letters(hass, fake_duet, bad):
    """The axes are pasted into G-code, so nothing but a letter gets through."""
    await setup_entry(hass, fake_duet)
    with pytest.raises(vol.Invalid):
        await call(hass, "home", {"axes": [bad]})
    assert fake_duet.gcodes == []


@pytest.mark.parametrize("busy", ["processing", "simulating", "paused", "pausing", "cancelling", "changingTool", "off", "halted"])
async def test_home_is_refused_unless_idle(hass, fake_duet, busy):
    await setup_entry(hass, fake_duet)
    status(fake_duet, busy)
    with pytest.raises(ServiceValidationError, match=busy):
        await call(hass, "home")
    assert fake_duet.gcodes == []


async def test_the_state_check_uses_the_printer_now_not_the_last_poll(hass, fake_duet):
    """A print can start between polls (30 s apart by default)."""
    await setup_entry(hass, fake_duet)  # last poll said idle
    status(fake_duet, "processing")  # a print started since
    with pytest.raises(ServiceValidationError):
        await call(hass, "home")
    assert fake_duet.gcodes == []


# --- pause / resume / cancel ----------------------------------------------


async def test_pause_a_running_job(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    status(fake_duet, "processing")
    await call(hass, "pause")
    assert fake_duet.gcodes == ["M25"]


@pytest.mark.parametrize("state", ["idle", "paused", "pausing", "off"])
async def test_pause_is_refused_unless_a_job_is_running(hass, fake_duet, state):
    await setup_entry(hass, fake_duet)
    status(fake_duet, state)
    with pytest.raises(ServiceValidationError):
        await call(hass, "pause")
    assert fake_duet.gcodes == []


async def test_resume_a_paused_job(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    status(fake_duet, "paused")
    await call(hass, "resume")
    assert fake_duet.gcodes == ["M24"]


@pytest.mark.parametrize("state", ["idle", "processing", "pausing"])
async def test_resume_is_refused_unless_paused(hass, fake_duet, state):
    await setup_entry(hass, fake_duet)
    status(fake_duet, state)
    with pytest.raises(ServiceValidationError):
        await call(hass, "resume")
    assert fake_duet.gcodes == []


async def test_cancel_a_paused_job_sends_only_m0(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    status(fake_duet, "paused")
    await call(hass, "cancel")
    assert fake_duet.gcodes == ["M0"]
    assert fake_duet.refused == []


async def test_cancel_with_no_job_does_not_run_stop_g(hass, fake_duet):
    """M0 with nothing printing is an error in the firmware (it only cancels a paused print), so refuse."""
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="idle"):
        await call(hass, "cancel")
    assert fake_duet.gcodes == []


# --- message box ----------------------------------------------------------


async def test_acknowledge_closes_the_message_box(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    fake_duet.model["state"]["messageBox"] = {"message": "Change filament", "mode": 2}
    await call(hass, "acknowledge_message")
    assert fake_duet.gcodes == ["M292"]


async def test_acknowledge_can_press_cancel(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    fake_duet.model["state"]["messageBox"] = {"message": "Continue?", "mode": 3}
    await call(hass, "acknowledge_message", {"cancel": True})
    assert fake_duet.gcodes == ["M292 P1"]


async def test_acknowledge_with_no_message_sends_nothing(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="no message"):
        await call(hass, "acknowledge_message")
    assert fake_duet.gcodes == []


# --- failures and targets -------------------------------------------------


async def test_unreachable_printer_is_reported_not_guessed_at(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    await fake_duet.close()
    with pytest.raises(HomeAssistantError, match="Voron"):
        await call(hass, "pause")


async def test_with_several_printers_the_caller_must_choose(hass, fake_duet):
    second = await start_server()
    try:
        first_entry = await setup_entry(hass, fake_duet)
        await setup_entry(hass, second, entry_for(second))

        with pytest.raises(ServiceValidationError, match="Choose which"):
            await call(hass, "send_code", {"gcode": "M115"})
        assert fake_duet.gcodes == second.gcodes == []

        (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), first_entry.entry_id)
        await call(hass, "send_code", {"gcode": "M115", "device_id": [device.id]})
        assert fake_duet.gcodes == ["M115"]
        assert second.gcodes == []
    finally:
        await second.close()


async def test_an_entity_target_picks_that_printer(hass, fake_duet):
    """services.yaml offers entities (hassfest bans a device filter), so the UI sends entity_id."""
    second = await start_server()
    try:
        first_entry = await setup_entry(hass, fake_duet)
        await setup_entry(hass, second, entry_for(second))

        entity = next(iter(er.async_entries_for_config_entry(er.async_get(hass), first_entry.entry_id)))
        await call(hass, "send_code", {"gcode": "M115", "entity_id": [entity.entity_id]})
        assert fake_duet.gcodes == ["M115"]
        assert second.gcodes == []
    finally:
        await second.close()


async def test_a_target_that_is_not_a_duet_is_refused(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="not a Duet3D printer"):
        await call(hass, "send_code", {"gcode": "M115", "device_id": ["nonexistent"]})
    assert fake_duet.gcodes == []


async def test_services_go_away_with_the_last_printer(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    for name in ("send_code", "home", "pause", "resume", "cancel", "acknowledge_message"):
        assert hass.services.has_service(DOMAIN, name)
    assert await hass.config_entries.async_unload(entry.entry_id)
    for name in ("send_code", "home", "pause", "resume", "cancel", "acknowledge_message"):
        assert not hass.services.has_service(DOMAIN, name)


def test_services_yaml_passes_home_assistants_own_schema():
    """A typo in services.yaml hides the actions' UI. Uses HA's private schema, so
    skip rather than fail if a future HA moves it."""
    from homeassistant.util.yaml import load_yaml_dict

    try:
        from homeassistant.helpers.service import _SERVICES_SCHEMA
    except ImportError:
        pytest.skip("HA no longer exposes its services.yaml schema here")

    path = Path(__file__).parent.parent / "custom_components" / "duet3d" / "services.yaml"
    services = _SERVICES_SCHEMA(load_yaml_dict(str(path)))
    assert set(services) == {
        "send_code", "home", "pause", "resume", "cancel", "acknowledge_message",
        "emergency_stop", "reset_after_emergency_stop", "load_filament", "unload_filament",
        "cancel_object",
    }
    for name, description in services.items():
        assert description["target"]["entity"][0]["integration"] == DOMAIN, name
        # hassfest rejects a device filter on a service target
        assert "device" not in description["target"], name
    assert "axes" in services["home"]["fields"]
    assert services["send_code"]["fields"]["gcode"]["required"] is True
