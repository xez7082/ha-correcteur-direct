"""Assistant de configuration du Correcteur en direct."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_API_KEY,
    CONF_AUTO_FILES,
    CONF_AUTO_SERVICES,
    CONF_COOLDOWN,
    CONF_LEVEL,
    CONF_MAX_CALLS,
    CONF_MANUAL_DELAY,
    CONF_MIN_CONFIDENCE,
    CONF_MODEL,
    CONF_NOTIFY,
    CONF_VERIFY_DELAY,
    DEFAUTS,
    DOMAIN,
    MODELES,
    NOM,
)
from .mistral import MistralCleInvalide, MistralErreur, verifier_cle

ENTIERS = (CONF_MAX_CALLS, CONF_VERIFY_DELAY, CONF_COOLDOWN, CONF_MIN_CONFIDENCE, CONF_MANUAL_DELAY)


def _nombre(mini: int, maxi: int, pas: int = 1, unite: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=mini, max=maxi, step=pas, mode=NumberSelectorMode.BOX)
    if unite:  # une unité vide est refusée par Home Assistant
        config["unit_of_measurement"] = unite
    return NumberSelector(config)


def _schema(v: dict[str, Any]) -> vol.Schema:
    v = {**DEFAUTS, **v}
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY, default=v.get(CONF_API_KEY, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_MODEL, default=v[CONF_MODEL]): SelectSelector(
                SelectSelectorConfig(
                    options=MODELES, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(CONF_LEVEL, default=v[CONF_LEVEL]): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value="ERROR", label="Erreurs uniquement"),
                        SelectOptionDict(value="WARNING", label="Erreurs et avertissements"),
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_AUTO_SERVICES, default=v[CONF_AUTO_SERVICES]): BooleanSelector(),
            vol.Required(CONF_AUTO_FILES, default=v[CONF_AUTO_FILES]): BooleanSelector(),
            vol.Required(CONF_MIN_CONFIDENCE, default=v[CONF_MIN_CONFIDENCE]): _nombre(
                50, 100, 5, "%"
            ),
            vol.Optional(
                CONF_NOTIFY, description={"suggested_value": v.get(CONF_NOTIFY) or ""}
            ): TextSelector(),
            vol.Required(CONF_MAX_CALLS, default=v[CONF_MAX_CALLS]): _nombre(1, 300),
            vol.Required(CONF_VERIFY_DELAY, default=v[CONF_VERIFY_DELAY]): _nombre(
                30, 900, 10, "s"
            ),
            vol.Required(CONF_COOLDOWN, default=v[CONF_COOLDOWN]): _nombre(1, 72, 1, "h"),
            vol.Required(CONF_MANUAL_DELAY, default=v[CONF_MANUAL_DELAY]): _nombre(1, 120, 1, "min"),
        }
    )


def _nettoyer(entree: dict[str, Any]) -> dict[str, Any]:
    sortie = dict(entree)
    for cle in ENTIERS:
        if cle in sortie:
            sortie[cle] = int(sortie[cle])
    sortie[CONF_NOTIFY] = str(sortie.get(CONF_NOTIFY) or "").strip().removeprefix("notify.")
    sortie[CONF_API_KEY] = str(sortie.get(CONF_API_KEY, "")).strip()
    return sortie


async def _tester_cle(hass, cle: str) -> str | None:
    try:
        await verifier_cle(async_get_clientsession(hass), cle)
    except MistralCleInvalide:
        return "cle_invalide"
    except MistralErreur:
        return "injoignable"
    return None


class CorrecteurConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        erreurs: dict[str, str] = {}
        if user_input is not None:
            donnees = _nettoyer(user_input)
            if err := await _tester_cle(self.hass, donnees[CONF_API_KEY]):
                erreurs["base"] = err
            else:
                return self.async_create_entry(title=NOM, data=donnees)
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=erreurs
        )

    @staticmethod
    @callback
    def async_get_options_flow(entree: ConfigEntry) -> OptionsFlow:
        return CorrecteurOptionsFlow()


class CorrecteurOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        erreurs: dict[str, str] = {}
        actuel = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            donnees = _nettoyer(user_input)
            if donnees[CONF_API_KEY] != actuel.get(CONF_API_KEY) and (
                err := await _tester_cle(self.hass, donnees[CONF_API_KEY])
            ):
                erreurs["base"] = err
            else:
                return self.async_create_entry(title="", data=donnees)
        return self.async_show_form(
            step_id="init", data_schema=_schema(user_input or actuel), errors=erreurs
        )
