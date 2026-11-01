"""Setup, unload and removal of a config entry."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cupra_dataact.api import AuthError, ConsentRequired, DataActError
from custom_components.cupra_dataact.const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

from .conftest import setup_entry


async def test_setup_and_unload(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    """The entry loads with the default poll interval, and unloading closes the client."""
    assert await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    mock_client.login.assert_awaited_once()
    assert config_entry.runtime_data.update_interval == DEFAULT_SCAN_INTERVAL

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    mock_client.close.assert_awaited_once()


async def test_scan_interval_option(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """The poll interval option is applied."""
    entry = MockConfigEntry(
        domain="cupra_dataact",
        data={"username": "a@b.c", "password": "pw", "brand": "cupra"},
        options={CONF_SCAN_INTERVAL: 12},
    )
    entry.add_to_hass(hass)
    assert await setup_entry(hass, entry)
    assert entry.runtime_data.update_interval == timedelta(minutes=12)


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Bad credentials at startup fail setup, start re-authentication and close the client."""
    mock_client.login.side_effect = AuthError("bad")
    assert not await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    mock_client.close.assert_awaited()


async def test_portal_error_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A portal error at startup schedules a setup retry and closes the client."""
    mock_client.vehicles.side_effect = DataActError("down")
    assert not await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_client.close.assert_awaited()


async def test_consent_required_raises_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A consent page at startup retries setup and raises a repair issue linking to the page."""
    from homeassistant.helpers import issue_registry as ir

    mock_client.login.side_effect = ConsentRequired("terms", "https://identity.vwgroup.io/terms")
    assert not await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    issue = ir.async_get(hass).async_get_issue("cupra_dataact", f"consent_{config_entry.entry_id}")
    assert issue is not None and issue.learn_more_url == "https://identity.vwgroup.io/terms"


async def test_options_change_reloads(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Changing options reloads the entry with the new interval."""
    assert await setup_entry(hass, config_entry)
    hass.config_entries.async_update_entry(config_entry, options={CONF_SCAN_INTERVAL: 7})
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.update_interval == timedelta(minutes=7)
    assert mock_client.login.await_count == 2


async def test_remove_entry_deletes_store(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, hass_storage: dict
) -> None:
    """Removing the entry deletes its saved data."""
    assert await setup_entry(hass, config_entry)
    await config_entry.runtime_data._store.async_save({"x": {}})
    key = f"cupra_dataact.{config_entry.entry_id}"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
