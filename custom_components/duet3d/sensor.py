"""Support for monitoring Duet3D sensors."""
import logging
import os
import re
from functools import partial
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfTemperature,
)
from . import DuetDataUpdateCoordinator
from .entity import add_dynamic
from .extruders import build_extruders, slicer_filament
from .hardware import message_box
from .jobinfo import build_objects, eta, progress_percent, projected_total_minutes, slicer_total
from .model import as_number, heater_power, heater_state, heater_value

_LOGGER = logging.getLogger(__name__)

from .const import (
    DOMAIN,
    LEGACY_TEMPERATURE_UNIQUE_ID,
    SENSOR_TYPES,
    PRINTER_STATUS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the available Duet3D sensors."""
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator"
    ]

    device_id = config_entry.entry_id
    assert device_id is not None

    _remove_legacy_temperature_sensors(hass, config_entry)

    def add(discover):
        add_dynamic(coordinator, config_entry, async_add_entities, discover)

    # Everything below is discovered from the object model on each poll, so a tool,
    # fan, board or filament monitor that appears later gets its sensors without a
    # reload.
    def heater_temperatures():
        for key, role in coordinator.heater_roles.items():
            for sensor_type in role["types"]:
                yield (key, sensor_type), partial(
                    DuetTemperatureSensor, coordinator, key, role["label"], sensor_type, device_id
                )

    def heater_extras():
        for key, role in coordinator.heater_roles.items():
            yield key, partial(_heater_sensors, coordinator, device_id, key, role["label"])

    def extruders():
        for extruder in _current_extruders(coordinator):
            index = extruder["extruder"]
            yield index, partial(
                DuetExtruderSensor, coordinator, f"Extruder {index}", index, device_id
            )

    def extruder_flow():
        for extruder in _current_extruders(coordinator):
            index = extruder["extruder"]
            yield index, partial(_flow_sensor, coordinator, device_id, index)

    def fans():
        for key, fan in coordinator.hardware["fans"].items():
            yield key, partial(_fan_sensor, coordinator, device_id, key, fan["label"])

    def boards():
        for key, board in coordinator.hardware["boards"].items():
            for field, suffix, options in BOARD_METRICS:
                # Not every board reports every value (v12 is null on most).
                if board[field] is not None:
                    yield (key, field), partial(
                        _board_sensor, coordinator, device_id, key, board["label"], field, suffix, options
                    )

    def interfaces():
        for key, interface in coordinator.hardware["interfaces"].items():
            if interface["ip"] is not None:
                yield (key, "ip"), partial(_ip_sensor, coordinator, device_id, key, interface["label"])
            # Only a connected Wi-Fi interface reports a signal.
            if interface["signal"] is not None:
                yield (key, "signal"), partial(_signal_sensor, coordinator, device_id, key, interface["label"])

    def volumes():
        for key, volume in coordinator.hardware["volumes"].items():
            yield key, partial(_storage_sensor, coordinator, device_id, key, volume["label"])

    def monitors():
        for key, monitor in coordinator.hardware["monitors"].items():
            yield key, partial(_monitor_sensor, coordinator, device_id, key, monitor["extruder"])

    for discover in (
        heater_temperatures,
        heater_extras,
        extruders,
        extruder_flow,
        fans,
        boards,
        interfaces,
        volumes,
        monitors,
    ):
        add(discover)

    entities: list[SensorEntity] = [
        DuetPrintJobPercentageSensor(coordinator, "Progress", device_id),
        DuetTimeRemainingSensor(coordinator, "Time Remaining", device_id),
        DuetSlicerTimeRemainingSensor(coordinator, "Slicer Time Remaining", device_id),
        DuetPrintDurationSensor(coordinator, "Time Elapsed", device_id),
        DuetPrintPositionSensor(coordinator, "Position (X,Y,Z)", device_id),
        DuetCurrentStateSensor(coordinator, "Current State", device_id),
        DuetCurrentLayerSensor(coordinator, "Current Layer", device_id),
        DuetTotalLayersSensor(coordinator, "Total Layers", device_id),
        DuetFileNameSensor(coordinator, "File Name", device_id),
        DuetFilamentExtrudedSensor(coordinator, "Filament Extruded", device_id),
        DuetCurrentToolSensor(coordinator, "Current Tool", device_id),
        *_static_sensors(coordinator, device_id),
        *_job_sensors(coordinator, device_id),
    ]
    async_add_entities(entities)


def _current_extruders(coordinator: DuetDataUpdateCoordinator) -> list[dict]:
    """Extruder descriptions from the latest coordinator data."""
    if not coordinator.data or not coordinator.data.get("status"):
        return []
    return build_extruders(
        coordinator.get_sensor_state(
            SENSOR_TYPES["Extruders"]["json_path"], "Extruders"
        ),
        coordinator.get_sensor_state(SENSOR_TYPES["Tools"]["json_path"], "Tools"),
        coordinator.get_sensor_state(
            SENSOR_TYPES["Current Tool"]["json_path"], "Current Tool"
        ),
    )


class DuetPrintSensorBase(CoordinatorEntity[DuetDataUpdateCoordinator], SensorEntity):
    """Representation of an Duet sensor."""

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
        self.sensor_name = sensor_name

    @property
    def device_info(self):
        """Device info."""
        return self.coordinator.device_info


@callback
def _remove_legacy_temperature_sensors(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Delete temperature sensors from versions that numbered tools from the config.

    They were keyed by tool number and read ``heaters[<number>]``, so they were
    labelled with the wrong tool whenever heater and tool numbers differed. Their
    replacements have different unique ids, so the old ones would otherwise linger
    as unavailable entities. The bed sensors keep their unique ids and are kept.
    """
    registry = er.async_get(hass)
    pattern = re.compile(LEGACY_TEMPERATURE_UNIQUE_ID + re.escape(config_entry.entry_id) + "$")
    for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        if entry.domain == "sensor" and pattern.match(entry.unique_id):
            _LOGGER.info("Removing legacy temperature sensor %s", entry.entity_id)
            registry.async_remove(entry.entity_id)


class DuetTemperatureSensor(DuetPrintSensorBase):
    """Temperature of one heater, named after what it heats (tool, bed or chamber)."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        key: str,
        label: str,
        sensor_type: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet sensor.

        ``key`` identifies the role in ``coordinator.heater_roles`` and is what the
        unique id is built from, so it must not depend on heater numbering.
        """
        super().__init__(
            coordinator,
            f"{label} {sensor_type} temperature",
            f"{key}-{sensor_type}-{device_id}",
        )
        self._key = key
        self._sensor_type = sensor_type

    @property
    def native_value(self):
        """Return the temperature, or None if the heater is gone or its sensor is faulted."""
        role = self.coordinator.heater_roles.get(self._key)
        if role is None:
            return None
        return heater_value(
            self.coordinator.get_sensor_state(SENSOR_TYPES["Heat"]["json_path"]),
            self.coordinator.get_sensor_state(SENSOR_TYPES["Tools"]["json_path"]),
            role,
            self._sensor_type,
        )

    @property
    def extra_state_attributes(self):
        """Which heater and tool this reading belongs to."""
        role = self.coordinator.heater_roles.get(self._key)
        if role is None:
            return None
        attributes = {"heater": role["heater"]}
        if role["kind"] == "tool":
            attributes["tool"] = role["tool"]
        return attributes

    @property
    def available(self) -> bool:
        """Unavailable once the object model no longer has this heater."""
        return self.coordinator.last_update_success and self._key in self.coordinator.heater_roles


class DuetPrintJobPercentageSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""

    _attr_native_unit_of_measurement = PERCENTAGE
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
    def native_value(self):
        """Return sensor state."""
        return progress_percent(
            self.coordinator.get_sensor_state(SENSOR_TYPES["Filament Extrusion"]["json_path"]),
            self.coordinator.get_sensor_state(SENSOR_TYPES["Progress"]["json_path"]),
        )


class DuetTimeRemainingSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""

    _attr_native_unit_of_measurement = "min"
    # _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-end"

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
    def native_value(self):
        """Return sensor state."""
        time_remaining_json_path = SENSOR_TYPES["Time Remaining"]["json_path"]
        print_file_time_left = self.coordinator.get_sensor_state(
            time_remaining_json_path, self.sensor_name
        )
        if print_file_time_left is not None:
            return round(print_file_time_left / 60.0, 2)
        else:
            return 0


class DuetSlicerTimeRemainingSensor(DuetPrintSensorBase):
    """Representation of slicer-estimated time remaining sensor."""

    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-end"

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
    def native_value(self):
        """Return sensor state."""
        slicer_time_json_path = SENSOR_TYPES["Slicer Time Remaining"]["json_path"]
        slicer_time_left = self.coordinator.get_sensor_state(
            slicer_time_json_path, self.sensor_name
        )
        if slicer_time_left is not None:
            return round(slicer_time_left / 60.0, 2)
        else:
            return 0


class DuetPrintDurationSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""

    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT

    _attr_icon = "mdi:clock-start"

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
    def native_value(self):
        """Return sensor state."""
        job_duration_json_path = SENSOR_TYPES[self.sensor_name]["json_path"]
        jobDuration = self.coordinator.get_sensor_state(
            job_duration_json_path, self.sensor_name
        )
        if jobDuration is not None:
            return round(jobDuration / 60.0, 2)
        else:
            return 0


class DuetPrintPositionSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""

    _attr_icon = "mdi:axis-x-arrow"

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet3D sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"{sensor_name}-{device_id}",
        )

    @property
    def native_value(self):
        """Return sensor state."""
        position_json_path = SENSOR_TYPES["Position"]["json_path"]
        axis_json = self.coordinator.get_sensor_state(position_json_path, "Position")
        if axis_json is not None:
            positions = [
                axis_json[i]["machinePosition"]
                for i in range(len(axis_json))
                if axis_json[i]["letter"] in SENSOR_TYPES["Position"]["axes"]
            ]
            return str(positions)
        return str(0)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class DuetCurrentStateSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""

    _attr_icon = "mdi:printer-3d"

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet3D sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"{sensor_name}-{device_id}",
        )

    @property
    def native_value(self):
        """Return sensor state."""
        current_state_json_path = SENSOR_TYPES["Current State"]["json_path"]
        current_state = self.coordinator.get_sensor_state(
            current_state_json_path, self.sensor_name
        )
        if current_state is not None and current_state in PRINTER_STATUS:
            return current_state

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success
    
class DuetCurrentLayerSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""

    _attr_icon = "mdi:layers"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet3D sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"{sensor_name}-{device_id}",
        )

    @property
    def native_value(self):
        """Return sensor state."""
        current_layer_json_path = SENSOR_TYPES["Current Layer"]["json_path"]
        current_layer = self.coordinator.get_sensor_state(
            current_layer_json_path, self.sensor_name
        )
        return current_layer

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

class DuetTotalLayersSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""
    _attr_icon = "mdi:layers-triple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet3D sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"{sensor_name}-{device_id}",
        )

    @property
    def native_value(self):
        """Return sensor state."""
        total_layer_json_path = SENSOR_TYPES["Total Layers"]["json_path"]
        total_layer = self.coordinator.get_sensor_state(
            total_layer_json_path, self.sensor_name
        )
        return total_layer

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success
    
class DuetFileNameSensor(DuetPrintSensorBase):
    """Representation of an Duet3D sensor."""
    _attr_icon = "mdi:file"

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet3D sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"{sensor_name}-{device_id}",
        )

    @property
    def native_value(self):
        """Return sensor state."""
        file_name_json_path = SENSOR_TYPES["File Name"]["json_path"]
        file_path = self.coordinator.get_sensor_state(
            file_name_json_path, self.sensor_name
        )
        if file_path is None:
            return None
        file_name = os.path.splitext(os.path.basename(file_path))[0]
        return file_name

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class DuetExtruderSensor(DuetPrintSensorBase):
    """One extruder and the filament the firmware believes is loaded in it.

    The state is the filament name set by ``M701`` (empty until one is loaded and
    after every firmware restart). Consumers that track spools, such as
    SpoolmanSync, use this entity as the slot a spool is assigned to, so its
    ``unique_id`` must stay stable: it depends only on the extruder index.

    Attributes deliberately mirror those other printer integrations expose per
    slot: ``name`` and ``type`` are both the filament name, because RRF has no
    separate material field and filament names are conventionally the material
    (``/sys/filaments/PLA``).
    """

    _attr_icon = "mdi:printer-3d-nozzle"

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        extruder: int,
        device_id: str,
    ) -> None:
        """Initialize a new Duet3D extruder sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"extruder-{extruder}-{device_id}",
        )
        self._extruder = extruder

    def _describe(self) -> dict | None:
        for extruder in _current_extruders(self.coordinator):
            if extruder["extruder"] == self._extruder:
                return extruder
        return None

    @property
    def native_value(self):
        """Return the loaded filament name, or None when nothing is loaded."""
        described = self._describe()
        if described is None or not described["filament"]:
            return None
        return described["filament"]

    @property
    def extra_state_attributes(self):
        """Return extruder details."""
        described = self._describe()
        if described is None:
            return None
        return {
            "extruder": described["extruder"],
            "name": described["filament"],
            "type": described["filament"],
            "filament_diameter": described["filament_diameter"],
            "position": described["position"],
            "tools": described["tools"],
            "active": described["active"],
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self._describe() is not None


class DuetFilamentExtrudedSensor(DuetPrintSensorBase):
    """Filament extruded by the current job, before extrusion factors, in mm.

    The coordinator works the value out once per poll (``events.extruded_reading``): it
    follows ``job.rawExtrusion`` during a real job, holds the final value for the poll
    that ends the job so the print-end automation reads all of it, and is 0 otherwise,
    simulations included. A ``utility_meter`` ignores a drop rather than treating it as
    a reset, so a value latched until the next job would make it throw away that job's
    first reading; falling to 0 right after the job lets every job count from 0.
    """

    _attr_native_unit_of_measurement = "mm"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:printer-3d-nozzle"

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
    def native_value(self):
        """Return sensor state."""
        return self.coordinator.extruded_mm

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class DuetCurrentToolSensor(DuetPrintSensorBase):
    """Number of the selected tool, or -1 when none is selected."""

    _attr_icon = "mdi:wrench"

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
    def native_value(self):
        """Return sensor state."""
        tool = self.coordinator.get_sensor_state(
            SENSOR_TYPES["Current Tool"]["json_path"], "Current Tool"
        )
        if isinstance(tool, int) and not isinstance(tool, bool):
            return tool
        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class DuetValueSensor(DuetPrintSensorBase):
    """A sensor whose state is computed from the coordinator's data by a function.

    ``exists_fn`` marks a sensor for a discovered thing (fan, board, storage...)
    unavailable once the printer stops reporting that thing.
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
        unit=None,
        device_class=None,
        state_class=None,
        icon=None,
        category=None,
        suggested_unit=None,
        enabled_default=True,
    ) -> None:
        super().__init__(coordinator, name, unique_id)
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn
        self._exists_fn = exists_fn
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_icon = icon
        self._attr_entity_category = category
        self._attr_suggested_unit_of_measurement = suggested_unit
        self._attr_entity_registry_enabled_default = enabled_default

    @property
    def native_value(self):
        return self._value_fn(self.coordinator)

    @property
    def extra_state_attributes(self):
        return self._attrs_fn(self.coordinator) if self._attrs_fn else None

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        return self._exists_fn is None or self._exists_fn(self.coordinator)


