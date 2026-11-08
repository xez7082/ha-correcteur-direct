"""Erreurs fréquentes expliquées localement, sans appel à l'IA."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .yaml_doctor import lister_yaml

RE_ID_DOUBLE = re.compile(
    r"Platform (\w+) does not generate unique IDs\. ID (.+?) already exists - ignoring ([\w.]+)"
)
RE_TEMPLATE_SOUS_SENSOR = re.compile(
    r"template integration under the (\w+) platform key is not supported", re.I
)


def cle_regroupement(message: str) -> str | None:
    """Clé commune à toute une famille d'erreurs (pour une seule carte au lieu de dizaines)."""
    m = RE_ID_DOUBLE.search(message)
    if m:
        return f"id_double|{m.group(1)}"
    return None


def ids_doubles(message: str) -> list[tuple[str, str, str]]:
    return [(m.group(1), m.group(2), m.group(3)) for m in RE_ID_DOUBLE.finditer(message)]


def _chercher_id(fichiers: dict[Path, list[str]], ident: str) -> list[tuple[Path, int]]:
    motif = re.compile(
        rf"^\s*(?:-\s*)?(?:unique_id|id)\s*:\s*['\"]?{re.escape(ident)}['\"]?\s*(?:#.*)?$"
    )
    return [
        (chemin, n + 1)
        for chemin, lignes in fichiers.items()
        for n, ligne in enumerate(lignes)
        if motif.match(ligne)
    ]


def _lire_tout(racine: Path) -> dict[Path, list[str]]:
    contenu = {}
    for chemin in lister_yaml(racine):
        try:
            contenu[chemin] = chemin.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    return contenu


def diagnostiquer(racine: Path, probleme: dict[str, Any]) -> dict[str, Any] | None:
    """Retourne {analyse, fichiers} si l'erreur est connue, sinon None (→ IA)."""
    message = probleme.get("message", "")

    doubles = list(dict.fromkeys(probleme.get("ids_doubles") or [])) or [
        i for _, i, _ in ids_doubles(message)
    ]
    if doubles:
        plateforme = ids_doubles(message)[0][0] if ids_doubles(message) else "?"
        fichiers = _lire_tout(racine)
        lignes_expl, emplacements = [], []
        un_seul = []
        for ident in doubles:
            trouves = _chercher_id(fichiers, ident)
            for chemin, ligne in trouves:
                emplacements.append({"chemin": str(chemin), "ligne": ligne, "origine": f"id « {ident} »"})
            if len(trouves) >= 2:
                lieux = " et ".join(f"{c.relative_to(racine)} ligne {l}" for c, l in trouves)
                lignes_expl.append(f"« {ident} » : déclaré dans {lieux}")
            else:
                un_seul.append(ident)
        fichiers_concernes = sorted({Path(e["chemin"]).relative_to(racine).as_posix() for e in emplacements})
        explication = (
            f"{len(doubles)} identifiant(s) sont déclarés deux fois, donc la 2ᵉ copie est ignorée par "
            "Home Assistant. Gardez une seule déclaration de chaque et supprimez (ou déplacez dans "
            "/config/_archive) l'autre. "
        )
        if len(fichiers_concernes) >= 2 and not un_seul:
            explication += (
                "Ici, tous les doublons viennent des fichiers : " + ", ".join(fichiers_concernes)
                + ". Il s'agit souvent d'un ancien fichier gardé à côté du nouveau : "
                "supprimez l'ancien en entier."
            )
        if un_seul:
            explication += (
                "Pour " + ", ".join(f"« {i} »" for i in un_seul) + " une seule déclaration a été "
                "trouvée dans les fichiers YAML : l'autre est sans doute créée par l'interface "
                "(Paramètres → Appareils et services → Entrées / Automatisations) ; supprimez celle-là."
            )
        return {
            "fichiers": emplacements[:30],
            "analyse": {
                "resume": f"{len(doubles)} identifiant(s) {plateforme} déclarés deux fois",
                "gravite": "moyenne",
                "cause": "\n".join(lignes_expl) or "Identifiant déclaré deux fois.",
                "correctif": {"type": "aucun"},
                "confiance": 1.0,
                "explication": explication,
                "local": True,
            },
        }

    m = RE_TEMPLATE_SOUS_SENSOR.search(message)
    if m:
        cle = m.group(1)
        fichiers = _lire_tout(racine)
        emplacements = [
            {"chemin": str(c), "ligne": n + 1, "origine": "platform: template"}
            for c, lignes in fichiers.items()
            for n, l in enumerate(lignes)
            if re.match(r"^\s*-\s*platform:\s*['\"]?template['\"]?\s*$", l)
        ]
        return {
            "fichiers": emplacements[:30],
            "analyse": {
                "resume": f"Capteurs template à déplacer sous « template: » ({cle})",
                "gravite": "moyenne",
                "cause": (
                    f"Une configuration écrite au nouveau format « template » se trouve sous "
                    f"« {cle}: - platform: template ». Home Assistant l'ignore."
                ),
                "correctif": {"type": "aucun"},
                "confiance": 0.9,
                "explication": (
                    f"Dans les fichiers listés, déplacez ces capteurs sous une clé « template: » "
                    f"(format moderne) au lieu de « {cle}: - platform: template ». "
                    f"{len(emplacements)} endroit(s) « platform: template » trouvés."
                ),
                "local": True,
            },
        }
    return None
