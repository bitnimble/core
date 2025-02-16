"""Config flow for Shelly integration."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from aioshelly.block_device import BlockDevice
from aioshelly.common import ConnectionOptions, get_info
from aioshelly.const import BLOCK_GENERATIONS, RPC_GENERATIONS
from aioshelly.exceptions import (
    DeviceConnectionError,
    FirmwareUnsupported,
    InvalidAuthError,
)
from aioshelly.rpc_device import RpcDevice
import voluptuous as vol

from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
    CONF_BLE_SCANNER_MODE,
    CONF_GEN,
    CONF_SLEEP_PERIOD,
    DOMAIN,
    LOGGER,
    MODEL_WALL_DISPLAY,
    BLEScannerMode,
)
from .coordinator import async_reconnect_soon
from .utils import (
    get_block_device_sleep_period,
    get_coap_context,
    get_device_entry_gen,
    get_info_auth,
    get_info_gen,
    get_model_name,
    get_rpc_device_wakeup_period,
    get_ws_context,
    mac_address_from_name,
)

HOST_SCHEMA: Final = vol.Schema({vol.Required(CONF_HOST): str})


BLE_SCANNER_OPTIONS = [
    BLEScannerMode.DISABLED,
    BLEScannerMode.ACTIVE,
    BLEScannerMode.PASSIVE,
]

INTERNAL_WIFI_AP_IP = "192.168.33.1"


async def validate_input(
    hass: HomeAssistant,
    host: str,
    info: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from HOST_SCHEMA with values provided by the user.
    """
    options = ConnectionOptions(host, data.get(CONF_USERNAME), data.get(CONF_PASSWORD))

    gen = get_info_gen(info)

    if gen in RPC_GENERATIONS:
        ws_context = await get_ws_context(hass)
        rpc_device = await RpcDevice.create(
            async_get_clientsession(hass),
            ws_context,
            options,
        )
        await rpc_device.shutdown()

        sleep_period = get_rpc_device_wakeup_period(rpc_device.status)

        return {
            "title": rpc_device.name,
            CONF_SLEEP_PERIOD: sleep_period,
            "model": rpc_device.shelly.get("model"),
            CONF_GEN: gen,
        }

    # Gen1
    coap_context = await get_coap_context(hass)
    block_device = await BlockDevice.create(
        async_get_clientsession(hass),
        coap_context,
        options,
    )
    block_device.shutdown()
    return {
        "title": block_device.name,
        CONF_SLEEP_PERIOD: get_block_device_sleep_period(block_device.settings),
        "model": block_device.model,
        CONF_GEN: gen,
    }


class ShellyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Shelly."""

    VERSION = 1

    host: str = ""
    info: dict[str, Any] = {}
    device_info: dict[str, Any] = {}
    entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host: str = user_input[CONF_HOST]
            try:
                self.info = await self._async_get_info(host)
            except DeviceConnectionError:
                errors["base"] = "cannot_connect"
            except FirmwareUnsupported:
                return self.async_abort(reason="unsupported_firmware")
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(self.info["mac"])
                self._abort_if_unique_id_configured({CONF_HOST: host})
                self.host = host
                if get_info_auth(self.info):
                    return await self.async_step_credentials()

                try:
                    device_info = await validate_input(
                        self.hass, self.host, self.info, {}
                    )
                except DeviceConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
                else:
                    if device_info["model"]:
                        return self.async_create_entry(
                            title=device_info["title"],
                            data={
                                **user_input,
                                CONF_SLEEP_PERIOD: device_info[CONF_SLEEP_PERIOD],
                                "model": device_info["model"],
                                CONF_GEN: device_info[CONF_GEN],
                            },
                        )
                    errors["base"] = "firmware_not_fully_provisioned"

        return self.async_show_form(
            step_id="user", data_schema=HOST_SCHEMA, errors=errors
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the credentials step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if get_info_gen(self.info) in RPC_GENERATIONS:
                user_input[CONF_USERNAME] = "admin"
            try:
                device_info = await validate_input(
                    self.hass, self.host, self.info, user_input
                )
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except DeviceConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if device_info["model"]:
                    return self.async_create_entry(
                        title=device_info["title"],
                        data={
                            **user_input,
                            CONF_HOST: self.host,
                            CONF_SLEEP_PERIOD: device_info[CONF_SLEEP_PERIOD],
                            "model": device_info["model"],
                            CONF_GEN: device_info[CONF_GEN],
                        },
                    )
                errors["base"] = "firmware_not_fully_provisioned"
        else:
            user_input = {}

        if get_info_gen(self.info) in RPC_GENERATIONS:
            schema = {
                vol.Required(CONF_PASSWORD, default=user_input.get(CONF_PASSWORD)): str,
            }
        else:
            schema = {
                vol.Required(CONF_USERNAME, default=user_input.get(CONF_USERNAME)): str,
                vol.Required(CONF_PASSWORD, default=user_input.get(CONF_PASSWORD)): str,
            }

        return self.async_show_form(
            step_id="credentials", data_schema=vol.Schema(schema), errors=errors
        )

    async def _async_discovered_mac(self, mac: str, host: str) -> None:
        """Abort and reconnect soon if the device with the mac address is already configured."""
        if (
            current_entry := await self.async_set_unique_id(mac)
        ) and current_entry.data.get(CONF_HOST) == host:
            await async_reconnect_soon(self.hass, current_entry)
        if host == INTERNAL_WIFI_AP_IP:
            # If the device is broadcasting the internal wifi ap ip
            # we can't connect to it, so we should not update the
            # entry with the new host as it will be unreachable
            #
            # This is a workaround for a bug in the firmware 0.12 (and older?)
            # which should be removed once the firmware is fixed
            # and the old version is no longer in use
            self._abort_if_unique_id_configured()
        else:
            self._abort_if_unique_id_configured({CONF_HOST: host})

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle zeroconf discovery."""
        host = discovery_info.host
        # First try to get the mac address from the name
        # so we can avoid making another connection to the
        # device if we already have it configured
        if mac := mac_address_from_name(discovery_info.name):
            await self._async_discovered_mac(mac, host)

        try:
            self.info = await self._async_get_info(host)
        except DeviceConnectionError:
            return self.async_abort(reason="cannot_connect")
        except FirmwareUnsupported:
            return self.async_abort(reason="unsupported_firmware")

        if not mac:
            # We could not get the mac address from the name
            # so need to check here since we just got the info
            await self._async_discovered_mac(self.info["mac"], host)

        self.host = host
        self.context.update(
            {
                "title_placeholders": {"name": discovery_info.name.split(".")[0]},
                "configuration_url": f"http://{discovery_info.host}",
            }
        )

        if get_info_auth(self.info):
            return await self.async_step_credentials()

        try:
            self.device_info = await validate_input(self.hass, self.host, self.info, {})
        except DeviceConnectionError:
            return self.async_abort(reason="cannot_connect")

        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle discovery confirm."""
        errors: dict[str, str] = {}

        if not self.device_info["model"]:
            errors["base"] = "firmware_not_fully_provisioned"
            model = "Shelly"
        else:
            model = get_model_name(self.info)
            if user_input is not None:
                return self.async_create_entry(
                    title=self.device_info["title"],
                    data={
                        "host": self.host,
                        CONF_SLEEP_PERIOD: self.device_info[CONF_SLEEP_PERIOD],
                        "model": self.device_info["model"],
                        CONF_GEN: self.device_info[CONF_GEN],
                    },
                )
            self._set_confirm_only()

        return self.async_show_form(
            step_id="confirm_discovery",
            description_placeholders={
                "model": model,
                "host": self.host,
            },
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Handle configuration by re-auth."""
        self.entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that reauth is required."""
        errors: dict[str, str] = {}
        assert self.entry is not None
        host = self.entry.data[CONF_HOST]

        if user_input is not None:
            try:
                info = await self._async_get_info(host)
            except (DeviceConnectionError, InvalidAuthError, FirmwareUnsupported):
                return self.async_abort(reason="reauth_unsuccessful")

            if get_device_entry_gen(self.entry) != 1:
                user_input[CONF_USERNAME] = "admin"
            try:
                await validate_input(self.hass, host, info, user_input)
            except (DeviceConnectionError, InvalidAuthError, FirmwareUnsupported):
                return self.async_abort(reason="reauth_unsuccessful")

            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, **user_input}
            )
            await self.hass.config_entries.async_reload(self.entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        if get_device_entry_gen(self.entry) in BLOCK_GENERATIONS:
            schema = {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        else:
            schema = {vol.Required(CONF_PASSWORD): str}

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def _async_get_info(self, host: str) -> dict[str, Any]:
        """Get info from shelly device."""
        return await get_info(async_get_clientsession(self.hass), host)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Return options flow support for this handler."""
        return (
            get_device_entry_gen(config_entry) in RPC_GENERATIONS
            and not config_entry.data.get(CONF_SLEEP_PERIOD)
            and config_entry.data.get("model") != MODEL_WALL_DISPLAY
        )


class OptionsFlowHandler(OptionsFlow):
    """Handle the option flow for shelly."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BLE_SCANNER_MODE,
                        default=self.config_entry.options.get(
                            CONF_BLE_SCANNER_MODE, BLEScannerMode.DISABLED
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=BLE_SCANNER_OPTIONS,
                            translation_key=CONF_BLE_SCANNER_MODE,
                        ),
                    ),
                }
            ),
        )
