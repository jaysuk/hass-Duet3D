"""Webcam, firmware update entity and the Online sensor."""
import pytest

from homeassistant.components.camera import async_get_image
from homeassistant.helpers import entity_registry as er

from custom_components.duet3d.webcam import first_jpeg, parse_dwc_settings, resolve_url

from fake_duet import DOMAIN, entry_for, refresh, setup_entry, state_of

BASE = "http://192.168.1.5:80"
FRAME = b"\xff\xd8\xff\xe0JFIF-fake-image\xff\xd9"


# --- pure ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://{hostname}:8080/?action=stream", "http://192.168.1.5:8080/?action=stream"),
        ("https://cam.local/snap", "https://cam.local/snap"),
        ("/webcam/snapshot", "http://192.168.1.5:80/webcam/snapshot"),
        ("  http://x/y  ", "http://x/y"),
        ("", None),
        ("   ", None),
        (None, None),
        (5, None),
        ("rtsp://cam/stream", None),
        ("cam.local/snap", None),
    ],
)
def test_resolve_url(url, expected):
    assert resolve_url(url, "192.168.1.5", BASE) == expected


def test_dwc_settings_with_a_webcam():
    settings = {"webcam": {"enabled": True, "url": "http://[HOSTNAME]/s", "liveUrl": "http://x/l"}}
    assert parse_dwc_settings(settings, "h", BASE) == {"url": "http://h/s", "stream": False}


def test_dwc_placeholder_is_replaced_and_the_old_spelling_still_works():
    assert resolve_url("http://[HOSTNAME]:8081/0/stream", "h", BASE) == "http://h:8081/0/stream"
    assert resolve_url("http://{hostname}/x", "h", BASE) == "http://h/x"


@pytest.mark.parametrize("interval, stream", [(0, True), (5000, False), (None, False), (False, False)])
def test_an_update_interval_of_zero_means_dwc_shows_a_stream(interval, stream):
    settings = {"webcam": {"enabled": True, "url": "http://h/s", "updateInterval": interval}}
    assert parse_dwc_settings(settings, "h", BASE)["stream"] is stream


def test_live_url_is_ignored():
    settings = {"webcam": {"enabled": True, "url": "http://h/s", "liveUrl": "http://h/click"}}
    assert parse_dwc_settings(settings, "h", BASE) == {"url": "http://h/s", "stream": False}


def test_old_nested_dwc_settings_are_merged_machine_over_main():
    settings = {
        "main": {"webcam": {"enabled": True, "url": "http://main/s", "updateInterval": 5000}},
        "machine": {"webcam": {"enabled": True, "url": "http://machine/s", "updateInterval": 0}},
    }
    assert parse_dwc_settings(settings, "h", BASE) == {"url": "http://machine/s", "stream": True}
    only_main = {"main": settings["main"], "machine": {}}
    assert parse_dwc_settings(only_main, "h", BASE) == {"url": "http://main/s", "stream": False}


@pytest.mark.parametrize(
    "settings",
    [None, "", [], {}, {"webcam": None}, {"webcam": {"enabled": False, "url": "http://x"}},
     {"webcam": {"enabled": True, "url": "", "liveUrl": ""}}],
)
def test_dwc_settings_without_a_usable_webcam(settings):
    assert parse_dwc_settings(settings, "h", BASE) == {"url": None, "stream": False}


def test_first_jpeg_finds_a_frame_inside_a_stream_slice():
    stream = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + FRAME + b"\r\n--frame\r\n\xff\xd8partial"
    assert first_jpeg(stream) == FRAME
    assert first_jpeg(b"no image here") is None
    assert first_jpeg(b"\xff\xd8 started but not finished") is None


# --- camera -----------------------------------------------------------------------


def webcam_entity(hass, entry):
    return er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"webcam-{entry.entry_id}")


async def test_no_webcam_entity_when_no_address_is_known(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert webcam_entity(hass, entry) is None


async def test_the_address_in_the_options_is_used(hass, fake_duet):
    url = f"http://{fake_duet.host}:{fake_duet.port}/webcam/snapshot"
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, webcam_url=url))
    entity_id = webcam_entity(hass, entry)
    assert entity_id
    image = await async_get_image(hass, entity_id)
    assert image.content == FRAME


