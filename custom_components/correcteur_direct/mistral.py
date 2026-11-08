"""Client Mistral AI : analyse d'une erreur Home Assistant."""
from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

from .const import MISTRAL_CHAT_URL, MISTRAL_MODELS_URL, SERVICES_SURS


class MistralErreur(Exception):
    """Erreur lors de l'appel à Mistral."""


class MistralQuota(MistralErreur):
    """Limite de débit atteinte (HTTP 429)."""

    def __init__(self, message: str, attente: float | None = None) -> None:
        super().__init__(message)
        self.attente = attente


class MistralCleInvalide(MistralErreur):
    """Clé API refusée."""


PROMPT_SYSTEME = f"""Tu es un expert Home Assistant (versions 2024 à 2026). On te donne UNE erreur
du journal Home Assistant, avec le contexte disponible (entités citées, extraits des fichiers YAML
de l'utilisateur). Tu dois trouver la cause et proposer UN correctif minimal et sûr.

Réponds UNIQUEMENT avec un objet JSON, en français, de cette forme exacte :
{{
  "resume": "titre court (max 80 caractères)",
  "gravite": "critique" | "moyenne" | "faible",
  "cause": "cause probable, 1 à 3 phrases",
  "correctif": {{
    "type": "aucun" | "recharger_integration" | "appel_service" | "modif_fichier" | "redemarrage",
    "domaine": "domaine de l'intégration (pour recharger_integration ou appel_service)",
    "service": "nom du service (pour appel_service)",
    "donnees": {{}},
    "fichier": "chemin relatif EXACT tel que fourni dans les extraits (pour modif_fichier)",
    "rechercher": "texte EXACT copié de l'extrait, sur une ou plusieurs lignes complètes",
    "remplacer": "nouveau texte qui remplace exactement 'rechercher'",
    "rechargement": "domaine à recharger après modif_fichier (ex: template, automation, script) ou vide"
  }},
  "confiance": nombre entre 0 et 1,
  "explication": "ce que fait le correctif, ou ce que l'utilisateur doit faire lui-même si type=aucun"
}}

Règles impératives :
- Préfère toujours le correctif le plus petit. Ne réécris jamais un fichier entier.
- modif_fichier : "rechercher" doit être copié caractère pour caractère depuis un extrait fourni,
  indentation comprise, et n'apparaître qu'une fois dans le fichier. Garde l'indentation YAML.
  N'utilise jamais un texte contenant le caractère ‹ (valeur masquée).
- Pour un modèle (template) qui plante sur unknown/unavailable/None, ajoute une garde
  (ex: `| float(0)`, `| int(0)`, `is_number`, `has_value`, ou `availability:`).
- appel_service : uniquement l'un de ces services : {", ".join(sorted(SERVICES_SURS))}.
- recharger_integration : pour une intégration configurée par l'interface qui a perdu la connexion.
- redemarrage : seulement si rien d'autre ne peut marcher.
- Si l'erreur vient d'un appareil hors ligne, d'un service internet en panne, d'une intégration
  tierce boguée, ou d'une configuration stockée dans l'interface (pas dans les extraits), mets
  type "aucun" et explique ce que l'utilisateur doit faire.
- Ne propose jamais de supprimer une entité, une intégration ou un fichier.
- "confiance" reflète honnêtement ta certitude que le correctif fera disparaître l'erreur.
"""


def _extraire_json(contenu: str) -> dict[str, Any]:
    try:
        return json.loads(contenu)
    except json.JSONDecodeError:
        trouve = re.search(r"\{.*\}", contenu, re.S)
        if not trouve:
            raise MistralErreur("Réponse de Mistral illisible") from None
        try:
            return json.loads(trouve.group(0))
        except json.JSONDecodeError as err:
            raise MistralErreur("Réponse de Mistral illisible") from err


def normaliser(res: dict[str, Any]) -> dict[str, Any]:
    """Nettoie la réponse pour éviter les valeurs inattendues."""
    gravite = str(res.get("gravite", "moyenne")).lower()
    if gravite not in ("critique", "moyenne", "faible"):
        gravite = "moyenne"
    try:
        confiance = float(res.get("confiance", 0))
    except (TypeError, ValueError):
        confiance = 0.0
    if confiance > 1:
        confiance = confiance / 100
    correctif = res.get("correctif") or {}
    if not isinstance(correctif, dict):
        correctif = {}
    donnees = correctif.get("donnees")
    correctif["donnees"] = donnees if isinstance(donnees, dict) else {}
    correctif["type"] = str(correctif.get("type") or "aucun")
    correctif.pop("local", None)
    correctif.pop("actions", None)
    for cle in ("domaine", "service", "fichier", "rechercher", "remplacer", "rechargement"):
        val = correctif.get(cle)
        correctif[cle] = "" if val is None else str(val)
    return {
        "resume": str(res.get("resume") or "Erreur Home Assistant")[:120],
        "gravite": gravite,
        "cause": str(res.get("cause") or "")[:1500],
        "correctif": correctif,
        "confiance": max(0.0, min(1.0, confiance)),
        "explication": str(res.get("explication") or "")[:2000],
    }


async def analyser(
    session: aiohttp.ClientSession, cle: str, modele: str, texte: str
) -> dict[str, Any]:
    """Envoie l'erreur (déjà masquée) à Mistral et retourne l'analyse normalisée."""
    charge = {
        "model": modele,
        "temperature": 0.1,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT_SYSTEME},
            {"role": "user", "content": texte},
        ],
    }
    try:
        async with session.post(
            MISTRAL_CHAT_URL,
            json=charge,
            headers={"Authorization": f"Bearer {cle}", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=90),
        ) as rep:
            if rep.status == 429:
                attente = None
                try:
                    attente = float(rep.headers.get("Retry-After", ""))
                except ValueError:
                    pass
                raise MistralQuota("Limite Mistral atteinte", attente)
            if rep.status in (401, 403):
                raise MistralCleInvalide("Clé API Mistral refusée")
            if rep.status >= 400:
                raise MistralErreur(f"Mistral HTTP {rep.status} : {(await rep.text())[:200]}")
            donnees = await rep.json()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise MistralErreur(f"Mistral injoignable : {err}") from err

    try:
        contenu = donnees["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as err:
        raise MistralErreur("Réponse de Mistral inattendue") from err
    return normaliser(_extraire_json(contenu))


async def verifier_cle(session: aiohttp.ClientSession, cle: str) -> None:
    """Lève une exception si la clé n'est pas valide."""
    try:
        async with session.get(
            MISTRAL_MODELS_URL,
            headers={"Authorization": f"Bearer {cle}"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as rep:
            if rep.status in (401, 403):
                raise MistralCleInvalide("Clé refusée")
            if rep.status >= 400:
                raise MistralErreur(f"HTTP {rep.status}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise MistralErreur(str(err)) from err
