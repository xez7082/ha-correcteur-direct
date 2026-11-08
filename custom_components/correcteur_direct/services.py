"""Services du Correcteur en direct."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .fixer import CorrectionErreur

SCHEMA_ID = vol.Schema({vol.Required("id"): str})


def _moteur(hass: HomeAssistant):
    moteur = hass.data.get(DOMAIN, {}).get("moteur")
    if moteur is None:
        raise HomeAssistantError("Le Correcteur en direct n'est pas chargé")
    return moteur


def async_enregistrer_services(hass: HomeAssistant) -> None:
    async def analyser_maintenant(call: ServiceCall) -> ServiceResponse:
        nouveaux = await _moteur(hass).balayer()
        return {"nouveaux_problemes": nouveaux}

    async def appliquer(call: ServiceCall) -> None:
        if not await _moteur(hass).appliquer(call.data["id"]):
            raise HomeAssistantError("Correctif non appliqué (voir le panneau Correcteur)")

    async def ignorer(call: ServiceCall) -> None:
        _moteur(hass).ignorer(call.data["id"])

    async def reactiver(call: ServiceCall) -> None:
        _moteur(hass).reactiver(call.data["id"])

    async def reanalyser(call: ServiceCall) -> None:
        _moteur(hass).reanalyser(call.data["id"])

    async def annuler(call: ServiceCall) -> None:
        try:
            ok = await _moteur(hass).annuler(call.data["id"])
        except CorrectionErreur as err:
            raise HomeAssistantError(str(err)) from err
        if not ok:
            raise HomeAssistantError("Rien à annuler pour ce problème")

    async def corriger_yaml(call: ServiceCall) -> ServiceResponse:
        return await _moteur(hass).corriger_yaml(appliquer=call.data.get("appliquer", True))

    async def verifier(call: ServiceCall) -> None:
        if not _moteur(hass).verifier(call.data["id"]):
            raise HomeAssistantError("Ce problème ne peut pas être vérifié maintenant")

    async def tout_verifier(call: ServiceCall) -> ServiceResponse:
        return {"en_verification": _moteur(hass).tout_verifier()}

    async def vider(call: ServiceCall) -> None:
        _moteur(hass).vider()

    async def tester(call: ServiceCall) -> None:
        logging.getLogger("correcteur_direct_test").error(
            "Test du Correcteur en direct : le capteur sensor.capteur_test_inexistant "
            "n'existe pas (erreur simulée, aucune correction attendue)"
        )

    hass.services.async_register(
        DOMAIN, "analyser_maintenant", analyser_maintenant,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "appliquer_correctif", appliquer, SCHEMA_ID)
    hass.services.async_register(DOMAIN, "ignorer", ignorer, SCHEMA_ID)
    hass.services.async_register(DOMAIN, "reactiver", reactiver, SCHEMA_ID)
    hass.services.async_register(DOMAIN, "reanalyser", reanalyser, SCHEMA_ID)
    hass.services.async_register(DOMAIN, "annuler_correctif", annuler, SCHEMA_ID)
    hass.services.async_register(DOMAIN, "vider_historique", vider)
    hass.services.async_register(DOMAIN, "verifier", verifier, SCHEMA_ID)
    hass.services.async_register(
        DOMAIN, "tout_verifier", tout_verifier, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "corriger_yaml", corriger_yaml,
        vol.Schema({vol.Optional("appliquer", default=True): bool}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "tester", tester)