def _hw(coordinator: DuetDataUpdateCoordinator, group: str, key: str, field: str | None = None):
    """One discovered thing (or one field of it) from the latest poll, or None."""
    item = coordinator.hardware.get(group, {}).get(key)
    return item if field is None or item is None else item.get(field)


def _heater_sensors(coordinator, device_id, key, label) -> list[DuetValueSensor]:
    """State (``off``, ``active``, ``fault`` ...) and average power of one heater."""

    def heat(c):
        return c.get_sensor_state(SENSOR_TYPES["Heat"]["json_path"])

    def state(c):
        role = c.heater_roles.get(key)
        return heater_state(heat(c), role) if role else None

    def power(c):
        role = c.heater_roles.get(key)
        return heater_power(heat(c), role) if role else None

    def exists(c):
        return key in c.heater_roles

    return [
        DuetValueSensor(
            coordinator, f"{label} heater state", f"{key}-state-{device_id}", state,
            exists_fn=exists, icon="mdi:radiator",
        ),
        DuetValueSensor(
            coordinator, f"{label} heater power", f"{key}-power-{device_id}", power,
            exists_fn=exists, unit=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:flash",
        ),
    ]


def _fan_sensor(coordinator, device_id, key, label) -> DuetValueSensor:
    return DuetValueSensor(
        coordinator, f"{label} speed", f"{key}-{device_id}",
        lambda c: _hw(c, "fans", key, "speed"),
        attrs_fn=lambda c: {
            "requested": _hw(c, "fans", key, "requested"),
            "rpm": _hw(c, "fans", key, "rpm"),
        },
        exists_fn=lambda c: _hw(c, "fans", key) is not None,
        unit=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT, icon="mdi:fan",
    )


