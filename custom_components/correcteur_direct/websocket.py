"""Commande WebSocket utilisée par le panneau latéral."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/liste"})
@websocket_api.require_admin
@callback
def ws_liste(hass: HomeAssistant, connexion: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    moteur = hass.data.get(DOMAIN, {}).get("moteur")
    if moteur is None:
        connexion.send_error(msg["id"], "non_charge", "Intégration non chargée")
        return
    connexion.send_result(msg["id"], moteur.exporter())


@callback
def async_enregistrer_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_liste)
