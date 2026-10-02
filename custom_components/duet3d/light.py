import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry

from . import DuetDataUpdateCoordinator
from .controls import send

from .const import CONF_NAME, DOMAIN, CONF_LIGHT, CONF_LED_STRIP_INDEX, CONF_LED_COUNT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the Duet3D light platform."""
    lightIncluded = config_entry.data[CONF_LIGHT]
    if lightIncluded:
        coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][
            config_entry.entry_id
        ]["coordinator"]
        device_id = config_entry.entry_id
        assert device_id is not None
        strip_index = config_entry.data.get(CONF_LED_STRIP_INDEX, 0)
        led_count = config_entry.data.get(CONF_LED_COUNT, 1)
        entities: list[LightEntity] = [
            Duet3DLight(coordinator, "LED", device_id, strip_index, led_count),
        ]
        async_add_entities(entities)


class Duet3DLightBase(CoordinatorEntity[DuetDataUpdateCoordinator], LightEntity):
    """Representation of a light connected to a Duet3D printer."""

    def __init__(
        self, coordinator: DuetDataUpdateCoordinator, light_name: str, device_id: str
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_name = f"{self.device_info['name']} {light_name}"
        self._attr_unique_id = device_id

    @property
    def device_info(self):
        """Device info."""
        return self.coordinator.device_info


class Duet3DLight(Duet3DLightBase):
    """Representation of a Duet3D LED light."""

    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        name: str,
        device_id: str,
        strip_index: int = 0,
        led_count: int = 1,
    ) -> None:
        super().__init__(coordinator, name, f"{name}-{device_id}")
        self._state = False
        self._brightness = 255
        self._rgb_color = (255, 255, 255)
        self._last_brightness = self._brightness
        self._strip_index = strip_index
        self._led_count = led_count
        self._attr_color_mode = ColorMode.RGB
        self._attr_supported_color_modes = {ColorMode.RGB}

    @property
    def name(self):
        """Return the name of the light."""
        return self._attr_name

    @property
    def is_on(self):
        """Return the state of the light."""
        return self._state

    @property
    def brightness(self):
        """Return the brightness of the light."""
        return self._brightness

    @property
    def rgb_color(self):
        """Return the RGB color of the light."""
        return self._rgb_color

    async def async_turn_on(self, **kwargs):
        # The state changes only once the board has accepted the command.
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._last_brightness)
        rgb_color = tuple(kwargs.get(ATTR_RGB_COLOR, self._rgb_color))

        # M150 (RRF 3.5+, with the E parameter)
        await send(
            self.coordinator,
            "M150 E{strip} R{r} U{g} B{b} P{p} S{s}".format(
                strip=self._strip_index,
                r=rgb_color[0],
                g=rgb_color[1],
                b=rgb_color[2],
                p=brightness,
                s=self._led_count,
            ),
            wait=True,
        )
        self._state = True
        self._brightness = self._last_brightness = brightness
        self._rgb_color = rgb_color
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        await send(
            self.coordinator,
            "M150 E{strip} R0 U0 B0 P0 S{s}".format(
                strip=self._strip_index, s=self._led_count
            ),
            wait=True,
        )
        self._state = False
        self._last_brightness = self._brightness or self._last_brightness
        self._brightness = 0
        self.async_write_ha_state()
