"""Collecte du contexte d'une erreur : entités citées et extraits YAML concernés."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOSSIERS_EXCLUS, FICHIERS_EXCLUS

RE_ENTITE = re.compile(r"\b([a-z][a-z0-9_]*)\.([a-z0-9][a-z0-9_]*)\b")
RE_TEMPLATE = re.compile(r"(?:rendering|template|Template)\s*'(.{6,600}?)'(?:\s|$|:|,)", re.S)
RE_GUILLEMETS = re.compile(r"['\"]([^'\"\n]{6,80})['\"]")
# Chemins cités par Home Assistant : « /config/packages/x.yaml, line 12 »
RE_CHEMIN = re.compile(
    r"(/(?:config|homeassistant)/[^\s,'\"()\]]+\.ya?ml)(?:\"?,?\s*(?:line|ligne)\s*(\d+))?",
    re.I,
)
RE_LOGGER = re.compile(r"^(?:homeassistant\.components|custom_components)\.([a-z0-9_]+)")

# Dossiers lus seulement s'ils sont cités par l'erreur elle-même
DOSSIERS_IGNORES_RECHERCHE = {"blueprints", "archive", "archives", "old", "backup"}
MOTS_COPIES = ("sauvegarde", "backup", ".bak", "_old", ".old", "copie", "copy")

TAILLE_MAX_FICHIER = 600_000
NB_FICHIERS_MAX = 800
LIGNES_AUTOUR = 12
EXTRAITS_MAX = 4


def integration_du_logger(nom: str) -> str | None:
    """homeassistant.components.template.sensor -> template."""
    m = RE_LOGGER.match(nom or "")
    return m.group(1) if m else None


def _termes(hass: HomeAssistant, probleme: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Retourne (entités citées, termes à chercher dans les YAML)."""
    texte = f"{probleme.get('message', '')}\n{probleme.get('exception') or ''}"
    entites: list[str] = []
    for m in RE_ENTITE.finditer(texte):
        eid = m.group(0)
        if eid not in entites and hass.states.get(eid) is not None:
            entites.append(eid)
        if len(entites) >= 5:
            break

    termes: list[str] = []
    for eid in entites:
        termes.append(eid)
        etat = hass.states.get(eid)
        if etat is None:
            continue
        # Les automatisations sont repérées par leur id ou leur alias dans le YAML
        if eid.startswith(("automation.", "script.")):
            ident = etat.attributes.get("id")
            if ident:
                termes.append(str(ident))
            nom = etat.attributes.get("friendly_name")
            if nom:
                termes.append(str(nom))
        # object_id seul, utile pour les capteurs template (clé du sensor)
        termes.append(eid.split(".", 1)[1])

    for m in RE_TEMPLATE.finditer(texte):
        modele = m.group(1)
        lignes = [l.strip() for l in modele.splitlines() if len(l.strip()) >= 8]
        if lignes:
            termes.append(max(lignes, key=len)[:70])
    for m in RE_GUILLEMETS.finditer(probleme.get("message", "")):
        val = m.group(1).strip()
        # un mot isolé (« template », « sensor ») ferait remonter des fichiers sans rapport
        if re.fullmatch(r"[a-z0-9_]+", val):
            continue
        if val and not val.startswith(("/", "http")) and len(termes) < 12:
            termes.append(val)

    vus: set[str] = set()
    uniques = []
    for t in termes:
        if t and t not in vus and len(t) >= 4:
            vus.add(t)
            uniques.append(t)
    return entites, uniques[:12]


def _fichiers_yaml(racine: Path) -> list[Path]:
    trouves: list[Path] = []
    for dossier, sous_dossiers, fichiers in os.walk(racine):
        sous_dossiers[:] = [
            d for d in sous_dossiers
            if d not in DOSSIERS_EXCLUS and d not in DOSSIERS_IGNORES_RECHERCHE
            and not d.startswith((".", "_"))
        ]
        for nom in fichiers:
            bas = nom.lower()
            if any(mot in bas for mot in MOTS_COPIES):
                continue  # copies de sauvegarde : jamais la source de l'erreur
            if nom.endswith((".yaml", ".yml")) and nom not in FICHIERS_EXCLUS:
                trouves.append(Path(dossier) / nom)
                if len(trouves) >= NB_FICHIERS_MAX:
                    return trouves
    return trouves