def _flow_sensor(coordinator, device_id, extruder) -> DuetValueSensor:
    """The M221 extrusion factor of one extruder, as a percentage."""

    def flow(c):
        factor = as_number(c.get_sensor_state(f"status.move.extruders[{extruder}].factor"))
        return None if factor is None else round(factor * 100, 1)

    return DuetValueSensor(
        coordinator, f"Extruder {extruder} flow", f"flow-{extruder}-{device_id}", flow,
        exists_fn=lambda c: c.get_sensor_state(f"status.move.extruders[{extruder}]") is not None,
        unit=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT, icon="mdi:water-percent",
    )


# (field in hardware.build_boards, name suffix, options)
BOARD_METRICS = (
    ("mcu_temp", "MCU temperature", dict(
        unit=UnitOfTemperature.CELSIUS, device_class=SensorDeviceClass.TEMPERATURE)),
    ("v_in", "input voltage", dict(
        unit=UnitOfElectricPotential.VOLT, device_class=SensorDeviceClass.VOLTAGE)),
    ("v_12", "12V rail", dict(
        unit=UnitOfElectricPotential.VOLT, device_class=SensorDeviceClass.VOLTAGE)),
    ("free_ram", "free RAM", dict(
        unit=UnitOfInformation.BYTES, device_class=SensorDeviceClass.DATA_SIZE,
        suggested_unit=UnitOfInformation.KILOBYTES, enabled_default=False)),
)


