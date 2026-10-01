"""Support for monitoring Duet3D binary sensors."""
import logging
from functools import partial

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from . import DuetDataUpdateCoordinator
from .entity import add_dynamic
from .hardware import axes_homed, startup_error

from .const import DOMAIN, SENSOR_TYPES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the available Duet3D binary sensors."""
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator"
    ]
    device_id = config_entry.entry_id
    assert device_id is not None

    def axes(c):
        return axes_homed(c.get_sensor_state(SENSOR_TYPES["Axes"]["json_path"]))

    def error(c):
        return startup_error(c.get_sensor_state(SENSOR_TYPES["Machine State"]["json_path"]))

    entities: list[BinarySensorEntity] = [
        DuetPrintingSensor(coordinator, "Printing", device_id),
        DuetValueBinarySensor(
            coordinator, "Homed", f"Homed-{device_id}",
            lambda c: None if axes(c) is None else axes(c)["all"],
            attrs_fn=lambda c: None if axes(c) is None else dict(axes(c)["axes"]),
            icon="mdi:home-search",
        ),
        DuetValueBinarySensor(
            coordinator, "Startup error", f"Startup error-{device_id}",
            lambda c: error(c) is not None,
            attrs_fn=error,
            device_class=BinarySensorDeviceClass.PROBLEM,
            category=EntityCategory.DIAGNOSTIC,
        ),
    ]
    async_add_entities(entities)

    def monitors():
        # filamentPresent is only reported by monitors that can tell.
        for key, monitor in coordinator.hardware["monitors"].items():
            if monitor["present"] is not None:
                yield key, partial(_filament_present, coordinator, device_id, key, monitor["extruder"])

    def boards():
        # Only expansion boards report a state.
        for key, board in coordinator.hardware["boards"].items():
            if board["state"] is not None:
                yield key, partial(_board_connected, coordinator, device_id, key, board["label"])

    add_dynamic(coordinator, config_entry, async_add_entities, monitors)
    add_dynamic(coordinator, config_entry, async_add_entities, boards)


class DuetPrintSensorBase(
    CoordinatorEntity[DuetDataUpdateCoordinator], BinarySensorEntity
):
    """Representation of an Duet3D sensor."""

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet3D sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_name = f"{self.device_info['name']} {sensor_name}"
        self._attr_unique_id = device_id

    @property
    def device_info(self):
        """Device info."""
        return self.coordinator.device_info


class DuetPrintingSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""

    _attr_icon = "mdi:file-percent"

    def __init__(
        self, coordinator: DuetDataUpdateCoordinator, sensor_name: str, device_id: str
    ) -> None:
        """Initialize a new Duet3D sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"{sensor_name}-{device_id}",
        )

    @property
    def is_on(self):
        """Return sensor state."""
        current_state_json_path = SENSOR_TYPES["Current State"]["json_path"]
        current_state = self.coordinator.get_sensor_state(
            current_state_json_path, "Current State"
        )
        if current_state is not None:
            if current_state in {"processing", "simulating"}:
                return True
            else:
                return False
        else:
            _LOGGER.warning("Received no data from coordinator")


class DuetValueBinarySensor(DuetPrintSensorBase):
    """A binary sensor computed from the coordinator's data by a function.

    ``value_fn`` returns True, False or None (unknown). ``exists_fn`` makes a
    sensor for a discovered thing unavailable once the printer stops reporting it.
    """

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        name: str,
        unique_id: str,
        value_fn,
        *,
        attrs_fn=None,
        exists_fn=None,
        device_class=None,
        category=None,
        icon=None,
    ) -> None:
        super().__init__(coordinator, name, unique_id)
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn
        self._exists_fn = exists_fn
        self._attr_device_class = device_class
        self._attr_entity_category = category
        self._attr_icon = icon

    @property
    def is_on(self):
        return self._value_fn(self.coordinator)

    @property
    def extra_state_attributes(self):
        return self._attrs_fn(self.coordinator) if self._attrs_fn else None

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        return self._exists_fn is None or self._exists_fn(self.coordinator)


def _filament_present(coordinator, device_id, key, extruder) -> DuetValueBinarySensor:
    return DuetValueBinarySensor(
        coordinator, f"Extruder {extruder} filament present", f"{key}-present-{device_id}",
        lambda c: c.hardware["monitors"].get(key, {}).get("present"),
        exists_fn=lambda c: c.hardware["monitors"].get(key, {}).get("present") is not None,
        icon="mdi:printer-3d-nozzle",
    )


def _board_connected(coordinator, device_id, key, label) -> DuetValueBinarySensor:
    """An expansion board reports ``running`` when the main board can talk to it."""

    def connected(c):
        state = c.hardware["boards"].get(key, {}).get("state")
        return None if state is None else state == "running"

    return DuetValueBinarySensor(
        coordinator, f"{label} connected", f"{key}-connected-{device_id}", connected,
        attrs_fn=lambda c: {"state": c.hardware["boards"].get(key, {}).get("state")},
        exists_fn=lambda c: key in c.hardware["boards"],
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        category=EntityCategory.DIAGNOSTIC,
    )
