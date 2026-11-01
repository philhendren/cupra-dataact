"""Diagnostics download."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cupra_dataact.diagnostics import async_get_config_entry_diagnostics

from .conftest import VIN, setup_entry, updater


async def test_diagnostics_redacts_credentials_and_location(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.update.side_effect = updater(
        {"state_of_charge": "72", "gps_latitude": "52.52", "parking_position.longitude": "13.40"}
    )
    assert await setup_entry(hass, config_entry)

    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["entry"]["username"] == "**REDACTED**"
    assert diag["entry"]["password"] == "**REDACTED**"
    assert diag["entry"]["brand"] == "cupra"

    (vehicle,) = diag["vehicles"]
    values = {p["name"]: p["value"] for p in vehicle["points"]}
    assert values == {
        "gps_latitude": "**REDACTED**",
        "parking_position.longitude": "**REDACTED**",
        "state_of_charge": "72",
    }
    assert vehicle["point_count"] == 3
    assert vehicle["configured"] is True
    assert VIN not in str(diag)
