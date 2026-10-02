"""Config flow for Duet3D Printer integration."""
from homeassistant import config_entries
import logging
from homeassistant.core import callback
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from typing import Any
from homeassistant.helpers.typing import UNDEFINED

from .const import (
    DOMAIN,
    CONF_NAME,
    DEFAULT_NAME,
    CONF_SBC_GCODE_PATH,
    CONF_SBC_STATUS_PATH,
    CONF_BASE_URL,
    CONF_LIGHT,
    CONF_LED_STRIP_INDEX,
    CONF_LED_COUNT,
    CONF_INTERVAL,
    CONF_STANDALONE,
)
from .detect import CannotConnect, InvalidAuth, NotADuet, detect_standalone

_LOGGER = logging.getLogger(__name__)


def _schema_with_defaults(
    name=DEFAULT_NAME,
    ssl=False,
    host="192.168.2.116",
    port=80,
    password="",
    update_interval=30,
):
    # Standalone or SBC mode is detected from the board, and the LED strip is set up in
    # the options once there is one. Do not wrap fields in a nested vol.Schema: HA
    # cannot serialise it and the form then fails to load with a 500.
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=name): str,
            vol.Required(CONF_SSL, default=ssl): bool,
            vol.Required(CONF_HOST, default=host): str,
            vol.Optional(CONF_PASSWORD, default=password): str,
            vol.Required(CONF_PORT, default=port): cv.port,
            vol.Required(CONF_INTERVAL, default=update_interval): int,
        },
    )


class Duet3dConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Duet3DPrinter."""

    VERSION = 1
    _LOGGER.debug("Entering config flow")

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            # Check if host is already configured
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()

            connection_url = "http{0}://{1}:{2}".format(
                "s" if user_input[CONF_SSL] else "",
                user_input[CONF_HOST],
                user_input[CONF_PORT],
            )

            standalone = None
            try:
                standalone = await detect_standalone(
                    connection_url, user_input.get(CONF_PASSWORD, "")
                )
            except InvalidAuth:
                errors[CONF_PASSWORD] = "invalid_auth"
            except NotADuet:
                errors[CONF_HOST] = "not_a_duet"
            except CannotConnect:
                errors[CONF_HOST] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors[CONF_HOST] = "unknown"

            if not errors:
                _LOGGER.info(
                    "%s is a %s Duet", user_input[CONF_HOST], "standalone" if standalone else "SBC"
                )
                return self.async_create_entry(
                    title=f"{user_input[CONF_NAME]} ({user_input[CONF_HOST]})",
                    data={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_PASSWORD: user_input.get(CONF_PASSWORD, ""),
                        CONF_SSL: user_input[CONF_SSL],
                        CONF_INTERVAL: user_input[CONF_INTERVAL],
                        CONF_LIGHT: user_input.get(CONF_LIGHT, False),
                        CONF_LED_STRIP_INDEX: user_input.get(CONF_LED_STRIP_INDEX, 0),
                        CONF_LED_COUNT: user_input.get(CONF_LED_COUNT, 1),
                        CONF_STANDALONE: standalone,
                        CONF_BASE_URL: connection_url,
                        CONF_SBC_STATUS_PATH: CONF_SBC_STATUS_PATH,
                        CONF_SBC_GCODE_PATH: CONF_SBC_GCODE_PATH,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema_with_defaults(),
            errors=errors,
        )

    async def async_step_import(self, user_input):
        """Handle import."""
        return await self.async_step_user(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return Duet3dOptionsFlow()


class Duet3dOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Duet3D Printer integration.

    ``self.config_entry`` is provided by Home Assistant; assigning it is an error.
    The LED strip questions are only asked when the printer has an LED strip.
    """

    title: str | None = None

    def __init__(self) -> None:
        self.new_entry_data: dict[str, Any] = {}

    @callback
    def finish_flow(self) -> FlowResult:
        """Update the ConfigEntry and finish the flow."""
        new_data = self.config_entry.data | self.new_entry_data
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data=new_data,
            title=self.title or UNDEFINED,
        )
        return self.async_create_entry(title="", data={})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        config_data = self.config_entry.data
        config_options = self.config_entry.options
        if user_input is not None:
            self.new_entry_data = {
                CONF_INTERVAL: user_input[CONF_INTERVAL],
                CONF_LIGHT: user_input[CONF_LIGHT],
            }
            if user_input[CONF_LIGHT]:
                return await self.async_step_led()
            return self.finish_flow()
        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_INTERVAL,
                    default=config_options.get(
                        CONF_INTERVAL, config_data.get(CONF_INTERVAL)
                    ),
                ): cv.positive_int,
                vol.Optional(
                    CONF_LIGHT,
                    default=config_data.get(CONF_LIGHT, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=options_schema)

    async def async_step_led(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Which LED strip, asked only after "LED's installed" is ticked."""
        if user_input is not None:
            self.new_entry_data[CONF_LED_STRIP_INDEX] = user_input[CONF_LED_STRIP_INDEX]
            self.new_entry_data[CONF_LED_COUNT] = user_input[CONF_LED_COUNT]
            return self.finish_flow()
        config_data = self.config_entry.data
        return self.async_show_form(
            step_id="led",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LED_STRIP_INDEX,
                        default=config_data.get(CONF_LED_STRIP_INDEX, 0),
                    ): int,
                    vol.Optional(
                        CONF_LED_COUNT,
                        default=config_data.get(CONF_LED_COUNT, 1),
                    ): int,
                }
            ),
        )
