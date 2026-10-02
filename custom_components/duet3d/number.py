"""Numbers: heater targets, speed factor and per-extruder flow.

These sit alongside the read-only sensors of the same name (different entity
domain, same readings) rather than replacing them, so no existing entity id or
unique id changes. They can be changed in any state, as in Duet Web Control:
adjusting a temperature, speed or flow mid-print is normal. The result is read
back on the next poll; nothing is shown optimistically.
"""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuetDataUpdateCoordinator
from .const import DOMAIN
from .controls import send_checked
from .entity import DuetEntity, add_dynamic
from .extruders import build_extruders
from .model import as_number, heater_limit, heater_value

# Marlin keeps heaters this far below their limit; so does this, to leave the
# firmware's own over-temperature protection something to do.
LIMIT_MARGIN = 15
DEFAULT_MAX_TEMPERATURE = 280


def _target_command(role: dict, value: int) -> str:
    if role["kind"] == "tool":
        return f"M104 S{value} T{role['tool']}"
    if role["kind"] == "bed":
        return f"M140 P{role['index']} S{value}"
    return f"M141 P{role['index']} S{value}"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entry_id = config_entry.entry_id

    def add(discover):
        add_dynamic(coordinator, config_entry, async_add_entities, discover)

    def targets():
        for key, role in coordinator.heater_roles.items():
            # M104/M140/M141 set the target of every heater of the tool, bed or chamber
            # at once, so a second heater of the same one has no target of its own.
            if role["slot"]:
                continue
            yield key, lambda k=key, r=role: DuetTargetNumber(coordinator, k, r["label"], entry_id)

    def speed():
        yield "speed", lambda: DuetSpeedNumber(coordinator, entry_id)

    def flow():
        for extruder in build_extruders(
            coordinator.get_sensor_state("status.move.extruders"),
            coordinator.get_sensor_state("status.tools"),
            coordinator.get_sensor_state("status.state.currentTool"),
        ):
            index = extruder["extruder"]
            yield index, lambda i=index: DuetFlowNumber(coordinator, i, entry_id)

    for discover in (targets, speed, flow):
        add(discover)


class DuetTargetNumber(DuetEntity, NumberEntity):
    """The active temperature target of one heater."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, coordinator, key: str, label: str, entry_id: str) -> None:
        super().__init__(coordinator, f"{label} target", f"target-{key}-{entry_id}")
        self._key = key

    @property
    def _role(self):
        return self.coordinator.heater_roles.get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._role is not None

    @property
    def native_max_value(self) -> float:
        role = self._role
        limit = (
            heater_limit(self.coordinator.get_sensor_state("status.heat"), role) if role else None
        )
        return (limit - LIMIT_MARGIN) if limit else DEFAULT_MAX_TEMPERATURE

    @property
    def native_value(self):
        role = self._role
        if role is None:
            return None
        return heater_value(
            self.coordinator.get_sensor_state("status.heat"),
            self.coordinator.get_sensor_state("status.tools"),
            role,
            "active",
        )

    async def async_set_native_value(self, value: float) -> None:
        await send_checked(self.coordinator, _target_command(self._role, int(value)), None, "set temperature", wait=True)


class DuetSpeedNumber(DuetEntity, NumberEntity):
    """The M220 speed factor, as a percentage."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 10
    _attr_native_max_value = 300
    _attr_native_step = 1
    _attr_icon = "mdi:speedometer"

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator, "Speed factor", f"speed-factor-{entry_id}")

    @property
    def native_value(self):
        factor = as_number(self.coordinator.get_sensor_state("status.move.speedFactor"))
        return None if factor is None else round(factor * 100, 1)

    async def async_set_native_value(self, value: float) -> None:
        await send_checked(self.coordinator, f"M220 S{int(value)}", None, "set speed", wait=True)


class DuetFlowNumber(DuetEntity, NumberEntity):
    """The M221 extrusion factor of one extruder, as a percentage."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 10
    _attr_native_max_value = 300
    _attr_native_step = 1
    _attr_icon = "mdi:water-percent"

    def __init__(self, coordinator, extruder: int, entry_id: str) -> None:
        super().__init__(coordinator, f"Extruder {extruder} flow", f"flow-control-{extruder}-{entry_id}")
        self._extruder = extruder

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.get_sensor_state(f"status.move.extruders[{self._extruder}]") is not None
        )

    @property
    def native_value(self):
        factor = as_number(
            self.coordinator.get_sensor_state(f"status.move.extruders[{self._extruder}].factor")
        )
        return None if factor is None else round(factor * 100, 1)

    async def async_set_native_value(self, value: float) -> None:
        await send_checked(self.coordinator, f"M221 D{self._extruder} S{int(value)}", None, "set flow", wait=True)
