"""Config, reauth and options flows."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cupra_dataact.api import AuthError, ConsentRequired, DataActError
from custom_components.cupra_dataact.const import CONF_LOCAL_SOC, CONF_SCAN_INTERVAL, DOMAIN

from .conftest import DATA, VIN2, setup_entry


async def start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return result


async def test_user_flow_creates_entry(hass: HomeAssistant, flow_client: MagicMock, mock_client: MagicMock) -> None:
    flow_client.vehicles.return_value = [
        {"vin": "V1", "name": "Born", "model": None},
        {"vin": VIN2, "name": "Formentor", "model": None},
    ]
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "me@example.com (Born, Formentor)"
    assert result["data"] == DATA
    assert result["result"].unique_id == "cupra:me@example.com"
    flow_client.close.assert_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthError("bad"), "invalid_auth"),
        (ConsentRequired("terms"), "consent_required"),
        (DataActError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, flow_client: MagicMock, error: Exception, expected: str) -> None:
    flow_client.login.side_effect = error
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    flow_client.close.assert_awaited()


async def test_user_flow_no_vehicles(hass: HomeAssistant, flow_client: MagicMock) -> None:
    flow_client.vehicles.return_value = []
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "no_vehicles"}


async def test_user_flow_recovers_after_error(
    hass: HomeAssistant, flow_client: MagicMock, mock_client: MagicMock
) -> None:
    flow_client.login.side_effect = AuthError("bad")
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "invalid_auth"}

    flow_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_duplicate_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry, flow_client: MagicMock
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**DATA, "username": "ME@example.com"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    flow_client.login.assert_not_awaited()


async def test_reauth_updates_password(
    hass: HomeAssistant, config_entry: MockConfigEntry, flow_client: MagicMock, mock_client: MagicMock
) -> None:
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new"


async def test_reauth_rejects_wrong_password(
    hass: HomeAssistant, config_entry: MockConfigEntry, flow_client: MagicMock
) -> None:
    flow_client.login.side_effect = AuthError("bad")
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_PASSWORD] == "pw"


async def test_options_flow(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    assert await setup_entry(hass, config_entry)
    hass.states.async_set("sensor.obd_soc", "50", {"device_class": "battery"})

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 10, CONF_LOCAL_SOC: "sensor.obd_soc"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_SCAN_INTERVAL: 10, CONF_LOCAL_SOC: "sensor.obd_soc"}
    assert hass.states.get("sensor.born_battery_level_best") is not None
