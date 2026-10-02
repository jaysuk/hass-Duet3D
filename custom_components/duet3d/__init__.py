"""Support for monitoring Duet 3D printers."""
import logging
import voluptuous as vol
import aiohttp
import asyncio
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import slugify as util_slugify
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import homeassistant.util.dt as dt_util
from typing import cast
from yarl import URL

from datetime import timedelta


from .events import JobTracker
from .hardware import build_hardware
from .webcam import DWC_SETTINGS_PATH, parse_dwc_settings
from .model import build_heater_roles, resolve, set_path
from .services import async_register_services, async_unregister_services

from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SSL,
    Platform,
    CONF_PASSWORD
)
from .const import (
    CONF_SBC_STATUS_PATH,
    CONF_SBC_API,
    CONF_SBC_GCODE_PATH,
    CONF_STANDALONE_API,
    CONF_STANDALONE_GCODE_PATH,
    CONF_STANDALONE,
    DOMAIN,
    CONF_INTERVAL,
    CONF_PRINTING_INTERVAL,
    CONF_WEBCAM_URL,
    DEFAULT_PRINTING_INTERVAL,
    EVENT_NAME,
    PRINTING_STATES,
    STANDALONE_POLL_FLAGS,
    STANDALONE_POLL_KEYS,
    STANDALONE_SLOW_POLL_KEYS,
    STANDALONE_SLOW_POLL_SECONDS,
    MACRO_DIRECTORY,
    TOLERATED_FAILED_POLLS,
    CONF_JSON_HEADER,
    CONF_TEXT_PLAIN_HEADER,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.UPDATE,
]


def has_all_unique_names(value):
    """Validate that printers have an unique name."""
    names = [util_slugify(printer["name"]) for printer in value]
    vol.Schema(vol.Unique())(names)
    return value


def ensure_valid_path(value):
    """Validate the path, ensuring it starts and ends with a /."""
    vol.Schema(cv.string)(value)
    if value[0] != "/":
        value = "/" + value
    if value[-1] != "/":
        value += "/"
    return value


async def options_update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_setup(hass, config):
    """Legacy way to set up Duet3D component from YAML."""
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Set up Duet3D component from a config entry."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    coordinator = DuetDataUpdateCoordinator(
        hass, config_entry, config_entry.data[CONF_INTERVAL]
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except (ConfigEntryNotReady, ConfigEntryAuthFailed):
        await coordinator.async_close_session()
        raise

    # Extract firmware and board info from the first successful data fetch
    try:
        if config_entry.data[CONF_STANDALONE]:
            _LOGGER.info("Using standalone mode")
            # boards[0] is the main board; expansion boards follow it.
            coordinator.firmware_version = coordinator.get_sensor_state(
                "status.boards[0].firmwareVersion"
            )
            coordinator.board_model = coordinator.get_sensor_state("status.boards[0].name")
        else:
            status = coordinator.data.get("status")
            if status:
                coordinator.firmware_version = coordinator.get_value_from_json(
                    status, "boards", "software", "firmwareVersion", None
                )
                coordinator.board_model = coordinator.get_value_from_json(
                    status, "boards", "software", "model", None
                )
    except (KeyError, TypeError):
        _LOGGER.error("Failed to extract firmware/board data")

    await coordinator.async_detect_webcam()

    hass.data[DOMAIN][config_entry.entry_id] = {"coordinator": coordinator}

    async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    config_entry.async_on_unload(config_entry.add_update_listener(update_listener))
    return True