async def test_the_address_is_found_in_the_dwc_settings(hass, fake_duet):
    fake_duet.downloads["0:/sys/dwc-settings.json"] = {
        "webcam": {
            "enabled": True,
            "url": f"http://{{hostname}}:{fake_duet.port}/webcam/snapshot",
            "updateInterval": 0,
        }
    }
    entry = await setup_entry(hass, fake_duet)
    entity_id = webcam_entity(hass, entry)
    assert entity_id
    assert (await async_get_image(hass, entity_id)).content == FRAME
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert coordinator.webcam_live_url == f"http://{fake_duet.host}:{fake_duet.port}/webcam/snapshot"


async def test_the_option_wins_over_dwc(hass, fake_duet):
    fake_duet.downloads["0:/sys/dwc-settings.json"] = {"webcam": {"enabled": True, "url": "http://elsewhere/x"}}
    own = f"http://{fake_duet.host}:{fake_duet.port}/webcam/snapshot"
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, webcam_url=own))
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert coordinator.webcam_url == own
    assert coordinator.webcam_live_url == own


async def test_a_stream_address_gives_its_first_frame(hass, fake_duet):
    url = f"http://{fake_duet.host}:{fake_duet.port}/webcam/stream"
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, webcam_url=url))
    assert (await async_get_image(hass, webcam_entity(hass, entry))).content == FRAME


async def test_a_dead_webcam_gives_no_image_and_does_not_break_anything(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet, entry_for(fake_duet, webcam_url="http://127.0.0.1:1/none"))
    with pytest.raises(Exception):  # HA: "Unable to get image"
        await async_get_image(hass, webcam_entity(hass, entry))
    assert state_of(hass, entry, "Current State").state == "idle"


async def test_the_options_form_offers_the_webcam_address(hass, fake_duet):
    from unittest.mock import patch

    entry = await setup_entry(hass, fake_duet)
    with patch("custom_components.duet3d.async_setup_entry", return_value=True):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert "webcam_url" in {str(k) for k in flow["data_schema"].schema}
        await hass.config_entries.options.async_configure(
            flow["flow_id"],
            {"update_interval": 30, "printing_interval": 5, "webcam_url": "  http://cam/s  "},
        )
    assert entry.data["webcam_url"] == "http://cam/s"


# --- online sensor ------------------------------------------------------------------


async def test_online_sensor_follows_the_printer_and_never_goes_unavailable(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    assert state_of(hass, entry, "Online", "binary_sensor").state == "on"

    fake_duet.fail = True
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    # a missed poll or two is tolerated: the printer is not reported offline
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entry, "Online", "binary_sensor").state == "on"
    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entry, "Online", "binary_sensor").state == "off"
    assert state_of(hass, entry, "Current State").state == "unavailable"

    fake_duet.fail = False
    await refresh(hass, entry)
    assert state_of(hass, entry, "Online", "binary_sensor").state == "on"


# --- firmware update -------------------------------------------------------------------


async def test_the_firmware_entity_is_off_by_default_so_github_is_not_asked(hass, fake_duet):
    entry = await setup_entry(hass, fake_duet)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("update", DOMAIN, f"firmware-{entry.entry_id}")
    assert registry.async_get(entity_id).disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


async def _enable_firmware(hass, entry):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("update", DOMAIN, f"firmware-{entry.entry_id}")
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entity_id


async def test_firmware_update_available_when_a_newer_release_exists(hass, fake_duet, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/Duet3D/RepRapFirmware/releases/latest",
        json={"tag_name": "3.6.1", "html_url": "https://github.com/Duet3D/RepRapFirmware/releases/tag/3.6.1"},
    )
    entry = await setup_entry(hass, fake_duet)  # the fake board runs 3.5.4
    entity_id = await _enable_firmware(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["installed_version"] == "3.5.4"
    assert state.attributes["latest_version"] == "3.6.1"
    assert state.attributes["release_url"].endswith("/3.6.1")
    assert not state.attributes["supported_features"]  # informational: nothing to install


async def test_a_board_on_a_newer_firmware_than_the_stable_release_is_not_offered_a_downgrade(hass, fake_duet, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/Duet3D/RepRapFirmware/releases/latest", json={"tag_name": "3.5.0"}
    )
    entry = await setup_entry(hass, fake_duet)
    entity_id = await _enable_firmware(hass, entry)
    assert hass.states.get(entity_id).state == "off"


async def test_firmware_entity_survives_github_being_unreachable(hass, fake_duet, aioclient_mock):
    aioclient_mock.get("https://api.github.com/repos/Duet3D/RepRapFirmware/releases/latest", status=403)
    entry = await setup_entry(hass, fake_duet)
    entity_id = await _enable_firmware(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["installed_version"] == "3.5.4"
