"""Support for monitoring Duet3D sensors."""
import logging
import os
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from . import DuetDataUpdateCoordinator
from .extruders import build_extruders

_LOGGER = logging.getLogger(__name__)

NOTIFICATION_ID = "duet3d_notification"
NOTIFICATION_TITLE = "Duet3d sensor setup error"

from .const import (
    DOMAIN,
    SENSOR_TYPES,
    PRINTER_STATUS,
    CONF_STANDALONE,
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

    tools = coordinator.get_tools()
    device_id = config_entry.entry_id
    assert device_id is not None

    if not tools:
        hass.components.persistent_notification.async_create(
            "Your printer appears to be offline.<br />"
            "If you do not want to have your printer on <br />"
            " at all times, and you would like to monitor <br /> "
            "temperatures, please add <br />"
            "bed and/or number&#95of&#95tools to your config <br />"
            "and restart.",
            title=NOTIFICATION_TITLE,
            notification_id=NOTIFICATION_ID,
        )
    known_tools = set()

    @callback
    def async_add_tool_sensors() -> None:
        tool_types = ["current", "active", "standby"]
        bed_types = ["current", "active"]
        if not coordinator.data["status"]:
            return

        new_tools = []
        for tool in tools:
            if tool == "bed":
                assert device_id is not None
                for bed_type in bed_types:
                    if tool + "," + bed_type not in known_tools:
                        known_tools.add(tool + "," + bed_type)
                        new_tools.append(
                            DuetTemperatureSensor(
                                coordinator,
                                f"Tool {tool} {bed_type} temperature",
                                tool,
                                bed_type,
                                device_id,
                            )
                        )
            else:
                assert device_id is not None
                for tool_type in tool_types:
                    if str(tool) + "," + tool_type not in known_tools:
                        known_tools.add(str(tool) + "," + tool_type)
                        new_tools.append(
                            DuetTemperatureSensor(
                                coordinator,
                                f"Tool {tool} {tool_type} temperature",
                                tool,
                                tool_type,
                                device_id,
                            )
                        )
        async_add_entities(new_tools)

    config_entry.async_on_unload(coordinator.async_add_listener(async_add_tool_sensors))

    if coordinator.data["status"]:
        async_add_tool_sensors()

    known_extruders: set[int] = set()

    @callback
    def async_add_extruder_sensors() -> None:
        """Add one sensor per extruder, including any that appear later."""
        new_extruders = []
        for extruder in _current_extruders(coordinator):
            index = extruder["extruder"]
            if index in known_extruders:
                continue
            known_extruders.add(index)
            new_extruders.append(
                DuetExtruderSensor(
                    coordinator, f"Extruder {index}", index, device_id
                )
            )
        if new_extruders:
            async_add_entities(new_extruders)

    config_entry.async_on_unload(
        coordinator.async_add_listener(async_add_extruder_sensors)
    )
    async_add_extruder_sensors()

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


class DuetTemperatureSensor(DuetPrintSensorBase):
    """Representation of an Duet sensor."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        sensor_name: str,
        tool: str,
        sensor_type: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet sensor."""
        super().__init__(
            coordinator,
            sensor_name,
            f"{tool}-{sensor_type}-{device_id}",
        )
        self._sensor_type = sensor_type
        self._no_of_tool = tool

    @property
    def native_value(self):
        """Return sensor state."""
        if self._no_of_tool == "bed":
            json_path = SENSOR_TYPES["Bed Temperatures"]["json_path"]
            bed_heater = self.coordinator.get_sensor_state(
                json_path + "." + self._sensor_type, "Bed Temperatures"
            )
            if self.coordinator.config_entry.data[CONF_STANDALONE]:
                bed_heater = bed_heater[self._sensor_type]
            if bed_heater is not None:
                return bed_heater
            else:
                return -1
        else:
            json_path = SENSOR_TYPES["Tool Temperatures"]["json_path"]
            tool_heater = self.coordinator.get_sensor_state(
                f"{json_path}", "Tool Temperatures"
            )
            if tool_heater is not None:
                return tool_heater[self._no_of_tool][self._sensor_type]
            else:
                return -1


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
        filament_info_json_path = SENSOR_TYPES["Progress"]["json_path"]
        job_printed_filament_json_path = SENSOR_TYPES["Filament Extrusion"]["json_path"]
        job_printed_filament = self.coordinator.get_sensor_state(
            job_printed_filament_json_path, "Filament Extrusion"
        )
        filament_info = self.coordinator.get_sensor_state(
            filament_info_json_path, "Progress"
        )

        if filament_info:
            job_total_mm_of_filament = filament_info[0]
        else:
            return 0
        if job_printed_filament is not None and job_total_mm_of_filament is not None:
            progress_percentage = (
                job_printed_filament / job_total_mm_of_filament
            ) * 100
            return round(progress_percentage, 2)
        else:
            return 0


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

    Resets when a new job starts, so it is a ``total_increasing`` sensor.
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
        extruded = self.coordinator.get_sensor_state(
            SENSOR_TYPES["Filament Extrusion"]["json_path"], "Filament Extrusion"
        )
        if isinstance(extruded, (int, float)) and not isinstance(extruded, bool):
            return round(extruded, 2)
        return 0

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