def _board_sensor(coordinator, device_id, key, label, field, suffix, options) -> DuetValueSensor:
    return DuetValueSensor(
        coordinator, f"{label} {suffix}", f"{key}-{field}-{device_id}",
        lambda c: _hw(c, "boards", key, field),
        exists_fn=lambda c: _hw(c, "boards", key) is not None,
        state_class=SensorStateClass.MEASUREMENT, category=EntityCategory.DIAGNOSTIC,
        **options,
    )


def _ip_sensor(coordinator, device_id, key, label) -> DuetValueSensor:
    return DuetValueSensor(
        coordinator, f"{label} IP address", f"{key}-ip-{device_id}",
        lambda c: _hw(c, "interfaces", key, "ip"),
        exists_fn=lambda c: _hw(c, "interfaces", key) is not None,
        category=EntityCategory.DIAGNOSTIC, icon="mdi:ip-network",
    )


def _signal_sensor(coordinator, device_id, key, label) -> DuetValueSensor:
    return DuetValueSensor(
        coordinator, f"{label} signal strength", f"{key}-signal-{device_id}",
        lambda c: _hw(c, "interfaces", key, "signal"),
        exists_fn=lambda c: _hw(c, "interfaces", key) is not None,
        unit=SIGNAL_STRENGTH_DECIBELS_MILLIWATT, device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT, category=EntityCategory.DIAGNOSTIC,
    )


def _storage_sensor(coordinator, device_id, key, label) -> DuetValueSensor:
    return DuetValueSensor(
        coordinator, f"{label} free space", f"{key}-free-{device_id}",
        lambda c: _hw(c, "volumes", key, "free"),
        attrs_fn=lambda c: {"capacity": _hw(c, "volumes", key, "capacity")},
        # unmounting the card makes this unavailable rather than 0
        exists_fn=lambda c: _hw(c, "volumes", key) is not None,
        unit=UnitOfInformation.BYTES, device_class=SensorDeviceClass.DATA_SIZE,
        suggested_unit=UnitOfInformation.GIGABYTES, state_class=SensorStateClass.MEASUREMENT,
        category=EntityCategory.DIAGNOSTIC,
    )


def _monitor_sensor(coordinator, device_id, key, extruder) -> DuetValueSensor:
    """Filament monitor status: ``ok``, or what the monitor is complaining about."""
    return DuetValueSensor(
        coordinator, f"Extruder {extruder} filament monitor", f"{key}-status-{device_id}",
        lambda c: _hw(c, "monitors", key, "status"),
        attrs_fn=lambda c: {
            "type": _hw(c, "monitors", key, "type"),
            "enable_mode": _hw(c, "monitors", key, "enable_mode"),
            "filament_present": _hw(c, "monitors", key, "present"),
            "extruder": extruder,
        },
        exists_fn=lambda c: _hw(c, "monitors", key) is not None,
        icon="mdi:motion-sensor",
    )


def _minutes(value):
    number = as_number(value)
    return None if number is None else round(number / 60.0, 2)


def _positive(value):
    number = as_number(value)
    return number if number and number > 0 else None


def _text_or_none(value):
    return value if isinstance(value, str) and value else None


def _file_stem(value):
    return os.path.splitext(os.path.basename(value))[0] if _text_or_none(value) else None


def _percent(value):
    number = as_number(value)
    return None if number is None else round(number * 100, 1)


