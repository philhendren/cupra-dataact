"""Diagnostics: the merged raw data points, used to map new fields."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import DataActConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "vin", "identifier"}
# position fields; the portal names vary, so match by substring
LOCATION_HINTS = ("latitude", "longitude", "gps", "position")


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: DataActConfigEntry) -> dict[str, Any]:
    vehicles = []
    for vehicle in entry.runtime_data.vehicles.values():
        points = [
            {**p.as_dict(), "value": "**REDACTED**"}
            if any(h in (p.name or "").lower() for h in LOCATION_HINTS)
            else p.as_dict()
            for p in sorted(vehicle.state.points.values(), key=lambda p: p.name or p.key)
        ]
        vehicles.append(
            {
                "model": vehicle.model,
                "configured": vehicle.configured,
                "after": vehicle.state.after,
                "newest_listed": vehicle.state.newest_listed,
                "point_count": len(points),
                "points": points,
            }
        )
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "vehicles": vehicles,
    }
