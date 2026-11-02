"""Sensor and binary sensor entities."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cupra_dataact.api import Point, parse_time
from custom_components.cupra_dataact.const import CONF_DISTANCE_UNIT, CONF_LOCAL_SOC, DOMAIN

from .conftest import DATA, VIN, fill, setup_entry, updater


async def test_value_sensors(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    """Each value sensor shows the expected state and unit."""
    assert await setup_entry(hass, config_entry)
    expected = {
        "sensor.born_battery_level": ("72.0", "%"),
        "sensor.born_range": ("280.0", "km"),
        "sensor.born_odometer": ("12345.0", "km"),
        "sensor.born_charge_target": ("80.0", "%"),
        "sensor.born_remaining_charging_time": ("95.0", "min"),
        "sensor.born_charging_state": ("charging", None),
    }
    for entity_id, (value, unit) in expected.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state == value, entity_id
        assert state.attributes.get("unit_of_measurement") == unit, entity_id


async def test_distance_sensors_in_miles(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """With the miles option, range and odometer show the portal values in miles, unconverted."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=DATA, options={CONF_DISTANCE_UNIT: "mi"}, unique_id="cupra:me@example.com"
    )
    entry.add_to_hass(hass)
    assert await setup_entry(hass, entry)
    for entity_id, value in {"sensor.born_range": "280.0", "sensor.born_odometer": "12345.0"}.items():
        state = hass.states.get(entity_id)
        assert state.state == value, entity_id
        assert state.attributes["unit_of_measurement"] == "mi", entity_id
    assert hass.states.get("sensor.born_battery_level").attributes["unit_of_measurement"] == "%"


async def test_measured_at_and_field_attributes(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Sensors expose when the value was measured and which field it came from."""
    assert await setup_entry(hass, config_entry)
    attrs = hass.states.get("sensor.born_battery_level").attributes
    assert attrs["field"] == "state_of_charge"
    assert attrs["measured_at"] == parse_time("2026-10-01T10:00:00Z")


async def test_missing_values_are_unknown_without_attributes(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Missing fields show as unknown with no measurement attributes."""
    mock_client.update.side_effect = updater({"mileage": "5"})
    assert await setup_entry(hass, config_entry)
    state = hass.states.get("sensor.born_battery_level")
    assert state.state == "unknown"
    assert "measured_at" not in state.attributes
    assert hass.states.get("binary_sensor.born_plugged_in").state == "unknown"


async def test_non_numeric_value_is_unknown(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A non-numeric value for a numeric sensor shows as unknown."""
    mock_client.update.side_effect = updater({"state_of_charge": "invalid"})
    assert await setup_entry(hass, config_entry)
    assert hass.states.get("sensor.born_battery_level").state == "unknown"


async def test_guid_soc_key_wins_over_named_field(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The current-SoC GUID key wins over the generic `state_of_charge` field."""

    def update(vin, state):
        state.merge(
            [
                Point("", "state_of_charge", "40", parse_time("2026-10-01T10:00:00Z")),
                Point("506cb83e-f99f-3af3-bbeb-0429b69a78d9", "battery_state_report.soc", "41", None),
            ]
        )
        return 1

    mock_client.update.side_effect = update
    assert await setup_entry(hass, config_entry)
    assert hass.states.get("sensor.born_battery_level").state == "41.0"


async def test_charge_power_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Charge power is created but disabled by default."""
    assert await setup_entry(hass, config_entry)
    entry = er.async_get(hass).async_get("sensor.born_charge_power")
    assert entry is not None and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get("sensor.born_charge_power") is None


async def test_diagnostic_timestamps(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Last measurement and last portal delivery are diagnostic timestamp sensors."""
    assert await setup_entry(hass, config_entry)
    registry = er.async_get(hass)
    for entity_id in ("sensor.born_last_measurement", "sensor.born_last_portal_delivery"):
        assert registry.async_get(entity_id).entity_category == "diagnostic"
    assert hass.states.get("sensor.born_last_portal_delivery").state == "2026-10-01T10:15:00+00:00"


async def test_device_info(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    """The car's device has its name, CUPRA as manufacturer and the VIN as serial number."""
    assert await setup_entry(hass, config_entry)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, VIN)})
    assert device.name == "Born"
    assert device.manufacturer == "CUPRA"
    assert device.serial_number == VIN


