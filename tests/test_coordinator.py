"""Coordinator: persistence, partial failures and repair issues."""

from __future__ import annotations

from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.cupra_dataact.api import AuthError, ConsentRequired, DataActError, NotConfigured
from custom_components.cupra_dataact.const import DEFAULT_SCAN_INTERVAL, DOMAIN

from .conftest import VIN, VIN2, fill, setup_entry, updater


async def refresh(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(DEFAULT_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_restores_saved_points(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, hass_storage: dict
) -> None:
    """Saved data points are restored at startup, so sensors have values before any download."""
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{config_entry.entry_id}",
        "data": {
            VIN: {
                "identifier": "req-1",
                "after": "2026-10-01T09:00:00+00:00",
                "seq": 3,
                "points": [{"key": "", "name": "mileage", "value": "999", "timestamp": "2026-10-01T09:00:00+00:00"}],
            }
        },
    }
    mock_client.update.side_effect = lambda vin, state: 0  # nothing new on the portal
    assert await setup_entry(hass, config_entry)

    vehicle = config_entry.runtime_data.vehicles[VIN]
    assert vehicle.state.identifier == "req-1"
    assert vehicle.state.seq == 3
    assert hass.states.get("sensor.born_odometer").state == "999.0"


async def test_saves_merged_points(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Newly merged data points are written to storage."""
    assert await setup_entry(hass, config_entry)
    freezer.tick(15)  # delayed save
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    saved = hass_storage[f"{DOMAIN}.{config_entry.entry_id}"]["data"][VIN]
    assert {p["name"]: p["value"] for p in saved["points"]}["state_of_charge"] == "72"


async def test_one_vehicle_failing_keeps_the_other(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """One car failing to update doesn't stop the other car's sensors."""
    mock_client.vehicles.return_value = [
        {"vin": VIN, "name": "Born", "model": "Born"},
        {"vin": VIN2, "name": "Formentor", "model": "Formentor"},
    ]

    def update(vin, state):
        if vin == VIN2:
            raise DataActError("download failed")
        return fill(state, {"state_of_charge": "50"})

    mock_client.update.side_effect = update
    assert await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.born_battery_level").state == "50.0"
    assert hass.states.get("sensor.formentor_battery_level").state == "unknown"


async def test_all_vehicles_failing_marks_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """Sensors go unavailable when every car fails, and recover on the next good poll."""
    assert await setup_entry(hass, config_entry)
    assert hass.states.get("sensor.born_battery_level").state == "72.0"

    mock_client.update.side_effect = DataActError("portal down")
    await refresh(hass, freezer)
    assert hass.states.get("sensor.born_battery_level").state == "unavailable"

    mock_client.update.side_effect = updater({"state_of_charge": "73"})
    await refresh(hass, freezer)
    assert hass.states.get("sensor.born_battery_level").state == "73.0"


async def test_not_configured_issue_is_created_and_cleared(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A missing data request raises a repair issue, which clears once data arrives."""
    mock_client.update.side_effect = NotConfigured(VIN)
    assert await setup_entry(hass, config_entry)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"not_configured_{VIN}") is not None
    assert not config_entry.runtime_data.vehicles[VIN].configured

    mock_client.update.side_effect = updater()
    await refresh(hass, freezer)
    assert registry.async_get_issue(DOMAIN, f"not_configured_{VIN}") is None
    assert config_entry.runtime_data.vehicles[VIN].configured


async def test_auth_error_during_update_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """An authentication error during polling starts re-authentication."""
    assert await setup_entry(hass, config_entry)
    mock_client.update.side_effect = AuthError("expired")
    await refresh(hass, freezer)
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_consent_during_update_raises_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A consent page during polling raises a repair issue and marks sensors unavailable."""
    assert await setup_entry(hass, config_entry)
    mock_client.update.side_effect = ConsentRequired("terms")
    await refresh(hass, freezer)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"consent_{config_entry.entry_id}") is not None
    assert hass.states.get("sensor.born_battery_level").state == "unavailable"


async def test_last_data_is_newest_point(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Last measurement is the newest timestamp across all points."""

    def update(vin, state):
        fill(state, {"mileage": "1"}, "2026-10-01T08:00:00Z")
        return fill(state, {"state_of_charge": "60"}, "2026-10-01T09:30:00Z")

    mock_client.update.side_effect = update
    assert await setup_entry(hass, config_entry)
    assert hass.states.get("sensor.born_last_measurement").state == "2026-10-01T09:30:00+00:00"
