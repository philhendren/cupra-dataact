"""Base entity for the CUPRA EU Data Act integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BRAND, DEFAULT_BRAND, DOMAIN, MANUFACTURERS
from .coordinator import DataActCoordinator, Vehicle


class DataActEntity(CoordinatorEntity[DataActCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: DataActCoordinator, vin: str, key: str) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"{vin}_{key}"
        self._attr_translation_key = key
        vehicle = self.vehicle
        brand = coordinator.config_entry.data.get(CONF_BRAND, DEFAULT_BRAND)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer=MANUFACTURERS.get(brand, brand),
            model=vehicle.model,
            name=vehicle.name,
            serial_number=vin,
        )

    @property
    def vehicle(self) -> Vehicle:
        return self.coordinator.vehicles[self._vin]