async def test_seat_brand_manufacturer(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A SEAT account labels the device manufacturer as SEAT."""
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "brand": "seat"})
    entry.add_to_hass(hass)
    assert await setup_entry(hass, entry)
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, VIN)}).manufacturer == "SEAT"


@pytest.mark.parametrize(
    ("values", "plugged", "charging"),
    [
        ({"plug_state": "connected", "charging_state": "charging"}, "on", "on"),
        ({"plug_state": "connected", "charging_state": "readyForCharging"}, "on", "off"),
        ({"plug_state": "disconnected", "charging_state": "notReadyForCharging"}, "off", "off"),
        ({"charging_state_report.charging_scenario": "AC_CHARGING_FINISHED"}, "on", "off"),
        ({"charging_state_report.charging_scenario": "DC_CHARGING_ACTIVE"}, "on", "on"),
    ],
)
async def test_binary_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    values: dict[str, str],
    plugged: str,
    charging: str,
) -> None:
    """Plugged in and charging follow the car's plug, charge state and charging scenario."""
    mock_client.update.side_effect = updater(values)
    assert await setup_entry(hass, config_entry)
    assert hass.states.get("binary_sensor.born_plugged_in").state == plugged
    assert hass.states.get("binary_sensor.born_charging").state == charging


# --- Battery level (best) -----------------------------------------------------


@pytest.fixture
def local_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options={CONF_LOCAL_SOC: "sensor.obd_soc"})
    entry.add_to_hass(hass)
    return entry


async def test_best_soc_absent_without_option(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Battery level (best) isn't created without a local sensor."""
    assert await setup_entry(hass, config_entry)
    assert hass.states.get("sensor.born_battery_level_best") is None


async def test_best_soc_uses_fresher_local_and_tracks_changes(
    hass: HomeAssistant, local_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Battery level (best) uses a newer local reading and updates when it changes."""
    hass.states.async_set("sensor.obd_soc", "80")
    assert await setup_entry(hass, local_entry)
    best = hass.states.get("sensor.born_battery_level_best")
    assert best.state == "80.0" and best.attributes["source"] == "sensor.obd_soc"

    hass.states.async_set("sensor.obd_soc", "81")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.born_battery_level_best").state == "81.0"


async def test_best_soc_prefers_newer_portal_value(
    hass: HomeAssistant, local_entry: MockConfigEntry, mock_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """Battery level (best) uses the portal value when it is newer than the local reading."""
    freezer.move_to("2026-10-01T09:00:00Z")
    hass.states.async_set("sensor.obd_soc", "50")  # reported before the portal measurement at 10:00
    freezer.move_to("2026-10-01T10:30:00Z")
    assert await setup_entry(hass, local_entry)
    best = hass.states.get("sensor.born_battery_level_best")
    assert best.state == "72.0" and best.attributes["source"] == "portal"


@pytest.mark.parametrize("local_state", ["unavailable", "unknown", "n/a"])
async def test_best_soc_ignores_unusable_local(
    hass: HomeAssistant, local_entry: MockConfigEntry, mock_client: MagicMock, local_state: str
) -> None:
    """Battery level (best) ignores an unavailable, unknown or non-numeric local sensor."""
    hass.states.async_set("sensor.obd_soc", local_state)
    assert await setup_entry(hass, local_entry)
    best = hass.states.get("sensor.born_battery_level_best")
    assert best.state == "72.0" and best.attributes["source"] == "portal"


async def test_best_soc_local_only(hass: HomeAssistant, local_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    """Battery level (best) uses the local reading when the portal has no SoC."""
    mock_client.update.side_effect = updater({"mileage": "1"})
    hass.states.async_set("sensor.obd_soc", "33")
    assert await setup_entry(hass, local_entry)
    assert hass.states.get("sensor.born_battery_level_best").state == "33.0"


async def test_best_soc_nothing_available(
    hass: HomeAssistant, local_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Battery level (best) is unknown with no source when neither value exists."""
    mock_client.update.side_effect = lambda vin, state: fill(state, {"mileage": "1"})
    assert await setup_entry(hass, local_entry)
    best = hass.states.get("sensor.born_battery_level_best")
    assert best.state == "unknown" and best.attributes["source"] is None


def test_now_is_timezone_aware() -> None:
    # guards the comparison between HA's last_reported and portal timestamps
    """Timestamps are timezone-aware, so local and portal readings compare correctly."""
    assert dt_util.utcnow().tzinfo is not None
    assert parse_time("2026-10-01T10:00:00Z") - parse_time("2026-10-01T09:00:00") == timedelta(hours=1)
