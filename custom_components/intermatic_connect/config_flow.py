"""Config flow for Intermatic Connect."""

from __future__ import annotations


from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IntermaticApi, IntermaticAuthError, IntermaticConnectionError
from .const import CONF_REFRESH_TOKEN, CONF_REFRESH_USERNAME, DOMAIN



class IntermaticConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip().lower()
            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()
            api = IntermaticApi(
                async_get_clientsession(self.hass),
                self.hass.async_add_executor_job,
                username,
            )
            try:
                await api.authenticate(user_input[CONF_PASSWORD])
                things = await api.get_things()
            except IntermaticAuthError:
                errors["base"] = "invalid_auth"
            except IntermaticConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Intermatic Connect ({len(things)} timer{'s' if len(things) != 1 else ''})",
                    data={
                        CONF_USERNAME: username,
                        CONF_REFRESH_TOKEN: api.refresh_token,
                        CONF_REFRESH_USERNAME: api.refresh_username,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )
