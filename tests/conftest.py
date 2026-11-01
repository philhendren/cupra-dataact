"""Shared fixtures: a mocked portal client and a helper to set up the integration."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cupra_dataact.api import Point, VehicleState, parse_time
from custom_components.cupra_dataact.const import CONF_BRAND, DOMAIN

VIN = "VSSZZZK1ZRP000001"
VIN2 = "VSSZZZKMZRP000002"
DATA = {CONF_USERNAME: "me@example.com", CONF_PASSWORD: "pw", CONF_BRAND: "cupra"}
MEASURED = "2026-10-01T10:00:00Z"
DELIVERED = "2026-10-01T10:15:00Z"

DEFAULT_VALUES = {
    "state_of_charge": "72",
    "mileage": "12345",
    "cruising_range_primary_engine": "280",
    "settings.target_soc": "80",
    "remaining_charging_time": "95",
    "charge_power": "7.2",
    "plug_state": "connected",
    "charging_state": "charging",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def fill(state: VehicleState, values: dict[str, str], ts: str = MEASURED) -> int:
    """Merge one dataset of named points into the state, as DataActClient.update would."""
    state.merge([Point("", k, v, parse_time(ts)) for k, v in values.items()])
    state.newest_listed = parse_time(DELIVERED)
    return 1


def updater(values: dict[str, str] | None = None) -> Callable[[str, VehicleState], int]:
    return lambda vin, state: fill(state, DEFAULT_VALUES if values is None else values)


def configure(client: MagicMock, vehicles: list[dict[str, Any]] | None = None, update: Any = None) -> MagicMock:
    client.login = AsyncMock()
    client.close = AsyncMock()
    client.vehicles = AsyncMock(
        return_value=vehicles if vehicles is not None else [{"vin": VIN, "name": "Born", "model": "Born"}]
    )
    client.update = AsyncMock(side_effect=update if update is not None else updater())
    return client


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """The DataActClient the integration creates during setup."""
    with patch("custom_components.cupra_dataact.DataActClient") as cls:
        yield configure(cls.return_value)


@pytest.fixture
def flow_client() -> Generator[MagicMock]:
    """The DataActClient the config flow creates to validate credentials."""
    with patch("custom_components.cupra_dataact.config_flow.DataActClient") as cls:
        yield configure(cls.return_value)


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="cupra:me@example.com", title="me@example.com")
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result
