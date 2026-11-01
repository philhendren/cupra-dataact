"""Sensors for the CUPRA EU Data Act integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfLength,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import DataActConfigEntry
from .api import Point
from .const import (
    CHARGE_POWER,
    CHARGING_STATE,
    CONF_LOCAL_SOC,
    ODOMETER,
    RANGE,
    REMAINING_CHARGE_TIME,
    SOC,
    TARGET_SOC,
)
from .coordinator import DataActCoordinator
from .entity import DataActEntity


def _float(p: Point | None) -> float | None:
    if p is None:
        return None
    try:
        return float(p.value)
    except ValueError:
        return None


@dataclass(frozen=True, kw_only=True)
class DataActSensorDescription(SensorEntityDescription):
    ids: tuple[str, ...]
    convert: Callable[[Point | None], Any] = _float


SENSORS: tuple[DataActSensorDescription, ...] = (
    DataActSensorDescription(
        key="battery_level",
        ids=SOC,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DataActSensorDescription(
        key="range",
        ids=RANGE,
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DataActSensorDescription(
        key="odometer",
        ids=ODOMETER,
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    DataActSensorDescription(
        key="target_soc",
        ids=TARGET_SOC,
        native_unit_of_measurement=PERCENTAGE,
    ),
    DataActSensorDescription(
        key="remaining_charging_time",
        ids=REMAINING_CHARGE_TIME,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    DataActSensorDescription(
        key="charge_power",
        ids=CHARGE_POWER,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    DataActSensorDescription(
        key="charging_state",
        ids=CHARGING_STATE,
        convert=lambda p: p.value if p else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DataActConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    local_soc = entry.options.get(CONF_LOCAL_SOC)
    entities: list[SensorEntity] = []
    for vin in coordinator.vehicles:
        entities.extend(DataActSensor(coordinator, vin, d) for d in SENSORS)
        entities.append(LastDataSensor(coordinator, vin))
        entities.append(LastDeliverySensor(coordinator, vin))
        if local_soc:
            entities.append(BestSocSensor(coordinator, vin, local_soc))
    async_add_entities(entities)


class DataActSensor(DataActEntity, SensorEntity):
    entity_description: DataActSensorDescription

    def __init__(self, coordinator: DataActCoordinator, vin: str, description: DataActSensorDescription) -> None:
        super().__init__(coordinator, vin, description.key)
        self.entity_description = description

    @property
    def _point(self) -> Point | None:
        return self.vehicle.state.lookup(*self.entity_description.ids)

    @property
    def native_value(self) -> Any:
        return self.entity_description.convert(self._point)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        # the portal lags the car by 15 min to several hours; expose when the value was measured
        p = self._point
        if p is None:
            return None
        return {"measured_at": p.timestamp, "field": p.name or p.key}


class LastDataSensor(DataActEntity, SensorEntity):
    """Newest measurement time in the merged data."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataActCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin, "last_data")

    @property
    def native_value(self) -> datetime | None:
        return self.vehicle.last_data


class LastDeliverySensor(DataActEntity, SensorEntity):
    """Newest dataset the portal listed, including empty ones; shows whether the portal pipeline runs."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataActCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin, "last_delivery")

    @property
    def native_value(self) -> datetime | None:
        return self.vehicle.state.newest_listed


class BestSocSensor(DataActEntity, SensorEntity):
    """The fresher of the portal SoC and a local live SoC sensor (e.g. an OBD dongle via MQTT)."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataActCoordinator, vin: str, local_entity: str) -> None:
        super().__init__(coordinator, vin, "battery_level_best")
        self._local_entity = local_entity
        self._source: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_state_change_event(self.hass, [self._local_entity], self._local_changed))

    @callback
    def _local_changed(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    def _local(self) -> tuple[float, datetime] | None:
        state = self.hass.states.get(self._local_entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            return float(state.state), state.last_reported
        except ValueError:
            return None

    @property
    def native_value(self) -> float | None:
        portal = self.vehicle.state.lookup(*SOC)
        portal_value = _float(portal)
        local = self._local()
        if local is not None and (portal is None or portal.timestamp is None or local[1] >= portal.timestamp):
            self._source = self._local_entity
            return local[0]
        self._source = "portal" if portal_value is not None else None
        return portal_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source": self._source}
