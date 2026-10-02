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
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.exceptions import ConfigEntryNotReady
import homeassistant.util.dt as dt_util
from typing import cast
from yarl import URL

from datetime import timedelta


from .hardware import build_hardware
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
    STANDALONE_POLL_FLAGS,
    STANDALONE_POLL_KEYS,
    STANDALONE_SLOW_POLL_KEYS,
    STANDALONE_SLOW_POLL_SECONDS,
    TOLERATED_FAILED_POLLS,
    CONF_JSON_HEADER,
    CONF_TEXT_PLAIN_HEADER,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.LIGHT, Platform.CAMERA]


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
    except ConfigEntryNotReady:
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
        self.interval = interval
        self.config_entry = config_entry
        self.printer_online = False
        self.status_error_logged = False
        # Heaters in use, found from the object model on every poll. See model.py.
        self.heater_roles: dict[str, dict] = {}
        # Fans, boards, storage, network and filament monitors, rebuilt every poll.
        self.hardware: dict[str, dict[str, dict]] = build_hardware(None)
        self._slow_status: dict = {}
        self._slow_fetched_at: float | None = None
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
        connect_url = f"{self.base_url}/rr_connect?password={self._password}"
        try:
            async with asyncio.timeout(10):
                async with session.get(connect_url, headers=CONF_JSON_HEADER) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    if data.get("err", 1) == 0:
                        self._authenticated = True
                        _LOGGER.debug("Authenticated with printer")
                    else:
                        _LOGGER.error(
                            "Authentication failed (err=%s)", data.get("err")
                        )
        except Exception as exc:
            _LOGGER.error("Could not authenticate with printer: %s", exc)
            self._authenticated = False

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
        return data

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
        self.heater_roles = build_heater_roles(
            resolve(status, "heat"), resolve(status, "tools")
        )
        self.hardware = build_hardware(status)
        return {"status": status, "last_read_time": dt_util.utcnow()}

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
