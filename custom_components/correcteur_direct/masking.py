"""Masquage local des données sensibles avant tout envoi à Mistral."""
from __future__ import annotations

import re

MARQUE = "‹"  # tout texte masqué commence par ce caractère

_REGLES: list[tuple[re.Pattern[str], str]] = [
    # URL avec identifiants : https://user:pass@hote
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/'\"@:]+:[^\s/'\"@]+@"), r"\1‹IDENTIFIANTS›@"),
    # Jetons JWT (jetons longue durée Home Assistant, etc.)
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "‹JETON›"),
    # En-tête Authorization
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ‹MASQUÉ›"),
    # clé: valeur / clé=valeur pour les noms sensibles
    (
        re.compile(
            r"(?i)(?<!!)\b(password|passwd|pwd|mot_de_passe|motdepasse|token|access_token|"
            r"refresh_token|api_key|apikey|api-key|secret|client_secret|private_key|"
            r"webhook_id|auth|authorization|cle|clé|key)\b(\s*[:=]\s*|\s+)"
            r"(?!!secret)(['\"]?)([^\s,'\"}\]]+)\3"
        ),
        r"\1\2\3‹MASQUÉ›\3",
    ),
    # Adresses e-mail
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "‹EMAIL›"),
    # Adresses MAC
    (re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b"), "‹MAC›"),
    # Adresses IPv4
    (re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b"), "‹IP›"),
    # Longues chaînes aléatoires (clés API, numéros de série)
    (re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{28,}\b"), "‹CLÉ›"),
    # Coordonnées GPS (latitude/longitude)
    (re.compile(r"(?i)\b(latitude|longitude|lat|lon|lng)(\s*[:=]\s*)-?\d{1,3}\.\d{3,}"), r"\1\2‹GPS›"),
]


def masquer(texte: str | None) -> str:
    """Retourne le texte avec les données sensibles remplacées."""
    if not texte:
        return ""
    for motif, remplacement in _REGLES:
        texte = motif.sub(remplacement, texte)
    return texte


def contient_masque(texte: str | None) -> bool:
    """Vrai si le texte contient une valeur masquée (donc inutilisable tel quel)."""
    return bool(texte) and MARQUE in texte
