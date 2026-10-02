"""Buttons, numbers, fans, macros and the filament and e-stop services."""
import pytest

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from fake_duet import DOMAIN, macro_file, refresh, setup_entry, state_of

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def entity_id(hass, entry, domain, unique_id):
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{unique_id}-{entry.entry_id}")
    assert found, f"no {domain} {unique_id}"
    return found


async def press(hass, entry, unique_id):
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id(hass, entry, "button", unique_id)}, blocking=True
    )


def status(server, value):
    server.model["state"]["status"] = value


# --- buttons ---------------------------------------------------------------


async def test_pause_resume_cancel_buttons_send_what_the_services_send(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    status(fake_duet, "processing")
    await press(hass, entry, "button-pause")
    status(fake_duet, "paused")
    await press(hass, entry, "button-resume")
    await press(hass, entry, "button-cancel")
    assert fake_duet.gcodes == ["M25", "M24", "M0"]


@pytest.mark.parametrize(
    "button, state",
    [("button-pause", "idle"), ("button-resume", "processing"), ("button-cancel", "idle"),
     ("button-home-all", "processing"), ("button-home-x", "paused")],
)
async def test_buttons_are_refused_in_the_wrong_state(hass, fake_duet, button, state):
    entry = await setup_entry(hass, fake_duet)
    status(fake_duet, state)
    with pytest.raises(ServiceValidationError, match=state):
        await press(hass, entry, button)
    assert fake_duet.gcodes == []


async def test_home_buttons_for_the_axes_the_printer_has(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    # the fake printer has X and Y only
    assert er.async_get(hass).async_get_entity_id("button", DOMAIN, f"button-home-z-{entry.entry_id}") is None
    await press(hass, entry, "button-home-all")
    await press(hass, entry, "button-home-x")
    await press(hass, entry, "button-home-y")
    assert fake_duet.gcodes == ["G28", "G28 X", "G28 Y"]


async def test_buttons_stay_available_whatever_the_state(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    status(fake_duet, "processing")
    await refresh(hass, entry)
    state = hass.states.get(entity_id(hass, entry, "button", "button-home-all"))
    assert state.state != "unavailable"


async def test_acknowledge_button_needs_a_message(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="no message"):
        await press(hass, entry, "button-acknowledge")
    fake_duet.model["state"]["messageBox"] = {"message": "Change filament", "mode": 2}
    await press(hass, entry, "button-acknowledge")
    assert fake_duet.gcodes == ["M292"]


async def test_emergency_stop_and_reset_are_off_by_default_and_gated_correctly(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    registry = er.async_get(hass)
    for unique_id in ("button-emergency-stop", "button-reset-after-emergency-stop"):
        reg = registry.async_get(entity_id(hass, entry, "button", unique_id))
        assert reg.disabled_by == er.RegistryEntryDisabler.INTEGRATION

    # enabled, e-stop works in any state and reset only when halted
    for unique_id in ("button-emergency-stop", "button-reset-after-emergency-stop"):
        registry.async_update_entity(entity_id(hass, entry, "button", unique_id), disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    status(fake_duet, "processing")
    await press(hass, entry, "button-emergency-stop")
    assert fake_duet.gcodes == ["M112"]
    with pytest.raises(ServiceValidationError, match="processing"):
        await press(hass, entry, "button-reset-after-emergency-stop")  # not halted
    status(fake_duet, "halted")
    await press(hass, entry, "button-reset-after-emergency-stop")
    assert fake_duet.gcodes == ["M112", "M999"]


async def test_emergency_stop_service_is_never_refused_and_reset_needs_halted(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    for state in ("idle", "processing", "paused"):
        status(fake_duet, state)
        await hass.services.async_call(DOMAIN, "emergency_stop", {}, blocking=True)
    assert fake_duet.gcodes == ["M112"] * 3
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reset_after_emergency_stop", {}, blocking=True)
    status(fake_duet, "halted")
    await hass.services.async_call(DOMAIN, "reset_after_emergency_stop", {}, blocking=True)
    assert fake_duet.gcodes[-1] == "M999"


# --- numbers ---------------------------------------------------------------


async def set_number(hass, entry, unique_id, value):
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": entity_id(hass, entry, "number", unique_id), "value": value},
        blocking=True,
    )


async def test_tool_bed_and_chamber_targets(hass, fake_duet):
    fake_duet.model["heat"]["chamberHeaterMapping"] = [[]]
    entry = await setup_entry(hass, fake_duet)
    tool = hass.states.get(entity_id(hass, entry, "number", "target-tool-0"))
    assert tool.state == "210.0"
    assert tool.attributes["unit_of_measurement"] == "°C"
    await set_number(hass, entry, "target-tool-0", 215)
    await set_number(hass, entry, "target-tool-1", 230)
    await set_number(hass, entry, "target-bed", 60)
    assert fake_duet.gcodes == ["M104 S215 T0", "M104 S230 T1", "M140 P0 S60"]


async def test_chamber_target_uses_m141(hass, fake_duet):
    fake_duet.model["heat"]["chamberHeaterMapping"] = [[1]]
    entry = await setup_entry(hass, fake_duet)
    await set_number(hass, entry, "target-chamber", 40)
    assert fake_duet.gcodes == ["M141 P0 S40"]


async def test_the_target_maximum_is_the_heaters_limit_less_a_margin(hass, fake_duet):
    fake_duet.model["heat"]["heaters"][2]["max"] = 300
    entry = await setup_entry(hass, fake_duet)
    assert hass.states.get(entity_id(hass, entry, "number", "target-tool-0")).attributes["max"] == 285
    # no reported limit: a conservative default
    assert hass.states.get(entity_id(hass, entry, "number", "target-tool-1")).attributes["max"] == 280


async def test_a_target_above_the_maximum_is_rejected_before_it_is_sent(hass, fake_duet):
    fake_duet.model["heat"]["heaters"][2]["max"] = 300
    entry = await setup_entry(hass, fake_duet)
    with pytest.raises(Exception, match="285"):
        await set_number(hass, entry, "target-tool-0", 290)
    assert fake_duet.gcodes == []


async def test_speed_and_flow_numbers(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert hass.states.get(entity_id(hass, entry, "number", "speed-factor")).state == "125.0"
    assert hass.states.get(entity_id(hass, entry, "number", "flow-control-1")).state == "95.0"
    await set_number(hass, entry, "speed-factor", 80)
    await set_number(hass, entry, "flow-control-1", 98)
    assert fake_duet.gcodes == ["M220 S80", "M221 D1 S98"]


async def test_numbers_work_in_any_state_like_dwc(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    status(fake_duet, "processing")
    await set_number(hass, entry, "speed-factor", 90)
    assert fake_duet.gcodes == ["M220 S90"]


async def test_the_read_only_sensors_keep_their_ids(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state_of(hass, entry, "Speed Factor").state == "125.0"
    assert state_of(hass, entry, "flow-0").state == "100.0"


# --- fans ------------------------------------------------------------------


async def test_a_fan_per_defined_fan_with_percentage_from_the_request(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fans = [e for e in er.async_get(hass).entities.values() if e.domain == "fan" and e.platform == DOMAIN]
    assert sorted(e.unique_id for e in fans) == [
        f"fan-control-0-{entry.entry_id}",
        f"fan-control-2-{entry.entry_id}",
    ]
    state = hass.states.get(entity_id(hass, entry, "fan", "fan-control-0"))
    assert state.state == "on"
    assert state.attributes["percentage"] == 60


async def test_fan_speed_and_off(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    fan = entity_id(hass, entry, "fan", "fan-control-0")
    await hass.services.async_call("fan", "set_percentage", {"entity_id": fan, "percentage": 35}, blocking=True)
    await hass.services.async_call("fan", "turn_on", {"entity_id": fan}, blocking=True)
    await hass.services.async_call("fan", "turn_off", {"entity_id": fan}, blocking=True)
    assert fake_duet.gcodes == ["M106 P0 S0.35", "M106 P0 S1.00", "M106 P0 S0.00"]


# --- macros ----------------------------------------------------------------


def macros(server, *names):
    server.files["0:/macros"] = [macro_file(n) if "." in n else macro_file(n, "d") for n in names]


async def enable_macros(hass, entry):
    registry = er.async_get(hass)
    for reg in list(registry.entities.values()):
        if reg.unique_id.startswith("macro-"):
            registry.async_update_entity(reg.entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def test_macros_become_disabled_buttons_files_only(hass, fake_duet):
    macros(fake_duet, "SET_STATUS.g", "SET_PROGRESS.g", "folder")
    entry = await setup_entry(hass, fake_duet)
    registered = sorted(
        e.unique_id for e in er.async_get(hass).entities.values() if e.unique_id.startswith("macro-")
    )
    assert registered == [f"macro-SET_PROGRESS.g-{entry.entry_id}", f"macro-SET_STATUS.g-{entry.entry_id}"]
    for reg in er.async_get(hass).entities.values():
        if reg.unique_id.startswith("macro-"):
            assert reg.disabled_by == er.RegistryEntryDisabler.INTEGRATION


async def test_pressing_a_macro_runs_it_and_is_refused_during_a_job(hass, fake_duet):
    macros(fake_duet, "SET_STATUS.g")
    entry = await setup_entry(hass, fake_duet)
    await enable_macros(hass, entry)
    await press(hass, entry, "macro-SET_STATUS.g")
    assert fake_duet.gcodes == ['M98 P"0:/macros/SET_STATUS.g"']
    status(fake_duet, "processing")
    with pytest.raises(ServiceValidationError, match="processing"):
        await press(hass, entry, "macro-SET_STATUS.g")
    assert len(fake_duet.gcodes) == 1


async def test_macro_names_that_could_break_out_of_the_string_get_no_button(hass, fake_duet):
    macros(fake_duet, 'bad".g', "ok.g", "semi;M112.g")
    entry = await setup_entry(hass, fake_duet)
    registered = [e.unique_id for e in er.async_get(hass).entities.values() if e.unique_id.startswith("macro-")]
    assert registered == [f"macro-ok.g-{entry.entry_id}"]


async def test_macro_listing_follows_pages(hass, fake_duet):
    macros(fake_duet, "a.g", "b.g", "c.g", "d.g", "e.g")
    fake_duet.page_size = 2
    entry = await setup_entry(hass, fake_duet)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert coordinator.macros == ["a.g", "b.g", "c.g", "d.g", "e.g"]


async def test_a_missing_macro_directory_does_not_break_the_poll(hass, fake_duet):
    del fake_duet.files["0:/macros"]
    entry = await setup_entry(hass, fake_duet)
    assert hass.data[DOMAIN][entry.entry_id]["coordinator"].macros == []
    assert state_of(hass, entry, "Current State").state == "idle"


async def test_macros_are_only_listed_once_a_minute(hass, fake_duet):
    macros(fake_duet, "a.g")
    entry = await setup_entry(hass, fake_duet)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    macros(fake_duet, "a.g", "b.g")
    await refresh(hass, entry)
    assert coordinator.macros == ["a.g"]
    coordinator._macros_fetched_at = None
    await refresh(hass, entry)
    assert coordinator.macros == ["a.g", "b.g"]


# --- filament services -----------------------------------------------------


async def test_load_filament_runs_m701_for_the_selected_tool(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 0
    await setup_entry(hass, fake_duet)
    await hass.services.async_call(DOMAIN, "load_filament", {"tool": 0, "filament": "PLA"}, blocking=True)
    assert fake_duet.gcodes == ['M701 S"PLA"']


async def test_unload_filament_runs_m702(hass, fake_duet):
    fake_duet.model["state"]["currentTool"] = 1
    await setup_entry(hass, fake_duet)
    await hass.services.async_call(DOMAIN, "unload_filament", {"tool": 1}, blocking=True)
    assert fake_duet.gcodes == ["M702"]


@pytest.mark.parametrize("selected", [-1, 1])
async def test_filament_services_refuse_a_tool_that_is_not_selected(hass, fake_duet, selected):
    """M701/M702 have no tool parameter; selecting a tool would run a tool change."""
    fake_duet.model["state"]["currentTool"] = selected
    await setup_entry(hass, fake_duet)
    with pytest.raises(ServiceValidationError, match="not selected"):
        await hass.services.async_call(DOMAIN, "load_filament", {"tool": 0, "filament": "PLA"}, blocking=True)
    with pytest.raises(ServiceValidationError, match="not selected"):
        await hass.services.async_call(DOMAIN, "unload_filament", {"tool": 0}, blocking=True)
    assert fake_duet.gcodes == []


@pytest.mark.parametrize("state", ["processing", "paused", "simulating", "cancelling"])
async def test_filament_services_are_refused_during_a_job(hass, fake_duet, state):
    fake_duet.model["state"]["currentTool"] = 0
    await setup_entry(hass, fake_duet)
    status(fake_duet, state)
    with pytest.raises(ServiceValidationError, match=state):
        await hass.services.async_call(DOMAIN, "load_filament", {"tool": 0, "filament": "PLA"}, blocking=True)
    with pytest.raises(ServiceValidationError, match=state):
        await hass.services.async_call(DOMAIN, "unload_filament", {"tool": 0}, blocking=True)
    assert fake_duet.gcodes == []


@pytest.mark.parametrize("name", ['PLA"; M112; "', "PLA\nM112", "a;b", ""])
async def test_filament_names_cannot_inject_gcode(hass, fake_duet, name):
    import voluptuous as vol

    fake_duet.model["state"]["currentTool"] = 0
    await setup_entry(hass, fake_duet)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "load_filament", {"tool": 0, "filament": name}, blocking=True)
    assert fake_duet.gcodes == []
