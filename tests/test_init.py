"""Home Assistant setup tests with the portal client mocked out."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cupra_dataact.api import AuthError, NotConfigured, Point, parse_time
from custom_components.cupra_dataact.const import CONF_BRAND, CONF_LOCAL_SOC, DOMAIN

VIN = "VSSZZZK1ZRP000001"
DATA = {CONF_USERNAME: "me@example.com", CONF_PASSWORD: "pw", CONF_BRAND: "cupra"}
CLIENT = "custom_components.cupra_dataact.DataActClient"


def _points(state, values):
    state.merge([Point("", k, v, parse_time("2026-10-01T10:00:00Z")) for k, v in values.items()])
    state.newest_listed = parse_time("2026-10-01T10:15:00Z")
    return 1


def mock_client(mock, update=None):
    client = mock.return_value
    client.login = AsyncMock()
    client.close = AsyncMock()
    client.vehicles = AsyncMock(return_value=[{"vin": VIN, "name": "Born", "model": "Born"}])
    client.update = AsyncMock(
        side_effect=update
        or (
            lambda vin, state: _points(
                state,
                {"state_of_charge": "72", "mileage": "12345", "plug_state": "connected", "charging_state": "charging"},
            )
        )
    )
    return client


async def test_setup_creates_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="cupra:me@example.com")
    entry.add_to_hass(hass)
    with patch(CLIENT) as mock:
        mock_client(mock)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.born_battery_level").state == "72.0"
    assert hass.states.get("sensor.born_battery_level").attributes["field"] == "state_of_charge"
    assert hass.states.get("sensor.born_odometer").state == "12345.0"
    assert hass.states.get("binary_sensor.born_plugged_in").state == "on"
    assert hass.states.get("binary_sensor.born_charging").state == "on"
    assert hass.states.get("sensor.born_range").state == "unknown"
    assert hass.states.get("sensor.born_last_portal_delivery").state == "2026-10-01T10:15:00+00:00"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_best_soc_prefers_fresher_local(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.obd_soc", "80", {"device_class": "battery"})
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options={CONF_LOCAL_SOC: "sensor.obd_soc"})
    entry.add_to_hass(hass)
    with patch(CLIENT) as mock:
        mock_client(mock)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    best = hass.states.get("sensor.born_battery_level_best")
    assert best.state == "80.0" and best.attributes["source"] == "sensor.obd_soc"
    hass.states.async_set("sensor.obd_soc", "81")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.born_battery_level_best").state == "81.0"


async def test_not_configured_raises_issue(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    with patch(CLIENT) as mock:
        mock_client(mock, update=NotConfigured(VIN))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"not_configured_{VIN}") is not None


async def test_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    with patch(CLIENT) as mock:
        client = mock_client(mock)
        client.login.side_effect = AuthError("bad")
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    with (
        patch("custom_components.cupra_dataact.config_flow.DataActClient") as flow_mock,
        patch(CLIENT) as mock,
    ):
        mock_client(flow_mock)
        mock_client(mock)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "me@example.com (Born)"


async def test_config_flow_invalid_auth(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.cupra_dataact.config_flow.DataActClient") as flow_mock:
        mock_client(flow_mock).login.side_effect = AuthError("bad")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "invalid_auth"}