def _static_sensors(coordinator, device_id) -> list[DuetValueSensor]:
    """Job details and machine messages that exist on every printer."""

    def at(sensor_type, transform=lambda v: v):
        path = SENSOR_TYPES[sensor_type]["json_path"]
        return lambda c: transform(c.get_sensor_state(path))

    def sensor(name, value_fn, **options):
        return DuetValueSensor(coordinator, name, f"{name}-{device_id}", value_fn, **options)

    measurement = SensorStateClass.MEASUREMENT
    state_path = SENSOR_TYPES["Machine State"]["json_path"]

    def box(c):
        found = message_box(c.get_sensor_state(state_path))
        return found["message"] if found else None

    def box_attributes(c):
        found = message_box(c.get_sensor_state(state_path))
        return {k: v for k, v in found.items() if k != "message"} if found else None

    return [
        sensor("Filament Time Remaining", at("Filament Time Remaining", _minutes),
               unit="min", state_class=measurement, icon="mdi:clock-end"),
        sensor("Warm-up Duration", at("Warm-up Duration", _minutes),
               unit="min", state_class=measurement, icon="mdi:clock-start"),
        sensor("Last File Name", at("Last File Name", _file_stem), icon="mdi:file-clock"),
        sensor("Layer Height", at("Layer Height", _positive),
               unit="mm", state_class=measurement, icon="mdi:layers"),
        sensor("Object Height", at("Object Height", _positive),
               unit="mm", state_class=measurement, icon="mdi:arrow-expand-vertical"),
        sensor("Generated By", at("Generated By", _text_or_none),
               category=EntityCategory.DIAGNOSTIC, icon="mdi:application-cog"),
        sensor("Speed Factor", at("Speed Factor", _percent),
               unit=PERCENTAGE, state_class=measurement, icon="mdi:speedometer"),
        sensor("Display Message", at("Display Message", _text_or_none), icon="mdi:message-text"),
        sensor("Message Box", box, attrs_fn=box_attributes, icon="mdi:message-alert"),
    ]


def _job_sensors(coordinator, device_id) -> list[DuetValueSensor]:
    """When the job will end, how long it will take, and what the slicer expected."""

    def at(sensor_type):
        path = SENSOR_TYPES[sensor_type]["json_path"]
        return lambda c: c.get_sensor_state(path)

    def sensor(name, value_fn, **options):
        return DuetValueSensor(coordinator, name, f"{name}-{device_id}", value_fn, **options)

    timestamp = SensorDeviceClass.TIMESTAMP
    left = SENSOR_TYPES["Time Remaining"]["json_path"]
    duration = SENSOR_TYPES["Time Elapsed"]["json_path"]
    filament = SENSOR_TYPES["Progress"]["json_path"]

    def read_at(c):
        return (c.data or {}).get("last_read_time")

    def slicer_attrs(c):
        lengths = slicer_filament(c.get_sensor_state(filament))
        return {"extruders": lengths} if lengths else None

    def objects(c):
        return build_objects(c.get_sensor_state(SENSOR_TYPES["Build"]["json_path"]))

    def objects_attrs(c):
        found = objects(c)
        if not found:
            return None
        current = as_number(c.get_sensor_state(SENSOR_TYPES["Build"]["json_path"] + ".currentObject"))
        return {
            "objects": found,
            "cancelled": sum(1 for o in found if o["cancelled"]),
            "current": current if current is not None and current >= 0 else None,
        }

    return [
        sensor("Print ETA", lambda c: eta(read_at(c), c.get_sensor_state(left)),
               device_class=timestamp, icon="mdi:clock-check"),
        sensor("Print start time", lambda c: c.job_started_at,
               device_class=timestamp, icon="mdi:clock-start"),
        sensor("Print end time", lambda c: c.job_ended_at,
               device_class=timestamp, icon="mdi:clock-end"),
        sensor("Projected total duration",
               lambda c: projected_total_minutes(c.get_sensor_state(duration), c.get_sensor_state(left)),
               unit="min", state_class=SensorStateClass.MEASUREMENT, icon="mdi:timer-sand"),
        sensor("Print speed", lambda c: as_number(at("Print Speed")(c)),
               unit="mm/s", state_class=SensorStateClass.MEASUREMENT, icon="mdi:speedometer"),
        sensor("Slicer filament length", lambda c: slicer_total(c.get_sensor_state(filament)),
               attrs_fn=slicer_attrs, unit="mm", icon="mdi:ruler"),
        sensor("Print objects", lambda c: len(objects(c)) or None,
               attrs_fn=objects_attrs, icon="mdi:cube-outline"),
    ]
