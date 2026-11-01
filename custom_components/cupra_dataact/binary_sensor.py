"""Binary sensors for the CUPRA EU Data Act integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DataActConfigEntry
from .api import charge_status
from .const import CHARGING_SCENARIO, CHARGING_STATE, PLUG_STATE
from .coordinator import DataActCoordinator
from .entity import DataActEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DataActConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        entity
        for vin in coordinator.vehicles
        for entity in (
            ChargeBinarySensor(coordinator, vin, "plugged_in", BinarySensorDeviceClass.PLUG, 0),
            ChargeBinarySensor(coordinator, vin, "charging", BinarySensorDeviceClass.BATTERY_CHARGING, 1),
        )
    )


class ChargeBinarySensor(DataActEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: DataActCoordinator,
        vin: str,
        key: str,
        device_class: BinarySensorDeviceClass,
        index: int,
    ) -> None:
        super().__init__(coordinator, vin, key)
        self._attr_device_class = device_class
        self._index = index

    @property
    def is_on(self) -> bool | None:
        return charge_status(self.vehicle.state, PLUG_STATE, CHARGING_STATE, CHARGING_SCENARIO)[self._index]