async def update_listener(hass, entry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await coordinator.async_close_session()
        del hass.data[DOMAIN][entry.entry_id]
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
    return unload_ok


class DuetDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, interval: int
    ) -> None:
        """Initialize Duet3D API and set headers needed later."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"duet3d-{config_entry.entry_id}",
            update_interval=timedelta(seconds=interval),
        )
        self.data = {"status": None, "last_read_time": None}
        # Seconds between polls when idle, and while a job runs. A job is polled at
        # least as often as an idle printer, never less.
        self.interval = interval
        self.printing_interval = min(
            interval,
            config_entry.data.get(CONF_PRINTING_INTERVAL, DEFAULT_PRINTING_INTERVAL),
        )
        self.tracker = JobTracker()
        # When the current or last job started and ended, as seen by this integration
        # (so unknown after a restart of Home Assistant until the next job).
        self.job_started_at = None
        self.job_ended_at = None
        # Webcam addresses found in Duet Web Control's settings (see detect_webcam).
        self.detected_webcam: dict[str, str | None] = {"url": None, "live_url": None}
        self._pending_events: list[dict] = []
        self.config_entry = config_entry
        self.printer_online = False
        self.status_error_logged = False
        # Heaters in use, found from the object model on every poll. See model.py.
        self.heater_roles: dict[str, dict] = {}
        # Fans, boards, storage, network and filament monitors, rebuilt every poll.
        self.hardware: dict[str, dict[str, dict]] = build_hardware(None)
        self._slow_status: dict = {}
        self._slow_fetched_at: float | None = None
        # File names of the macros in the top level of 0:/macros, refreshed with the slow poll.
        self.macros: list[str] = []
        self._macros_fetched_at: float | None = None
        self._failed_polls = 0
        self.base_url = "http{0}://{1}:{2}".format(
            "s" if self.config_entry.data[CONF_SSL] else "",
            config_entry.data[CONF_HOST],
            config_entry.data[CONF_PORT],
        )

        if self.config_entry.data[CONF_STANDALONE]:
            self.status_api_url = self.base_url + CONF_STANDALONE_API
        else:
            self.status_api_url = "{0}{1}{2}".format(
                self.base_url,
                CONF_SBC_API,
                CONF_SBC_STATUS_PATH,
            )

        self._password = config_entry.data.get(CONF_PASSWORD, "")
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False
        self.firmware_version = None
        self.board_model = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a persistent aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._authenticated = False
        return self._session

    async def _ensure_authenticated(self):
        """Authenticate with the printer if using standalone mode with a password."""
        if not self.config_entry.data[CONF_STANDALONE]:
            return
        if not self._password:
            return
        if self._authenticated:
            return

        session = await self._get_session()
        rejected = False
        try:
            async with asyncio.timeout(10):
                async with session.get(
                    f"{self.base_url}/rr_connect",
                    params={"password": self._password},
                    headers=CONF_JSON_HEADER,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    if data.get("err", 1) == 0:
                        self._authenticated = True
                        _LOGGER.debug("Authenticated with printer")
                    elif data.get("err") == 1:
                        # err 1 is "wrong password". (Other errors, such as err 2, no
                        # free session, are the board's trouble and may pass.)
                        rejected = True
                    else:
                        _LOGGER.error(
                            "Authentication failed (err=%s)", data.get("err")
                        )
        except Exception as exc:
            _LOGGER.error("Could not authenticate with printer: %s", exc)
            self._authenticated = False
        if rejected:
            self._authenticated = False
            raise ConfigEntryAuthFailed("The board rejected the password")

    async def async_close_session(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._authenticated = False

    async def get_status(self, key=None, flags=None):
        """Send a get request, and return the response as a dict."""
        if self.config_entry.data[CONF_STANDALONE]:
            url = f"{self.status_api_url}?key={key}"
            if flags:
                url += f"&flags={flags}"
        else:
            url = self.status_api_url
        _LOGGER.debug("URL: %s", url)

        await self._ensure_authenticated()
        session = await self._get_session()

        try:
            async with asyncio.timeout(10):
                async with session.get(url, headers=CONF_JSON_HEADER) as response:
                    if response.status == 401:
                        # Session expired, re-authenticate and retry once
                        _LOGGER.debug("Session expired, re-authenticating")
                        self._authenticated = False
                        await self._ensure_authenticated()
                        async with session.get(
                            url, headers=CONF_JSON_HEADER
                        ) as retry_resp:
                            retry_resp.raise_for_status()
                            data = await retry_resp.json()
                    else:
                        response.raise_for_status()
                        data = await response.json()

                    self.printer_online = True
                    self.status_error_logged = False
                    return data
        except aiohttp.ClientConnectorError as conn_exc:
            if not self.status_error_logged:
                _LOGGER.error("Failed to connect to Duet3D board: %s", conn_exc)
                self.status_error_logged = True
            self.printer_online = False
            raise UpdateFailed(conn_exc) from conn_exc
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            # A busy or briefly unreachable board: HTTP errors, dropped connections,
            # timeouts, a body that is not JSON.
            self.printer_online = False
            raise UpdateFailed(exc) from exc

    async def send_gcode(self, gcode: str) -> str | None:
        """Send G-code to the printer (used by service handler)."""
        await self._ensure_authenticated()
        session = await self._get_session()

        if self.config_entry.data[CONF_STANDALONE]:
            url = f"{self.base_url}{CONF_STANDALONE_GCODE_PATH}"
            params = {"gcode": gcode}
            async with asyncio.timeout(10):
                async with session.get(
                    url, params=params, headers=CONF_TEXT_PLAIN_HEADER
                ) as response:
                    if response.status == 401:
                        self._authenticated = False
                        await self._ensure_authenticated()
                        async with session.get(
                            url, params=params, headers=CONF_TEXT_PLAIN_HEADER
                        ) as retry_resp:
                            retry_resp.raise_for_status()
                            return await retry_resp.text()
                    response.raise_for_status()
                    return await response.text()
        else:
            url = f"{self.base_url}{CONF_SBC_API}{CONF_SBC_GCODE_PATH}"
            async with asyncio.timeout(10):
                async with session.post(
                    url, data=gcode, headers=CONF_TEXT_PLAIN_HEADER
                ) as response:
                    response.raise_for_status()
                    return await response.text()

    async def _async_update_data(self):
        """Update printer data via API.

        A board on Wi-Fi regularly misses a request. One failed poll must not make
        every entity unavailable and then available again, because each of those
        recoveries is a state change for every entity (a wall of Activity entries).
        So the last good data is kept for ``TOLERATED_FAILED_POLLS`` failures in a
        row, and only a printer that stays unreachable is reported as such.
        """
        try:
            data = await self._poll()
        except UpdateFailed:
            self._failed_polls += 1
            if (
                self.data
                and self.data.get("status") is not None
                and self._failed_polls <= TOLERATED_FAILED_POLLS
            ):
                _LOGGER.debug(
                    "Poll failed (%s in a row), keeping the last data", self._failed_polls
                )
                return self.data
            raise
        self._failed_polls = 0
        if data is not None:
            self._adapt_interval(data["status"])
            try:
                events = self.tracker.update(data["status"])
                self._pending_events.extend(events)
                self._track_job_times(events, data)
            except Exception:  # pylint: disable=broad-except
                # Events are a convenience: never let them cost the readings.
                _LOGGER.exception("Could not work out job events from the last poll")
        return data

    def _track_job_times(self, events: list[dict], data: dict) -> None:
        now = data["last_read_time"]
        for event in events:
            if event["type"] == "job_started":
                self.job_started_at, self.job_ended_at = now, None
            elif event["type"] in ("job_finished", "job_cancelled", "job_failed"):
                self.job_ended_at = now
        if self.tracker.in_job and self.job_started_at is None:
            # A job already running when we started watching: work back from its duration.
            duration = resolve(data, "status.job.duration")
            elapsed = duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0
            self.job_started_at = now - timedelta(seconds=elapsed)

    def _adapt_interval(self, status) -> None:
        """Poll faster while a job runs: that is when the readings matter."""
        state = resolve({"status": status}, "status.state.status")
        seconds = self.printing_interval if state in PRINTING_STATES else self.interval
        interval = timedelta(seconds=seconds)
        if self.update_interval != interval:
            _LOGGER.debug("Polling every %s s (printer is %s)", seconds, state)
            self.update_interval = interval

    @callback
    def async_update_listeners(self) -> None:
        """Tell the entities first, then announce the events.

        An automation that reacts to ``job_finished`` reads entity states, so they
        must already show the poll that produced the event.
        """
        super().async_update_listeners()
        self._fire_events()

    @callback
    def _fire_events(self) -> None:
        events, self._pending_events = self._pending_events, []
        if not events:
            return
        registry = dr.async_get(self.hass)
        identifier = (DOMAIN, cast(str, self.config_entry.unique_id))
        if hasattr(registry, "async_get_device_by_identifier"):
            device = registry.async_get_device_by_identifier(
                identifier, self.config_entry.entry_id
            )
        else:  # Home Assistant before the lookup was scoped to a config entry
            device = registry.async_get_device(identifiers={identifier})
        for event in events:
            self.hass.bus.async_fire(
                EVENT_NAME,
                {
                    "device_id": device.id if device else None,
                    "name": self.config_entry.data[CONF_NAME],
                    **event,
                },
            )

    async def _poll(self):
        """Read the object model once."""
        if self.config_entry.data[CONF_STANDALONE]:
            # One request per object model key rather than one per sensor.
            status = {}
            await self._fetch_keys(STANDALONE_POLL_KEYS, status)
            now = self.hass.loop.time()
            if (
                self._slow_fetched_at is None
                or now - self._slow_fetched_at >= STANDALONE_SLOW_POLL_SECONDS
            ):
                self._slow_status = {}
                await self._fetch_keys(STANDALONE_SLOW_POLL_KEYS, self._slow_status)
                self._slow_fetched_at = now
            status.update(self._slow_status)
        else:
            status = await self.get_status()
        if status is None:
            return None
        await self._refresh_macros()
        self.heater_roles = build_heater_roles(
            resolve(status, "heat"), resolve(status, "tools")
        )
        self.hardware = build_hardware(status)
        return {"status": status, "last_read_time": dt_util.utcnow()}

    async def fetch_json(self, url: str, params: dict | None = None):
        """GET ``url`` and decode the JSON, logging in again once if the session expired."""
        await self._ensure_authenticated()
        session = await self._get_session()
        async with asyncio.timeout(10):
            async with session.get(url, params=params, headers=CONF_JSON_HEADER) as response:
                if response.status == 401:
                    self._authenticated = False
                    await self._ensure_authenticated()
                    async with session.get(
                        url, params=params, headers=CONF_JSON_HEADER
                    ) as retry:
                        retry.raise_for_status()
                        return await retry.json(content_type=None)
                response.raise_for_status()
                return await response.json(content_type=None)

    @property
    def webcam_url(self) -> str | None:
        """The webcam to show: the one set in the options, else the one DWC has."""
        return self.config_entry.data.get(CONF_WEBCAM_URL) or self.detected_webcam["url"]

    @property
    def webcam_live_url(self) -> str | None:
        """The address to stream from: DWC's live address if it has one."""
        if self.config_entry.data.get(CONF_WEBCAM_URL):
            return self.config_entry.data[CONF_WEBCAM_URL]
        return self.detected_webcam["live_url"] or self.detected_webcam["url"]

    async def async_detect_webcam(self) -> None:
        """Read the webcam address from DWC's settings file on the board, if there is one."""
        try:
            if self.config_entry.data[CONF_STANDALONE]:
                settings = await self.fetch_json(
                    f"{self.base_url}/rr_download", {"name": DWC_SETTINGS_PATH}
                )
            else:
                settings = await self.fetch_json(
                    f"{self.base_url}{CONF_SBC_API}/file/{DWC_SETTINGS_PATH}"
                )
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("No webcam settings read from the board: %s", exc)
            return
        self.detected_webcam = parse_dwc_settings(
            settings, self.config_entry.data[CONF_HOST], self.base_url
        )

    async def _refresh_macros(self) -> None:
        """Re-read the macro directory, at most as often as the slow poll.

        Macros are a convenience: a missing directory or a failed request keeps the
        last list and never fails the poll.
        """
        now = self.hass.loop.time()
        if (
            self._macros_fetched_at is not None
            and now - self._macros_fetched_at < STANDALONE_SLOW_POLL_SECONDS
        ):
            return
        self._macros_fetched_at = now
        try:
            self.macros = await self._list_macros()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not list %s: %s", MACRO_DIRECTORY, exc)

    async def _list_macros(self) -> list[str]:
        if not self.config_entry.data[CONF_STANDALONE]:
            listing = await self.fetch_json(
                f"{self.base_url}{CONF_SBC_API}/directory/{MACRO_DIRECTORY}"
            )
            entries = listing if isinstance(listing, list) else []
            return sorted(
                e["name"] for e in entries
                if isinstance(e, dict) and e.get("type") == "f" and isinstance(e.get("name"), str)
            )
        names: list[str] = []
        first = 0
        for _ in range(100):  # a page holds a few dozen entries; never loop forever
            page = await self.fetch_json(
                f"{self.base_url}/rr_filelist", {"dir": MACRO_DIRECTORY, "first": first}
            )
            if not isinstance(page, dict) or page.get("err"):
                break
            names.extend(
                e["name"] for e in page.get("files") or []
                if isinstance(e, dict) and e.get("type") == "f" and isinstance(e.get("name"), str)
            )
            first = page.get("next") or 0
            if not first:
                break
        return sorted(names)

    async def _fetch_keys(self, keys, status: dict) -> None:
        """Fetch each rr_model key into ``status`` at the same place DSF would have it."""
        for key in keys:
            response = await self.get_status(key, STANDALONE_POLL_FLAGS)
            set_path(status, key, response.get("result") if isinstance(response, dict) else None)

    def get_sensor_state(self, json_path=None, sensor_name=None):
        """Value at ``json_path`` (``status.…``) in either mode, or None if absent.

        ``sensor_name`` is unused and kept so existing callers keep working.
        """
        return resolve(self.data, json_path) if json_path else None

    @property
    def device_info(self) -> DeviceInfo:
        """Device info."""
        unique_id = cast(str, self.config_entry.unique_id)
        configuration_url = URL.build(
            scheme=self.config_entry.data[CONF_SSL] and "https" or "http",
            host=self.config_entry.data[CONF_HOST],
            port=self.config_entry.data[CONF_PORT],
        )

        return DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            manufacturer="Duet3D",
            name=self.config_entry.data[CONF_NAME],
            model=self.board_model,
            sw_version=self.firmware_version,
            configuration_url=str(configuration_url),
        )

    def get_value_from_json(self, json_dict, end_point, sensor_type, group, tool):
        """Return the value for sensor_type from the JSON."""
        if end_point == "boards":
            if group == "firmwareVersion":
                return json_dict[end_point][0]["firmwareVersion"]
            if group == "model":
                return json_dict[end_point][0]["shortName"]
