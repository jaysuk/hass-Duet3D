"""LED strips come from the object model (``ledStrips[]``, ``maxLeds`` is M950's ``U``)."""
import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.duet3d.hardware import build_led_strips
from fake_duet import DOMAIN, by_unique_id_prefix, refresh, setup_entry


def strip(max_leds=60, kind="neopixel"):
    return {"colorOrder": 0, "maxLeds": max_leds, "stopMovement": False, "type": kind, "pin": "led"}


def lights(hass):
    return {e.unique_id: e.entity_id for e in by_unique_id_prefix(hass, "LED")}


async def turn_on(hass, entity_id, **data):
    await hass.services.async_call("light", "turn_on", {"entity_id": entity_id, **data}, blocking=True)


async def read_strips_now(hass, entry):
    """The strips are read on the slow poll; make it due."""
    hass.data[DOMAIN][entry.entry_id]["coordinator"]._slow_fetched_at = None
    await refresh(hass, entry)


def test_a_strip_is_described_by_its_object_model_entry():
    assert build_led_strips([strip(60), None, strip(8, "dotstar")]) == {
        "led-0": {"index": 0, "label": "LED", "count": 60, "type": "neopixel", "rgbw": False},
        "led-2": {"index": 2, "label": "LED 2", "count": 8, "type": "dotstar", "rgbw": False},
    }


@pytest.mark.parametrize(
    "kind, rgbw",
    [("NeoPixel_RGBW", True), ("neopixel_rgbw", True), ("NeoPixel_RGB", False), ("DotStar", False), ("", False), (None, False)],
)
def test_only_an_rgbw_neopixel_strip_has_a_white_channel(kind, rgbw):
    assert build_led_strips([strip(3, kind)])["led-0"]["rgbw"] is rgbw


@pytest.mark.parametrize("junk", [None, "", {}, 5, [None], ["x"]])
def test_junk_in_the_strip_list_gives_no_strips(junk):
    assert build_led_strips(junk) == {}


@pytest.mark.parametrize("max_leds", [None, "", 0, -1, True])
def test_a_strip_with_no_usable_length_has_no_count(max_leds):
    assert build_led_strips([strip(max_leds)])["led-0"]["count"] is None


async def test_a_printer_without_a_strip_has_no_light(hass, fake_duet):
    await setup_entry(hass, fake_duet)
    assert lights(hass) == {}


async def test_a_configured_strip_becomes_a_light_with_no_setup(hass, fake_duet):
    fake_duet.model["ledStrips"] = [strip(60)]
    entry = await setup_entry(hass, fake_duet)
    # strip 0 keeps the unique id the single configured light always had
    assert lights(hass) == {f"LED-{entry.entry_id}": "light.voron_led"}
    assert hass.states.get("light.voron_led").state == "off"


async def test_the_whole_strip_is_set_using_the_length_from_the_board(hass, fake_duet):
    fake_duet.model["ledStrips"] = [strip(60)]
    await setup_entry(hass, fake_duet)
    await turn_on(hass, "light.voron_led", rgb_color=[255, 0, 0], brightness=128)
    assert fake_duet.gcodes == ["M150 E0 R255 U0 B0 P128 S60"]
    state = hass.states.get("light.voron_led")
    assert state.state == "on" and state.attributes["rgb_color"] == (255, 0, 0)

    await hass.services.async_call("light", "turn_off", {"entity_id": "light.voron_led"}, blocking=True)
    assert fake_duet.gcodes[-1] == "M150 E0 R0 U0 B0 P0 S60"
    assert hass.states.get("light.voron_led").state == "off"


async def test_each_strip_is_its_own_light_and_empty_slots_are_skipped(hass, fake_duet):
    fake_duet.model["ledStrips"] = [None, strip(24)]
    entry = await setup_entry(hass, fake_duet)
    assert lights(hass) == {f"LED-1-{entry.entry_id}": "light.voron_led_1"}
    await turn_on(hass, "light.voron_led_1")
    assert fake_duet.gcodes == ["M150 E1 R255 U255 B255 P255 S24"]


async def test_two_strips_are_separate_lights(hass, fake_duet):
    fake_duet.model["ledStrips"] = [strip(60), strip(24)]
    entry = await setup_entry(hass, fake_duet)
    assert lights(hass) == {
        f"LED-{entry.entry_id}": "light.voron_led",
        f"LED-1-{entry.entry_id}": "light.voron_led_1",
    }
    await turn_on(hass, "light.voron_led_1", brightness=10)
    assert fake_duet.gcodes == ["M150 E1 R255 U255 B255 P10 S24"]


