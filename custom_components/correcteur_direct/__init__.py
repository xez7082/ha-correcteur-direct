"""Correcteur en direct : détecte et corrige les erreurs Home Assistant avec Mistral AI."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, NOM, PANEL_URL, STATIC_URL, VERSION
from .engine import Moteur
from .services import async_enregistrer_services
from .websocket import async_enregistrer_websocket

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

type CorrecteurConfigEntry = ConfigEntry[Moteur]


async def async_setup_entry(hass: HomeAssistant, entree: CorrecteurConfigEntry) -> bool:
    donnees = hass.data.setdefault(DOMAIN, {})
    moteur = Moteur(hass, entree)
    await moteur.async_demarrer()
    entree.runtime_data = moteur
    donnees["moteur"] = moteur

    if not donnees.get("statique"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "www"), False)]
        )
        async_enregistrer_websocket(hass)
        async_enregistrer_services(hass)
        donnees["statique"] = True

    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL,
            webcomponent_name="correcteur-direct-panel",
            sidebar_title="Correcteur",
            sidebar_icon="mdi:auto-fix",
            module_url=f"{STATIC_URL}/panel.js?v={VERSION}",
            require_admin=True,
            config={"titre": NOM},
        )
    except ValueError:
        pass  # panneau déjà présent
    except Exception as err:  # noqa: BLE001 - le panneau ne doit jamais bloquer l'intégration
        _LOGGER.warning("Panneau Correcteur non installé : %s", err)

    await hass.config_entries.async_forward_entry_setups(entree, PLATFORMS)
    entree.async_on_unload(entree.add_update_listener(_options_modifiees))
    return True


async def _options_modifiees(hass: HomeAssistant, entree: CorrecteurConfigEntry) -> None:
    await hass.config_entries.async_reload(entree.entry_id)


async def async_unload_entry(hass: HomeAssistant, entree: CorrecteurConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entree, PLATFORMS)
    if ok:
        await entree.runtime_data.async_arreter()
        hass.data.get(DOMAIN, {}).pop("moteur", None)
        frontend.async_remove_panel(hass, PANEL_URL)
    return ok
