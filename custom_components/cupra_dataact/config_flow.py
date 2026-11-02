"""Config flow for the CUPRA EU Data Act integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .api import BRANDS, AuthError, ConsentRequired, DataActClient, DataActError
from .const import (
    CONF_BRAND,
    CONF_DISTANCE_UNIT,
    CONF_LOCAL_SOC,
    CONF_SCAN_INTERVAL,
    DEFAULT_BRAND,
    DEFAULT_DISTANCE_UNIT,
    DEFAULT_SCAN_INTERVAL,
    DISTANCE_SENSOR_SUFFIXES,
    DISTANCE_UNITS,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
    PORTAL_URL,
)

_LOGGER = logging.getLogger(__name__)


async def _validate(data: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Log in and list vehicles; returns vehicles and form errors."""
    client = DataActClient(data[CONF_USERNAME], data[CONF_PASSWORD], data.get(CONF_BRAND, DEFAULT_BRAND))
    try:
        await client.login()
        vehicles = await client.vehicles()
    except AuthError:
        return [], {"base": "invalid_auth"}
    except ConsentRequired:
        return [], {"base": "consent_required"}
    except DataActError as err:
        _LOGGER.warning("Portal login failed: %s", err)
        return [], {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error during portal login")
        return [], {"base": "unknown"}
    finally:
        await client.close()
    if not vehicles:
        return [], {"base": "no_vehicles"}
    return vehicles, {}


class DataActConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(f"{user_input[CONF_BRAND]}:{user_input[CONF_USERNAME].lower()}")
            self._abort_if_unique_id_configured()
            vehicles, errors = await _validate(user_input)
            if not errors:
                names = ", ".join(v["name"] for v in vehicles)
                return self.async_create_entry(title=f"{user_input[CONF_USERNAME]} ({names})", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_BRAND, default=DEFAULT_BRAND): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(BRANDS), translation_key="brand")
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"url": PORTAL_URL},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            _, errors = await _validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME], "url": PORTAL_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DataActOptionsFlow()


class DataActOptionsFlow(OptionsFlow):
    """Poll interval, distance unit and an optional live SoC source (e.g. an OBD dongle) to blend in."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            unit = user_input.get(CONF_DISTANCE_UNIT, DEFAULT_DISTANCE_UNIT)
            if unit != self.config_entry.options.get(CONF_DISTANCE_UNIT, DEFAULT_DISTANCE_UNIT):
                self._set_distance_display_unit(unit)
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=int(DEFAULT_SCAN_INTERVAL.total_seconds() // 60)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES, max=60, step=1, unit_of_measurement="min"
                    )
                ),
                vol.Required(CONF_DISTANCE_UNIT, default=DEFAULT_DISTANCE_UNIT): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(DISTANCE_UNITS), translation_key="distance_unit")
                ),
                vol.Optional(CONF_LOCAL_SOC): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="battery")
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )

    def _set_distance_display_unit(self, unit: str) -> None:
        """Show existing distance sensors in the chosen unit.

        Home Assistant keeps the old display unit when the native unit changes, which would convert
        the newly labelled miles back to km. A user choice made later in the entity settings still wins.
        """
        registry = er.async_get(self.hass)
        for entity in er.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            if entity.domain == "sensor" and entity.unique_id.endswith(DISTANCE_SENSOR_SUFFIXES):
                options = {**entity.options.get("sensor", {}), "unit_of_measurement": unit}
                registry.async_update_entity_options(entity.entity_id, "sensor", options)
