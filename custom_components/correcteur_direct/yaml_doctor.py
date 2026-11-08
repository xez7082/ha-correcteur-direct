"""Détection et réparation locale des erreurs de syntaxe YAML (sans IA)."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

import yaml

from .const import DOSSIERS_EXCLUS

DOSSIERS_IGNORES = {"blueprints", "archive", "archives", "old", "backup"}
MOTS_COPIES = ("sauvegarde", "backup", ".bak", "_old", ".old", "copie", "copy")
TAILLE_MAX = 2_000_000
PASSES_MAX = 6  # nombre maximal d'erreurs successives réparées dans un fichier

MOTS_ERREUR_YAML = (
    "while scanning",
    "while parsing",
    "mapping values are not allowed",
    "could not find expected",
    "found character that cannot start",
    "found unexpected end of stream",
    "expected <block end>",
    "did not find expected",
    "found a tab character",
)


class _Chargeur(yaml.SafeLoader):
    """Lit le YAML de Home Assistant en acceptant ses balises (!secret, !include…)."""


_Chargeur.add_multi_constructor("!", lambda chargeur, suffixe, noeud: None)


def est_erreur_yaml(message: str) -> bool:
    bas = (message or "").lower()
    return any(m in bas for m in MOTS_ERREUR_YAML)


def erreur(texte: str) -> dict[str, Any] | None:
    """Retourne la première erreur de syntaxe (ligne/colonne 1-based) ou None."""
    try:
        for _ in yaml.load_all(texte, Loader=_Chargeur):  # noqa: S506 - chargeur sûr
            pass
    except yaml.MarkedYAMLError as err:
        marque = err.problem_mark or err.context_mark
        return {
            "ligne": (marque.line + 1) if marque else 1,
            "colonne": (marque.column + 1) if marque else 1,
            "message": " ".join(
                str(x) for x in (err.context, err.problem) if x
            ) or str(err).splitlines()[0],
        }
    except yaml.YAMLError as err:
        return {"ligne": 1, "colonne": 1, "message": str(err).splitlines()[0]}
    return None


def lister_yaml(racine: Path) -> list[Path]:
    trouves = []
    for dossier, sous, fichiers in os.walk(racine):
        sous[:] = [
            d for d in sous
            if d not in DOSSIERS_EXCLUS and d not in DOSSIERS_IGNORES
            and not d.startswith((".", "_"))
        ]
        for nom in fichiers:
            bas = nom.lower()
            if not bas.endswith((".yaml", ".yml")) or nom == "secrets.yaml":
                continue
            if any(m in bas for m in MOTS_COPIES):
                continue
            trouves.append(Path(dossier) / nom)
    return trouves


def scanner(racine: Path) -> list[dict[str, Any]]:
    """Vérifie la syntaxe de tous les YAML de /config."""
    resultats = []
    for chemin in lister_yaml(racine):
        try:
            if chemin.stat().st_size > TAILLE_MAX:
                continue
            texte = chemin.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        err = erreur(texte)
        if err:
            resultats.append({"chemin": str(chemin), "relatif": str(chemin.relative_to(racine)), **err})
    return resultats


# ---------------------------------------------------------------- réparations

def _indent(ligne: str) -> int:
    return len(ligne) - len(ligne.lstrip(" "))


def _utile(ligne: str) -> bool:
    s = ligne.strip()
    return bool(s) and not s.startswith("#")


def _essai_tabulations(lignes: list[str], _l: int) -> list[list[str]]:
    if not any(re.match(r"^ *\t", x) for x in lignes):
        return []
    nouv = []
    for x in lignes:
        m = re.match(r"^([ \t]*)", x)
        debut = m.group(1) if m else ""
        nouv.append(debut.replace("\t", "  ") + x[len(debut):])
    return [nouv]


def _essai_indentation(lignes: list[str], l: int) -> list[list[str]]:
    """Décale la ligne fautive sur une indentation déjà utilisée au-dessus.

    On essaie d'abord de déplacer la seule ligne, puis la ligne avec son bloc.
    """
    seuls, blocs = [], []
    for i in (l, l - 1, l + 1, l - 2, l + 2):
        if not 0 <= i < len(lignes) or not _utile(lignes[i]):
            continue
        actuel = _indent(lignes[i])
        niveaux = []
        for j in range(i - 1, max(-1, i - 60), -1):
            if _utile(lignes[j]):
                for n in (_indent(lignes[j]), _indent(lignes[j]) + 2):
                    if n not in niveaux:
                        niveaux.append(n)
        niveaux = sorted({n for n in niveaux if n != actuel}, key=lambda n: (abs(n - actuel), n))
        fin = i + 1
        while fin < len(lignes) and (not _utile(lignes[fin]) or _indent(lignes[fin]) > actuel):
            fin += 1
        for cible in niveaux[:8]:
            delta = cible - actuel
            for portee, liste in ((i + 1, seuls), (fin, blocs)):
                if portee == i + 1 and liste is blocs:
                    continue
                nouv = list(lignes)
                for k in range(i, portee):
                    if not nouv[k].strip():
                        continue
                    if delta > 0:
                        nouv[k] = " " * delta + nouv[k]
                    else:
                        nouv[k] = nouv[k][min(-delta, _indent(nouv[k])):]
                liste.append(nouv)
    return seuls + blocs


def _essai_deux_points(lignes: list[str], l: int) -> list[list[str]]:
    essais = []
    for i in (l, l - 1):
        if not 0 <= i < len(lignes):
            continue
        m = re.match(r"^(\s*(?:- )?[A-Za-z_][\w\-]*):(?=[^\s:/])(.*)$", lignes[i])
        if m and "://" not in lignes[i]:
            nouv = list(lignes)
            nouv[i] = f"{m.group(1)}: {m.group(2)}"
            essais.append(nouv)
    return essais


def _essai_guillemets(lignes: list[str], l: int) -> list[list[str]]:
    essais = []
    for i in (l, l - 1, l - 2):
        if not 0 <= i < len(lignes):
            continue
        ligne = lignes[i]
        corps, _, commentaire = ligne.partition(" #")
        for q in ('"', "'"):
            if corps.count(q) % 2 == 1:
                nouv = list(lignes)
                nouv[i] = corps.rstrip() + q + ((" #" + commentaire) if commentaire else "")
                essais.append(nouv)
    return essais


ESSAIS = (_essai_tabulations, _essai_deux_points, _essai_guillemets, _essai_indentation)
NOMS = {
    "_essai_tabulations": "tabulations remplacées par des espaces",
    "_essai_deux_points": "espace ajoutée après « : »",
    "_essai_guillemets": "guillemet fermant ajouté",
    "_essai_indentation": "indentation corrigée",
}


def reparer(texte: str) -> tuple[str, list[str]] | None:
    """Répare localement. Retourne (nouveau texte, actions) seulement si le résultat est valide."""
    fin_ligne = "\r\n" if "\r\n" in texte else "\n"
    lignes = texte.replace("\r\n", "\n").split("\n")
    actions: list[str] = []
    err = erreur("\n".join(lignes))
    if not err:
        return None
    for _ in range(PASSES_MAX):
        l = err["ligne"] - 1
        trouve = False
        for essai in ESSAIS:
            for nouv in essai(lignes, l):
                nouvelle_err = erreur("\n".join(nouv))
                if nouvelle_err is None or nouvelle_err["ligne"] > err["ligne"]:
                    lignes = nouv
                    actions.append(f"ligne {err['ligne']} : {NOMS[essai.__name__]}")
                    err = nouvelle_err
                    trouve = True
                    break
            if trouve:
                break
        if not trouve:
            return None
        if err is None:
            return fin_ligne.join(lignes), actions
    return None


def extrait_modifie(avant: str, apres: str) -> tuple[str, str]:
    """Plus petit bloc de lignes complètes qui diffère, unique dans le fichier."""
    a, b = avant.split("\n"), apres.split("\n")
    debut = 0
    while debut < min(len(a), len(b)) and a[debut] == b[debut]:
        debut += 1
    fa, fb = len(a), len(b)
    while fa > debut and fb > debut and a[fa - 1] == b[fb - 1]:
        fa -= 1
        fb -= 1
    # élargir jusqu'à ce que le bloc « avant » soit unique
    while True:
        rech = "\n".join(a[debut:fa])
        if rech and avant.count(rech) == 1:
            break
        if debut > 0:
            debut -= 1
        elif fa < len(a):
            fa += 1
            fb += 1
        else:
            break
    return "\n".join(a[debut:fa]), "\n".join(b[debut:fb])


def preparer_correctif(chemin: Path, racine: Path) -> dict[str, Any] | None:
    """Lit le fichier, tente la réparation locale, renvoie un correctif prêt à appliquer."""
    try:
        texte = chemin.read_text(encoding="utf-8")
    except OSError:
        return None
    res = reparer(texte)
    if not res:
        return None
    nouveau, actions = res
    rech, remp = extrait_modifie(texte, nouveau)
    if not rech or texte.count(rech) != 1:
        return None
    return {
        "type": "modif_fichier",
        "fichier": str(chemin.relative_to(racine)),
        "rechercher": rech,
        "remplacer": remp,
        "rechargement": "",
        "domaine": "",
        "service": "",
        "donnees": {},
        "local": True,
        "actions": actions,
    }


def simuler(chemin: Path, rech: str, remp: str) -> bool:
    """Vrai si appliquer ce remplacement donne un YAML valide."""
    try:
        texte = chemin.read_text(encoding="utf-8")
    except OSError:
        return False
    if texte.count(rech) != 1:
        return False
    return erreur(texte.replace(rech, remp, 1)) is None
