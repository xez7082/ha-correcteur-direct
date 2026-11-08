"""Capteurs du Correcteur en direct."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NOM, SIGNAL_MAJ, VERSION
from .engine import Moteur


async def async_setup_entry(
    hass: HomeAssistant, entree, ajouter: AddEntitiesCallback
) -> None:
    moteur: Moteur = entree.runtime_data
    ajouter(
        [
            CapteurProblemes(moteur),
            CapteurCorrections(moteur),
            CapteurDerniere(moteur),
        ]
    )


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, moteur: Moteur, cle: str, nom: str, icone: str) -> None:
        self.moteur = moteur
        self._attr_unique_id = f"{DOMAIN}_{cle}"
        self._attr_name = nom
        self._attr_icon = icone
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "moteur")},
            name=NOM,
            manufacturer="xez7082",
            model="Mistral AI",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_MAJ, self._rafraichir)
        )

    @callback
    def _rafraichir(self) -> None:
        self.async_write_ha_state()


class CapteurProblemes(_Base):
    _attr_native_unit_of_measurement = "problèmes"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, moteur: Moteur) -> None:
        super().__init__(moteur, "problemes", "Problèmes détectés", "mdi:alert-circle-outline")

    @property
    def native_value(self) -> int:
        return len(self.moteur.ouverts())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ouverts = sorted(self.moteur.ouverts(), key=lambda p: p["dernier"], reverse=True)
        return {
            "critiques": sum(
                1 for p in ouverts if (p.get("analyse") or {}).get("gravite") == "critique"
            ),
            "liste": [
                {
                    "id": p["id"],
                    "resume": (p.get("analyse") or {}).get("resume") or p["message"][:100],
                    "gravite": (p.get("analyse") or {}).get("gravite"),
                    "statut": p["statut"],
                    "occurrences": p["occurrences"],
                }
                for p in ouverts[:10]
            ],
        }


class CapteurCorrections(_Base):
    _attr_native_unit_of_measurement = "corrections"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, moteur: Moteur) -> None:
        super().__init__(moteur, "corrections", "Corrections appliquées", "mdi:auto-fix")

    @property
    def native_value(self) -> int:
        return int(self.moteur.stats.get("corrections", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "echecs": self.moteur.stats.get("echecs", 0),
            "appels_ia": self.moteur.stats.get("appels_ia", 0),
            "erreurs_vues": self.moteur.stats.get("erreurs_vues", 0),
        }


class CapteurDerniere(_Base):
    def __init__(self, moteur: Moteur) -> None:
        super().__init__(moteur, "derniere", "Dernière erreur", "mdi:message-alert-outline")

    def _derniere(self) -> dict[str, Any] | None:
        if not self.moteur.problemes:
            return None
        return max(self.moteur.problemes.values(), key=lambda p: p["dernier"])

    @property
    def native_value(self) -> str:
        pb = self._derniere()
        if not pb:
            return "Aucune"
        return ((pb.get("analyse") or {}).get("resume") or pb["message"])[:250]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pb = self._derniere()
        if not pb:
            return {}
        a = pb.get("analyse") or {}
        return {
            "id": pb["id"],
            "statut": pb["statut"],
            "gravite": a.get("gravite"),
            "integration": pb["nom"],
            "cause": a.get("cause"),
            "correctif": a.get("explication"),
            "vue_le": pb["dernier"],
        }
