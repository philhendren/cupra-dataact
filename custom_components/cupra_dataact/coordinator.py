"""Polling coordinator for the EU Data Act portal."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, ConsentRequired, DataActClient, DataActError, NotConfigured, VehicleState
from .const import DOMAIN, PORTAL_URL, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


@dataclass
class Vehicle:
    vin: str
    name: str
    model: str | None
    state: VehicleState
    configured: bool = True

    @property
    def last_data(self) -> datetime | None:
        stamps = [p.timestamp for p in self.state.points.values() if p.timestamp]
        return max(stamps) if stamps else None


class DataActCoordinator(DataUpdateCoordinator[dict[str, Vehicle]]):
    """Fetch new datasets for all vehicles of one account and keep the merged state on disk."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: DataActClient,
        interval: timedelta,
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=interval)
        self.client = client
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.vehicles: dict[str, Vehicle] = {}

    async def _async_setup(self) -> None:
        """Restore merged data points so values survive restarts without a re-download."""
        saved = await self._store.async_load() or {}
        try:
            await self.client.login()
            listed = await self.client.vehicles()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ConsentRequired as err:
            self._consent_issue(err.url)
            raise UpdateFailed(str(err)) from err
        except DataActError as err:
            raise UpdateFailed(str(err)) from err
        ir.async_delete_issue(self.hass, DOMAIN, f"consent_{self.config_entry.entry_id}")

        for v in listed:
            raw = saved.get(v["vin"])
            state = VehicleState.from_dict(raw) if raw else VehicleState()
            self.vehicles[v["vin"]] = Vehicle(v["vin"], v["name"], v.get("model"), state)

    async def _async_update_data(self) -> dict[str, Vehicle]:
        changed = False
        errors = []
        for vin, vehicle in self.vehicles.items():
            try:
                merged = await self.client.update(vin, vehicle.state)
            except NotConfigured:
                vehicle.configured = False
                self._not_configured_issue(vin, vehicle.name)
                continue
            except AuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except ConsentRequired as err:
                self._consent_issue(err.url)
                raise UpdateFailed(str(err)) from err
            except DataActError as err:
                errors.append(f"{vehicle.name}: {err}")
                continue
            if not vehicle.configured:
                vehicle.configured = True
                ir.async_delete_issue(self.hass, DOMAIN, f"not_configured_{vin}")
            changed |= merged > 0

        if changed:
            self._store.async_delay_save(lambda: {vin: v.state.to_dict() for vin, v in self.vehicles.items()}, 10)
        # keep serving the merged values over transient portal errors as long as one car updated
        if errors and len(errors) == len(self.vehicles):
            raise UpdateFailed("; ".join(errors))
        for err in errors:
            _LOGGER.warning("Update failed for %s", err)
        return self.vehicles

    async def async_remove_store(self) -> None:
        await self._store.async_remove()

    def _not_configured_issue(self, vin: str, name: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"not_configured_{vin}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="not_configured",
            translation_placeholders={"name": name, "url": PORTAL_URL},
            learn_more_url=PORTAL_URL,
        )

    def _consent_issue(self, url: str | None) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"consent_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="consent_required",
            translation_placeholders={"url": PORTAL_URL},
            learn_more_url=url or PORTAL_URL,
        )