def _chercher(racine: Path, termes: list[str]) -> list[dict[str, Any]]:
    """Cherche les termes dans les YAML et renvoie des extraits avec leurs lignes."""
    if not termes:
        return []
    extraits: list[dict[str, Any]] = []
    deja: set[tuple[str, int]] = set()
    contenus: dict[Path, str] = {}
    for chemin in _fichiers_yaml(racine):
        try:
            if chemin.stat().st_size <= TAILLE_MAX_FICHIER:
                contenus[chemin] = chemin.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    for terme in termes:  # les termes les plus précis d'abord
        for chemin, texte in contenus.items():
            if terme not in texte:
                continue
            lignes = texte.splitlines()
            for i, ligne in enumerate(lignes):
                if terme not in ligne:
                    continue
                rel = str(chemin.relative_to(racine))
                if any(f == rel and abs(l - i) <= LIGNES_AUTOUR for f, l in deja):
                    continue
                deja.add((rel, i))
                debut = max(0, i - LIGNES_AUTOUR)
                fin = min(len(lignes), i + LIGNES_AUTOUR + 1)
                extraits.append(
                    {
                        "fichier": rel,
                        "ligne": i + 1,
                        "debut": debut + 1,
                        "fin": fin,
                        "texte": "\n".join(lignes[debut:fin])[:3000],
                    }
                )
                break  # une occurrence par fichier et par terme
            if len(extraits) >= EXTRAITS_MAX:
                return extraits
    return extraits


async def collecter_contexte(hass: HomeAssistant, probleme: dict[str, Any]) -> dict[str, Any]:
    """Rassemble ce que Mistral doit savoir sur l'erreur (non masqué)."""
    entites, termes = _termes(hass, probleme)
    racine = Path(hass.config.config_dir)
    extraits = await hass.async_add_executor_job(_chercher, racine, termes)

    etats = []
    for eid in entites:
        etat = hass.states.get(eid)
        if etat is None:
            continue
        attrs = {
            k: v
            for k, v in etat.attributes.items()
            if k in ("friendly_name", "unit_of_measurement", "device_class", "state_class", "id")
        }
        etats.append({"entite": eid, "etat": etat.state, "attributs": attrs})

    integration = integration_du_logger(probleme.get("nom", ""))
    entrees = []
    if integration:
        for entree in hass.config_entries.async_entries(integration):
            entrees.append({"titre": entree.title, "etat": str(entree.state.value)})

    # Fichiers concernés : d'abord ceux cités par l'erreur, puis ceux trouvés par recherche
    fichiers: list[dict[str, Any]] = []
    texte = f"{probleme.get('message', '')}\n{probleme.get('exception') or ''}"
    for m in RE_CHEMIN.finditer(texte):
        chemin = m.group(1)
        if chemin.startswith("/homeassistant/"):
            chemin = str(racine / chemin.removeprefix("/homeassistant/"))
        ligne = int(m.group(2)) if m.group(2) else None
        if not any(f["chemin"] == chemin for f in fichiers):
            fichiers.append({"chemin": chemin, "ligne": ligne, "origine": "cité par l'erreur"})
    for x in extraits:
        chemin = str(racine / x["fichier"])
        if not any(f["chemin"] == chemin for f in fichiers):
            fichiers.append({"chemin": chemin, "ligne": x["ligne"], "origine": "trouvé par recherche"})

    return {
        "fichiers": fichiers[:6],
        "integration": integration,
        "entrees": entrees,
        "etats": etats,
        "extraits": extraits,
    }