async def test_a_strip_added_later_appears_and_a_length_change_is_followed(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert lights(hass) == {}

    fake_duet.model["ledStrips"] = [strip(30)]
    await read_strips_now(hass, entry)
    assert hass.states.get("light.voron_led").state == "off"

    fake_duet.model["ledStrips"] = [strip(45)]  # M950 E0 ... U45
    await read_strips_now(hass, entry)
    await turn_on(hass, "light.voron_led")
    assert fake_duet.gcodes == ["M150 E0 R255 U255 B255 P255 S45"]


async def test_a_strip_that_is_deleted_is_unavailable(hass, fake_duet):
    fake_duet.model["ledStrips"] = [strip(60)]
    entry = await setup_entry(hass, fake_duet)
    fake_duet.model["ledStrips"] = []  # M950 E0 C"nil"
    await read_strips_now(hass, entry)
    assert hass.states.get("light.voron_led").state == "unavailable"


async def test_a_failed_light_command_raises_and_changes_nothing(hass, fake_duet, monkeypatch):
    fake_duet.model["ledStrips"] = [strip(60)]
    entry = await setup_entry(hass, fake_duet)

    async def broken(gcode, wait=False):
        raise OSError("down")

    monkeypatch.setattr(hass.data[DOMAIN][entry.entry_id]["coordinator"], "send_gcode", broken)
    with pytest.raises(HomeAssistantError):
        await turn_on(hass, "light.voron_led", rgb_color=[0, 255, 0])
    assert hass.states.get("light.voron_led").state == "off"


async def test_an_old_entry_with_the_retired_led_settings_still_loads(hass, fake_duet):
    """Entries made before the strips were discovered hold ``light`` and ``led_*``."""
    from fake_duet import entry_for

    fake_duet.model["ledStrips"] = [strip(60)]
    entry = entry_for(fake_duet, light=True, led_strip_index=0, led_count=3)
    await setup_entry(hass, fake_duet, entry)
    await turn_on(hass, "light.voron_led")
    # the board's length wins over the old setting
    assert fake_duet.gcodes == ["M150 E0 R255 U255 B255 P255 S60"]


# --- RGBW strips ---------------------------------------------------------------------------


async def test_an_rgbw_strip_is_an_rgbw_light_that_turns_on_to_white(hass, fake_duet):
    fake_duet.model["ledStrips"] = [strip(3, "NeoPixel_RGBW")]
    await setup_entry(hass, fake_duet)
    state = hass.states.get("light.voron_led")
    assert state.attributes["supported_color_modes"] == ["rgbw"]

    await turn_on(hass, "light.voron_led")
    assert fake_duet.gcodes == ["M150 E0 R0 U0 B0 W255 P255 S3"]
    state = hass.states.get("light.voron_led")
    assert state.attributes["color_mode"] == "rgbw" and state.attributes["rgbw_color"] == (0, 0, 0, 255)


async def test_an_rgbw_colour_is_sent_with_its_white_level(hass, fake_duet):
    fake_duet.model["ledStrips"] = [strip(3, "NeoPixel_RGBW")]
    await setup_entry(hass, fake_duet)
    await turn_on(hass, "light.voron_led", rgbw_color=[10, 20, 30, 40], brightness=100)
    assert fake_duet.gcodes == ["M150 E0 R10 U20 B30 W40 P100 S3"]
    assert hass.states.get("light.voron_led").attributes["rgbw_color"] == (10, 20, 30, 40)

    await hass.services.async_call("light", "turn_off", {"entity_id": "light.voron_led"}, blocking=True)
    assert fake_duet.gcodes[-1] == "M150 E0 R0 U0 B0 W0 P0 S3"

    # turned on again with nothing specified, it comes back as it was
    await turn_on(hass, "light.voron_led")
    assert fake_duet.gcodes[-1] == "M150 E0 R10 U20 B30 W40 P100 S3"


async def test_an_rgb_colour_asked_of_an_rgbw_strip_is_turned_into_rgbw(hass, fake_duet):
    """Home Assistant converts it, taking the white out of the colour."""
    fake_duet.model["ledStrips"] = [strip(3, "NeoPixel_RGBW")]
    await setup_entry(hass, fake_duet)
    await turn_on(hass, "light.voron_led", rgb_color=[255, 255, 255])
    assert fake_duet.gcodes == ["M150 E0 R0 U0 B0 W255 P255 S3"]


async def test_a_plain_rgb_strip_never_gets_a_white_value(hass, fake_duet):
    fake_duet.model["ledStrips"] = [strip(60, "NeoPixel_RGB")]
    await setup_entry(hass, fake_duet)
    assert hass.states.get("light.voron_led").attributes["supported_color_modes"] == ["rgb"]
    await turn_on(hass, "light.voron_led")
    assert fake_duet.gcodes == ["M150 E0 R255 U255 B255 P255 S60"]
