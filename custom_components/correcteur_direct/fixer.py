"""Application sécurisée des correctifs, avec sauvegarde et retour arrière."""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.check_config import async_check_ha_config_file

from .const import (
    DOSSIER_SAUVEGARDES,
    DOSSIERS_EXCLUS,
    FICHIERS_EXCLUS,
    SAUVEGARDES_MAX,
    SERVICES_SURS,
    TYPES_CORRECTIF,
)
from .masking import contient_masque
from . import yaml_doctor

_LOGGER = logging.getLogger(__name__)


class CorrectionErreur(Exception):
    """Le correctif n'a pas pu être appliqué."""


class Correcteur:
    """Valide et applique un correctif proposé par l'IA."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.racine = Path(hass.config.config_dir).resolve()
        self.sauvegardes = self.racine / DOSSIER_SAUVEGARDES
        self.en_controle = 0  # > 0 pendant nos propres vérifications de configuration

    # ---------- validation ----------

    def chemin_autorise(self, relatif: str, local: bool = False) -> Path:
        """Retourne le chemin absolu si le fichier peut être modifié, sinon lève."""
        if not relatif:
            raise CorrectionErreur("Aucun fichier indiqué")
        chemin = (self.racine / relatif.lstrip("/")).resolve()
        if chemin.parent != self.racine and self.racine not in chemin.parents:
            raise CorrectionErreur("Fichier hors du dossier de configuration")
        if chemin.suffix not in (".yaml", ".yml"):
            raise CorrectionErreur("Seuls les fichiers YAML peuvent être modifiés")
        parties = set(chemin.relative_to(self.racine).parts[:-1])
        if parties & DOSSIERS_EXCLUS or any(p.startswith((".", "_")) for p in parties):
            raise CorrectionErreur("Dossier protégé (archives, sauvegardes…)")
        # Une réparation locale (sans IA) peut toucher known_devices.yaml, jamais secrets.yaml
        if chemin.name == "secrets.yaml" or (chemin.name in FICHIERS_EXCLUS and not local):
            raise CorrectionErreur("Fichier protégé")
        return chemin

    async def valider(self, correctif: dict[str, Any]) -> str | None:
        """Retourne None si le correctif est applicable, sinon la raison du refus."""
        type_ = correctif.get("type")
        if type_ not in TYPES_CORRECTIF or type_ == "aucun":
            return "Aucun correctif applicable"
        if type_ == "recharger_integration":
            if not self.hass.config_entries.async_entries(correctif.get("domaine", "")):
                return f"Intégration « {correctif.get('domaine')} » introuvable"
        elif type_ == "appel_service":
            nom = f"{correctif.get('domaine')}.{correctif.get('service')}"
            if nom not in SERVICES_SURS:
                return f"Service {nom} non autorisé"
            if not self.hass.services.has_service(correctif["domaine"], correctif["service"]):
                return f"Service {nom} indisponible"
        elif type_ == "modif_fichier":
            rech, remp = correctif.get("rechercher", ""), correctif.get("remplacer", "")
            if not rech.strip() or rech == remp:
                return "Modification vide"
            local = correctif.get("local") is True
            if not local and (contient_masque(rech) or contient_masque(remp)):
                return "La modification touche une valeur masquée (mot de passe, IP…)"
            if not local and (len(rech) > 4000 or len(remp) > 6000):
                return "Modification trop importante pour être sûre"
            try:
                chemin = self.chemin_autorise(correctif.get("fichier", ""), local)
            except CorrectionErreur as err:
                return str(err)
            try:
                texte = await self.hass.async_add_executor_job(
                    chemin.read_text, "utf-8"
                )
            except OSError:
                return "Fichier illisible ou introuvable"
            nb = texte.count(rech)
            if nb == 0:
                return "Le texte à remplacer n'existe pas dans le fichier"
            if nb > 1:
                return "Le texte à remplacer apparaît plusieurs fois (ambigu)"
            # Le fichier doit rester (ou redevenir) un YAML valide
            if yaml_doctor.erreur(texte.replace(rech, remp, 1)) is not None:
                return "Après cette modification, le fichier YAML serait invalide"
        return None

    # ---------- application ----------

    async def appliquer(self, correctif: dict[str, Any]) -> dict[str, Any]:
        """Applique le correctif. Retourne les infos de retour arrière."""
        raison = await self.valider(correctif)
        if raison:
            raise CorrectionErreur(raison)
        type_ = correctif["type"]

        if type_ == "recharger_integration":
            for entree in self.hass.config_entries.async_entries(correctif["domaine"]):
                if entree.disabled_by is None:
                    await self.hass.config_entries.async_reload(entree.entry_id)
            return {"type": "rechargement", "domaine": correctif["domaine"]}

        if type_ == "appel_service":
            await self.hass.services.async_call(
                correctif["domaine"],
                correctif["service"],
                correctif.get("donnees") or {},
                blocking=True,
            )
            return {"type": "service"}

        if type_ == "redemarrage":
            await self.hass.services.async_call("homeassistant", "restart", {})
            return {"type": "redemarrage"}

        return await self._modifier_fichier(correctif)

    async def _modifier_fichier(self, correctif: dict[str, Any]) -> dict[str, Any]:
        chemin = self.chemin_autorise(correctif["fichier"], correctif.get("local") is True)
        avant = await self._erreurs_config()

        def _ecrire() -> str:
            texte = chemin.read_text(encoding="utf-8")
            if texte.count(correctif["rechercher"]) != 1:
                raise CorrectionErreur("Le fichier a changé entre-temps")
            self.sauvegardes.mkdir(exist_ok=True)
            horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
            nom_sauv = f"{horodatage}__{str(chemin.relative_to(self.racine)).replace('/', '__')}"
            sauvegarde = self.sauvegardes / nom_sauv
            shutil.copy2(chemin, sauvegarde)
            chemin.write_text(
                texte.replace(correctif["rechercher"], correctif["remplacer"], 1),
                encoding="utf-8",
            )
            # Rotation des sauvegardes
            anciennes = sorted(self.sauvegardes.iterdir(), key=lambda p: p.name)
            for vieux in anciennes[:-SAUVEGARDES_MAX]:
                vieux.unlink(missing_ok=True)
            return str(sauvegarde)

        sauvegarde = await self.hass.async_add_executor_job(_ecrire)
        info = {"type": "fichier", "fichier": str(chemin), "sauvegarde": sauvegarde}

        apres = await self._erreurs_config()
        nouvelles = await self.hass.async_add_executor_job(
            self._vraies_nouvelles, apres - avant, chemin
        )
        if nouvelles:
            await self._restaurer(info)
            raise CorrectionErreur(
                "Configuration invalide après correction, fichier restauré : "
                + next(iter(nouvelles))[:300]
            )

        info["recharge"] = await self._recharger(correctif.get("rechargement", ""))
        return info

    @staticmethod
    def _vraies_nouvelles(nouvelles: set[str], modifie: Path) -> set[str]:
        """Écarte les erreurs qui viennent d'un AUTRE fichier déjà cassé avant.

        Home Assistant s'arrête au premier fichier YAML illisible : réparer l'un
        fait apparaître l'erreur du suivant, qui n'est pas une nouvelle erreur.
        """
        gardees = set()
        for message in nouvelles:
            chemins = {Path(c) for c in re.findall(r'(/[^"\s,]+\.ya?ml)', message)}
            autres = [c for c in chemins if c.resolve() != modifie.resolve()]
            deja_casse = False
            for autre in autres:
                try:
                    if yaml_doctor.erreur(autre.read_text(encoding="utf-8", errors="replace")):
                        deja_casse = True
                except OSError:
                    pass
            if not deja_casse:
                gardees.add(message)
        return gardees

    async def _erreurs_config(self) -> set[str]:
        self.en_controle += 1
        try:
            res = await async_check_ha_config_file(self.hass)
        except Exception as err:  # noqa: BLE001
            return {f"Vérification impossible : {err}"}
        finally:
            self.en_controle -= 1
        return {str(e.message) for e in res.errors}

    async def _recharger(self, domaine: str) -> str:
        """Recharge le domaine concerné, sinon tout le YAML rechargeable."""
        domaine = (domaine or "").strip().split(".")[0]
        if domaine and self.hass.services.has_service(domaine, "reload"):
            await self.hass.services.async_call(domaine, "reload", {}, blocking=True)
            return f"{domaine}.reload"
        if self.hass.services.has_service("homeassistant", "reload_all"):
            await self.hass.services.async_call("homeassistant", "reload_all", {}, blocking=True)
            return "homeassistant.reload_all"
        return "redémarrage conseillé"

    # ---------- retour arrière ----------

    async def _restaurer(self, info: dict[str, Any]) -> None:
        def _copier() -> None:
            shutil.copy2(info["sauvegarde"], info["fichier"])

        await self.hass.async_add_executor_job(_copier)

    async def annuler(self, info: dict[str, Any] | None) -> bool:
        """Annule un correctif de fichier. Retourne False si rien à annuler."""
        if not info or info.get("type") != "fichier":
            return False
        if not Path(info.get("sauvegarde", "")).exists():
            raise CorrectionErreur("Sauvegarde introuvable")
        await self._restaurer(info)
        recharge = info.get("recharge", "")
        try:
            if recharge and recharge != "redémarrage conseillé":
                dom, svc = recharge.split(".", 1)
                await self.hass.services.async_call(dom, svc, {}, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Rechargement après annulation impossible : %s", err)
        return True
