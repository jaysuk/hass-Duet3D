"""LED strips: one light per strip the board has configured (``M950 E``), set with ``M150``.

The strips, their lengths and whether they have a white channel come from the object
model (``ledStrips[].maxLeds`` is the ``U`` of ``M950``, ``type`` is ``NeoPixel_RGBW`` for
RGBW), so nothing needs configuring here. The board does not report a strip's colour, so
the light shows what was last sent from Home Assistant.
"""
import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

from . import DuetDataUpdateCoordinator
from .controls import send
from .entity import DuetEntity, add_dynamic

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up a light for each LED strip the printer reports."""
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator"
    ]

    def strips():
        for key, strip in coordinator.hardware["led_strips"].items():
            yield key, lambda k=key, s=strip: Duet3DLight(
                coordinator, k, s["index"], s["label"], config_entry.entry_id
            )

    add_dynamic(coordinator, config_entry, async_add_entities, strips)


class Duet3DLight(DuetEntity, LightEntity):
    """One LED strip. An RGBW strip takes a white level as well as a colour."""

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        key: str,
        strip_index: int,
        label: str,
        entry_id: str,
    ) -> None:
        # Strip 0 keeps the unique id the single configured LED light always had.
        unique_id = f"LED-{entry_id}" if strip_index == 0 else f"LED-{strip_index}-{entry_id}"
        super().__init__(coordinator, label, unique_id)
        self._key = key
        self._strip_index = strip_index
        self._state = False
        self._brightness = 255
        self._last_brightness = self._brightness
        # red, green, blue, white. An RGBW strip starts on its white LED, which is
        # what "turn on" should mean for it; red + green + blue is only a poor white.
        self._color = (0, 0, 0, 255) if self._rgbw else (255, 255, 255, 0)

    def _strip(self) -> dict | None:
        return self.coordinator.hardware["led_strips"].get(self._key)

    @property
    def _rgbw(self) -> bool:
        strip = self._strip()
        return bool(strip and strip["rgbw"])

    @property
    def available(self) -> bool:
        return super().available and self._strip() is not None

    @property
    def supported_color_modes(self):
        return {ColorMode.RGBW if self._rgbw else ColorMode.RGB}

    @property
    def color_mode(self):
        return ColorMode.RGBW if self._rgbw else ColorMode.RGB

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
        return self._color[:3]

    @property
    def rgbw_color(self):
        """Return the RGBW color of the light."""
        return self._color

    def _m150(self, color, brightness) -> str:
        """``M150`` for the whole strip, as long as the board says it is (``M950 U``)."""
        strip = self._strip()
        count = strip["count"] if strip else None
        gcode = "M150 E{strip} R{r} U{g} B{b}".format(
            strip=self._strip_index, r=color[0], g=color[1], b=color[2]
        )
        if self._rgbw:
            gcode += f" W{color[3]}"
        return f"{gcode} P{brightness} S{count or 1}"

    async def async_turn_on(self, **kwargs):
        # The state changes only once the board has accepted the command.
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._last_brightness)
        color = self._color
        if ATTR_RGBW_COLOR in kwargs:
            color = tuple(kwargs[ATTR_RGBW_COLOR])
        elif ATTR_RGB_COLOR in kwargs:
            color = (*kwargs[ATTR_RGB_COLOR], 0)

        # M150 (RRF 3.5+, with the E parameter)
        await send(self.coordinator, self._m150(color, brightness), wait=True)
        self._state = True
        self._brightness = self._last_brightness = brightness
        self._color = color
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        await send(self.coordinator, self._m150((0, 0, 0, 0), 0), wait=True)
        self._state = False
        self._last_brightness = self._brightness or self._last_brightness
        self._brightness = 0
        self.async_write_ha_state()
