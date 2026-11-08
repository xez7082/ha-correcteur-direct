"""Moteur : écoute du journal en direct, analyse, correction, vérification."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import logging
import re
import time
import traceback
from typing import Any
import uuid

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import (
    CONF_API_KEY,
    CONF_AUTO_FILES,
    CONF_AUTO_SERVICES,
    CONF_COOLDOWN,
    CONF_LEVEL,
    CONF_MAX_CALLS,
    CONF_MANUAL_DELAY,
    CONF_MIN_CONFIDENCE,
    CONF_MODEL,
    CONF_NOTIFY,
    CONF_VERIFY_DELAY,
    DEFAUTS,
    DOMAIN,
    PANEL_URL,
    PROBLEMES_MAX,
    SERVICES_SURS,
    SIGNAL_MAJ,
    ST_ANALYSE,
    ST_ANNULE,
    ST_APPLICATION,
    ST_CORRIGE,
    ST_ECHEC,
    ST_ERREUR_IA,
    ST_FILE,
    ST_IGNORE,
    ST_PROPOSE,
    ST_QUOTA,
    ST_RECIDIVE,
    ST_SANS,
    ST_VERIF,
    STATUTS_OUVERTS,
    VERSION,
)
from pathlib import Path

from . import diagnostics_locaux, yaml_doctor
from .const import FICHIERS_EXCLUS
from .context import collecter_contexte
from .fixer import CorrectionErreur, Correcteur
from .masking import masquer
from .mistral import MistralCleInvalide, MistralErreur, MistralQuota, analyser

_LOGGER = logging.getLogger(__name__)
PAQUET = __package__ or "custom_components.correcteur_direct"


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_s(iso: str | None) -> float:
    if not iso:
        return 1e12
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return 1e12


RE_COEUR = re.compile(
    r"((?:ValueError|TypeError|KeyError|UndefinedError|ZeroDivisionError)?:?\s*"
    r"Template error:.*|TemplateError\(.*)",
    re.S | re.I,
)


def ilya_court(iso: str | None) -> str:
    sec = _age_s(iso)
    if sec >= 1e11:
        return "pendant la vérification"
    return f"au bout de {int(sec // 60)} min {int(sec % 60)} s"


def signature(nom: str, message: str) -> str:
    """Empreinte d'une erreur, indépendante des nombres et horodatages.

    Une même panne de modèle produit souvent deux lignes différentes
    (« Error while executing automation… » et « …: Error executing script… ») :
    on ne garde que le cœur de l'erreur pour les regrouper.
    """
    message = (message or "").strip().split("\n", 1)[0]
    famille = diagnostics_locaux.cle_regroupement(message)
    if famille:
        return hashlib.sha1(f"{nom}|{famille}".encode()).hexdigest()[:16]
    coeur = RE_COEUR.search(message)
    if coeur:
        message = coeur.group(1)
    norm = message.lower()
    norm = re.sub(r"0x[0-9a-f]+", "#", norm)
    norm = re.sub(r"\d+(?:[.,:]\d+)*", "#", norm)
    norm = re.sub(r"\s+", " ", norm).strip()[:300]
    return hashlib.sha1(f"{nom}|{norm}".encode()).hexdigest()[:16]


LOGGERS_CONTROLE = (
    "annotatedyaml",
    "homeassistant.config",
    "homeassistant.util.yaml",
    "homeassistant.helpers.check_config",
)


class _Ecouteur(logging.Handler):
    """Reçoit chaque ligne de journal, depuis n'importe quel fil d'exécution."""

    def __init__(self, moteur: "Moteur") -> None:
        super().__init__(logging.WARNING)
        self.moteur = moteur

    def emit(self, record: logging.LogRecord) -> None:
        try:
            nom = record.name
            if nom == PAQUET or nom.startswith(PAQUET + "."):
                return  # jamais nos propres messages (évite les boucles)
            if record.levelno < self.moteur.niveau:
                return
            exception = None
            if record.exc_info and record.exc_info[0] is not None:
                exception = "".join(traceback.format_exception(*record.exc_info))[-3000:]
            elif record.exc_text:
                exception = record.exc_text[-3000:]
            donnees = {
                "nom": nom,
                "message": record.getMessage()[:2500],
                "niveau": record.levelname,
                "source": f"{record.pathname}:{record.lineno}",
                "exception": exception,
            }
            self.moteur.hass.loop.call_soon_threadsafe(self.moteur.recevoir, donnees)
        except Exception:  # noqa: BLE001 - un gestionnaire de journal ne doit jamais planter
            pass


class Moteur:
    """Cœur du Correcteur en direct."""

    def __init__(self, hass: HomeAssistant, entree: ConfigEntry) -> None:
        self.hass = hass
        self.entree = entree
        self.store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.donnees")
        self.problemes: dict[str, dict[str, Any]] = {}
        self.par_sig: dict[str, str] = {}
        self.ignores: set[str] = set()
        self.stats = {"corrections": 0, "echecs": 0, "appels_ia": 0, "erreurs_vues": 0}
        self.file: asyncio.Queue[str] = asyncio.Queue()
        self.appels: deque[float] = deque()
        self.en_verif: dict[str, str] = {}
        self.correcteur = Correcteur(hass)
        self.niveau = logging.ERROR
        self.cle_invalide = False
        self.pause_jusqua = 0.0
        self.derniere_erreur_ia: dict[str, Any] | None = None
        self.verrou_yaml = asyncio.Lock()
        self.intervalle = 2.0  # secondes entre deux appels, s'adapte aux refus de Mistral
        self._ecouteur: _Ecouteur | None = None
        self._tache: asyncio.Task | None = None
        self._desabonner: list = []

    # ---------- options ----------

    @property
    def opts(self) -> dict[str, Any]:
        return {**DEFAUTS, **self.entree.data, **self.entree.options}

    # ---------- cycle de vie ----------

    async def async_demarrer(self) -> None:
        donnees = await self.store.async_load() or {}
        self.ignores = set(donnees.get("ignores", []))
        self.stats.update(donnees.get("stats", {}))
        for pb in donnees.get("problemes", []):
            if pb.get("statut") in (ST_FILE, ST_ANALYSE, ST_QUOTA, ST_APPLICATION):
                pb["statut"] = ST_SANS
                pb["erreur"] = "Interrompu par un redémarrage — utilisez « Réanalyser »."
            elif pb.get("statut") == ST_VERIF and not pb.get("verif_manuelle"):
                pb["statut"] = ST_CORRIGE
            self.problemes[pb["id"]] = pb
            self.par_sig[pb["sig"]] = pb["id"]

        for pb in self.problemes.values():
            if pb.get("type") != "yaml" and not (pb.get("analyse") or {}).get("local"):
                pb.pop("fichiers", None)  # recalculés avec la recherche actuelle

        for pb in list(self.problemes.values()):
            if pb.get("statut") == ST_VERIF and pb.get("verif_manuelle"):
                self.verifier(pb["id"])  # relancée après redémarrage

        self.niveau = logging.WARNING if self.opts[CONF_LEVEL] == "WARNING" else logging.ERROR
        self._ecouteur = _Ecouteur(self)
        logging.getLogger().addHandler(self._ecouteur)

        self._tache = self.entree.async_create_background_task(
            self.hass, self._boucle(), f"{DOMAIN}_boucle"
        )
        self._desabonner.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._action_mobile)
        )

        @callback
        def _balayage_differe(_now: Any) -> None:
            self.hass.async_create_task(self.balayer())

        @callback
        def _relocaliser(_now: Any) -> None:
            for iid in list(self.problemes):
                self.hass.async_create_task(self._localiser(iid))

        self._desabonner.append(async_call_later(self.hass, 5, _relocaliser))

        if self.hass.is_running:
            self._desabonner.append(async_call_later(self.hass, 20, _balayage_differe))
        else:

            @callback
            def _demarre(_ev: Event) -> None:
                # l'écouteur « une fois » s'est retiré tout seul : on l'oublie
                if desab in self._desabonner:
                    self._desabonner.remove(desab)
                self._desabonner.append(async_call_later(self.hass, 180, _balayage_differe))

            desab = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _demarre)
            self._desabonner.append(desab)

    async def async_arreter(self) -> None:
        if self._ecouteur:
            logging.getLogger().removeHandler(self._ecouteur)
            self._ecouteur = None
        for unsub in self._desabonner:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._desabonner.clear()
        if self._tache:
            self._tache.cancel()
        await self.store.async_save(self._exporter_store())

    def _exporter_store(self) -> dict[str, Any]:
        return {
            "problemes": list(self.problemes.values()),
            "ignores": sorted(self.ignores),
            "stats": self.stats,
        }

    @callback
    def _maj(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_MAJ)
        self.store.async_delay_save(self._exporter_store, 5)

    # ---------- réception en direct ----------

    @callback
    def recevoir(self, d: dict[str, Any], balayage: bool = False) -> None:
        """Appelé pour chaque erreur, à l'instant où elle est écrite au journal."""
        # Messages produits par nos propres vérifications : ce ne sont pas de nouvelles erreurs
        if (self.correcteur.en_controle or self.verrou_yaml.locked()) and (
            d["nom"].startswith(LOGGERS_CONTROLE) or yaml_doctor.est_erreur_yaml(d["message"])
        ):
            return
        sig = signature(d["nom"], d["message"])
        if sig in self.ignores:
            return
        self.stats["erreurs_vues"] += 1
        maintenant = _maintenant()

        if sig in self.en_verif:
            pb = self.problemes.get(self.en_verif[sig])
            if pb:
                pb["recidive_verif"] = True
                if pb.get("verif_manuelle"):
                    pb["occurrences"] += 1
                    pb["dernier"] = maintenant
                    self._fin_verif_manuelle(pb["id"])
                    return

        iid = self.par_sig.get(sig)
        pb = self.problemes.get(iid) if iid else None
        nouveaux_ids = [i for _, i, _ in diagnostics_locaux.ids_doubles(d["message"])]
        if pb:
            if nouveaux_ids:
                connus = pb.setdefault("ids_doubles", [])
                ajout = [i for i in nouveaux_ids if i not in connus]
                if ajout:
                    connus.extend(ajout)
                    if pb["statut"] not in (ST_FILE, ST_ANALYSE, ST_IGNORE):
                        self._mettre_en_file(pb)  # diagnostic local : gratuit
            if balayage:
                self._maj()
                return
            pb["occurrences"] += 1
            pb["dernier"] = maintenant
            if pb["statut"] in (ST_CORRIGE, ST_ANNULE):
                pb["statut"] = ST_RECIDIVE
                if self._reanalysable(pb):
                    self._mettre_en_file(pb)
            elif pb["statut"] in (ST_ECHEC, ST_SANS, ST_ERREUR_IA) and self._reanalysable(pb):
                self._mettre_en_file(pb)
            self._maj()
            return

        pb = {
            "id": uuid.uuid4().hex[:10],
            "sig": sig,
            "nom": d["nom"],
            "niveau": d.get("niveau", "ERROR"),
            "message": d["message"],
            "exception": d.get("exception"),
            "source": d.get("source"),
            "occurrences": int(d.get("count", 1) or 1),
            "premier": maintenant,
            "dernier": maintenant,
            "statut": ST_FILE,
            "analyse": None,
            "historique": [],
            "ids_doubles": nouveaux_ids,
        }
        self.problemes[pb["id"]] = pb
        self.par_sig[sig] = pb["id"]
        self._purger()
        self._mettre_en_file(pb)
        self._maj()
        self.hass.async_create_task(self._localiser(pb["id"]))

    async def _localiser(self, iid: str) -> None:
        """Retrouve les fichiers concernés tout de suite, sans attendre l'IA."""
        pb = self.problemes.get(iid)
        if not pb or pb.get("fichiers") or (pb.get("analyse") or {}).get("local"):
            return
        try:
            ctx = await collecter_contexte(self.hass, pb)
        except Exception:  # noqa: BLE001
            return
        if not pb.get("fichiers"):
            pb["fichiers"] = ctx.get("fichiers", [])
            self._maj()

    def _reanalysable(self, pb: dict[str, Any]) -> bool:
        return _age_s(pb.get("analyse_le")) > float(self.opts[CONF_COOLDOWN]) * 3600

    @callback
    def _mettre_en_file(self, pb: dict[str, Any]) -> None:
        pb["statut"] = ST_FILE
        pb.pop("erreur", None)
        self.file.put_nowait(pb["id"])

    def _purger(self) -> None:
        if len(self.problemes) <= PROBLEMES_MAX:
            return
        fermes = sorted(
            (p for p in self.problemes.values() if p["statut"] not in STATUTS_OUVERTS),
            key=lambda p: p["dernier"],
        )
        for p in fermes[: len(self.problemes) - PROBLEMES_MAX]:
            self.problemes.pop(p["id"], None)
            if self.par_sig.get(p["sig"]) == p["id"]:
                self.par_sig.pop(p["sig"], None)

    async def balayer(self) -> int:
        """Reprend les erreurs déjà présentes dans le journal système."""
        avant = len(self.problemes)
        journal = self.hass.data.get("system_log")
        try:
            entrees = journal.records.to_list() if journal else []
        except Exception:  # noqa: BLE001
            entrees = []
        for r in entrees:
            niveau = logging.getLevelName(str(r.get("level", "ERROR")))
            if isinstance(niveau, int) and niveau < self.niveau:
                continue
            msg = r.get("message")
            msg = "\n".join(map(str, msg)) if isinstance(msg, (list, tuple)) else str(msg)
            src = r.get("source")
            src = ":".join(map(str, src)) if isinstance(src, (list, tuple)) else str(src)
            nom = str(r.get("name", "inconnu"))
            if nom == PAQUET or nom.startswith(PAQUET + "."):
                continue
            self.recevoir(
                {
                    "nom": nom,
                    "message": msg[:2500],
                    "niveau": str(r.get("level", "ERROR")),
                    "source": src,
                    "exception": (r.get("exception") or None),
                    "count": r.get("count", 1),
                },
                balayage=True,
            )
        return len(self.problemes) - avant

    # ---------- analyse ----------

    def _quota_ok(self) -> bool:
        limite = time.monotonic() - 3600
        while self.appels and self.appels[0] < limite:
            self.appels.popleft()
        return len(self.appels) < int(self.opts[CONF_MAX_CALLS])

    @callback
    def _reprendre_plus_tard(self, iid: str, delai: int) -> None:
        @callback
        def _cb(_now: Any) -> None:
            pb = self.problemes.get(iid)
            if pb and pb["statut"] == ST_QUOTA:
                self._mettre_en_file(pb)
                self._maj()

        self._desabonner.append(async_call_later(self.hass, delai, _cb))

    async def _boucle(self) -> None:
        while True:
            iid = await self.file.get()
            pb = self.problemes.get(iid)
            if not pb or pb["statut"] != ST_FILE:
                continue
            attente = self.pause_jusqua - time.monotonic()
            if attente > 0:
                await asyncio.sleep(attente)
            try:
                await self._traiter(pb)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                pb["statut"] = ST_ERREUR_IA
                pb["erreur"] = f"Erreur interne : {err}"[:300]
            self._maj()
            await asyncio.sleep(self.intervalle)

    def _construire_texte(self, pb: dict[str, Any], ctx: dict[str, Any]) -> str:
        parties = [
            "ERREUR HOME ASSISTANT",
            f"Journal (logger) : {pb['nom']}",
            f"Niveau : {pb['niveau']}",
            f"Occurrences : {pb['occurrences']}",
            f"Code source : {pb.get('source')}",
        ]
        if ctx.get("integration"):
            entrees = ", ".join(f"{e['titre']} ({e['etat']})" for e in ctx["entrees"]) or "aucune"
            parties.append(
                f"Intégration concernée : {ctx['integration']} — entrées configurées : {entrees}"
            )
        parties += ["", "Message :", pb["message"]]
        if pb.get("exception"):
            parties += ["", "Trace (fin) :", pb["exception"][-1800:]]
        if ctx.get("etats"):
            parties += ["", "Entités citées :"]
            for e in ctx["etats"]:
                parties.append(f"- {e['entite']} : état={e['etat']} attributs={e['attributs']}")
        if ctx.get("extraits"):
            parties += ["", "Extraits des fichiers YAML de l'utilisateur :"]
            for x in ctx["extraits"]:
                parties += [
                    f"### fichier « {x['fichier']} » (lignes {x['debut']} à {x['fin']})",
                    "```yaml",
                    x["texte"],
                    "```",
                ]
        else:
            parties += [
                "",
                "Aucun extrait YAML trouvé : la configuration est peut-être faite par l'interface.",
            ]
        precedent = (pb.get("analyse") or {}).get("correctif")
        if precedent and pb.get("applique_le"):
            parties += [
                "",
                "ATTENTION : un correctif précédent n'a pas fait disparaître l'erreur :",
                str(precedent)[:800],
                "Propose autre chose.",
            ]
        return masquer("\n".join(parties))

    async def _traiter(self, pb: dict[str, Any]) -> None:
        opts = self.opts
        diag = await self.hass.async_add_executor_job(
            diagnostics_locaux.diagnostiquer, self.correcteur.racine, pb
        )
        if diag:
            pb["fichiers"] = diag["fichiers"]
            pb["analyse"] = diag["analyse"]
            pb["analyse_le"] = _maintenant()
            pb["statut"] = ST_SANS
            pb.pop("erreur", None)
            return

        if pb.get("type") != "yaml" and yaml_doctor.est_erreur_yaml(
            f"{pb['message']}\n{pb.get('exception') or ''}"
        ):
            bilan = await self.corriger_yaml(appliquer=bool(opts[CONF_AUTO_FILES]))
            pb["statut"] = ST_SANS
            pb["erreur"] = (
                f"Erreur de syntaxe YAML : {bilan['erreurs']} fichier(s) en erreur, "
                f"{bilan['corriges']} réparé(s), {bilan['proposes']} à valider. "
                "Voir la ou les cartes « Syntaxe YAML »."
            )
            return

        if not opts.get(CONF_API_KEY) or self.cle_invalide:
            pb["statut"] = ST_ERREUR_IA
            pb["erreur"] = "Clé API Mistral absente ou refusée (voir Options de l'intégration)."
            return
        if not self._quota_ok():
            pb["statut"] = ST_QUOTA
            pb["erreur"] = (
                f"Limite réglée atteinte ({int(opts[CONF_MAX_CALLS])} appels/heure). "
                "Nouvel essai dans 5 min — vous pouvez augmenter la limite dans les réglages."
            )
            self._reprendre_plus_tard(pb["id"], 300)
            return

        pb["statut"] = ST_ANALYSE
        self._maj()
        ctx = await collecter_contexte(self.hass, pb)
        texte = self._construire_texte(pb, ctx)
        pb["fichiers"] = ctx.get("fichiers", [])

        self.appels.append(time.monotonic())
        self.stats["appels_ia"] += 1
        session = async_get_clientsession(self.hass)
        try:
            res = await analyser(session, opts[CONF_API_KEY], opts[CONF_MODEL], texte)
        except MistralQuota as err:
            self.derniere_erreur_ia = {"le": _maintenant(), "message": str(err)}
            self.appels.pop() if self.appels else None  # refusé : ne compte pas
            self.intervalle = min(120.0, self.intervalle * 2)
            attente = int(max(err.attente or 0, self.intervalle * 2, 10))
            pb["statut"] = ST_QUOTA
            pb["erreur"] = (
                f"{err} — nouvel essai dans {attente} s, puis un appel toutes les "
                f"{int(self.intervalle)} s."
            )
            self.pause_jusqua = time.monotonic() + attente
            self._reprendre_plus_tard(pb["id"], attente)
            return
        except MistralCleInvalide as err:
            self.cle_invalide = True
            pb["statut"] = ST_ERREUR_IA
            pb["erreur"] = str(err)
            return
        except MistralErreur as err:
            self.derniere_erreur_ia = {"le": _maintenant(), "message": str(err)}
            pb["statut"] = ST_ERREUR_IA
            pb["erreur"] = str(err)[:400]
            return

        if pb.get("analyse"):
            pb["historique"].append(
                {"le": pb.get("analyse_le"), "resume": pb["analyse"].get("resume"),
                 "correctif": pb["analyse"].get("correctif", {}).get("type")}
            )
            pb["historique"] = pb["historique"][-5:]
        self.intervalle = max(2.0, self.intervalle * 0.8)  # ça passe : on réaccélère doucement
        self.derniere_erreur_ia = None
        pb["analyse"] = res
        pb["analyse_le"] = _maintenant()
        pb.pop("erreur", None)

        correctif = res["correctif"]
        if correctif["type"] == "modif_fichier" and correctif["fichier"]:
            correctif["chemin_complet"] = str(
                self.correcteur.racine / correctif["fichier"].lstrip("/")
            )
        if correctif["type"] == "aucun":
            pb["statut"] = ST_SANS
            if res["gravite"] == "critique":
                self._notifier(pb, avec_actions=False)
            return

        raison = await self.correcteur.valider(correctif)
        if raison:
            pb["statut"] = ST_SANS
            pb["erreur"] = f"Correctif proposé refusé : {raison}"
            return

        pb["statut"] = ST_PROPOSE
        if self._automatique(correctif, res["confiance"]):
            await self.appliquer(pb["id"], auto=True)
        else:
            self._notifier(pb, avec_actions=True)

    def _automatique(self, correctif: dict[str, Any], confiance: float) -> bool:
        opts = self.opts
        if confiance * 100 < float(opts[CONF_MIN_CONFIDENCE]):
            return False
        type_ = correctif["type"]
        if type_ == "recharger_integration":
            return bool(opts[CONF_AUTO_SERVICES])
        if type_ == "appel_service":
            nom = f"{correctif['domaine']}.{correctif['service']}"
            return bool(opts[CONF_AUTO_SERVICES]) and nom in SERVICES_SURS
        if type_ == "modif_fichier":
            return bool(opts[CONF_AUTO_FILES])
        return False  # jamais de redémarrage automatique

    # ---------- application / vérification ----------

    async def appliquer(self, iid: str, auto: bool = False) -> bool:
        pb = self.problemes.get(iid)
        if not pb or not pb.get("analyse"):
            return False
        if pb["statut"] in (ST_APPLICATION, ST_VERIF):
            return False
        correctif = pb["analyse"]["correctif"]
        pb["statut"] = ST_APPLICATION
        self._maj()
        try:
            info = await self.correcteur.appliquer(correctif)
        except CorrectionErreur as err:
            pb["statut"] = ST_ECHEC
            pb["erreur"] = str(err)
            self.stats["echecs"] += 1
            self._notifier(pb, avec_actions=False)
            self._maj()
            return False
        except Exception as err:  # noqa: BLE001
            pb["statut"] = ST_ECHEC
            pb["erreur"] = f"Échec : {err}"[:300]
            self.stats["echecs"] += 1
            self._maj()
            return False

        pb["retour_arriere"] = info
        pb["applique_le"] = _maintenant()
        pb["auto"] = auto
        pb.pop("erreur", None)
        pb.pop("recidive_verif", None)

        if info.get("type") == "redemarrage":
            pb["statut"] = ST_CORRIGE
            self._maj()
            return True

        if pb.get("type") == "yaml":
            pb["statut"] = ST_CORRIGE
            self.stats["corrections"] += 1
            self._notifier_resolu(pb)
            self._maj()
            return True

        pb["statut"] = ST_VERIF
        self.en_verif[pb["sig"]] = iid
        self._maj()

        @callback
        def _fin(_now: Any) -> None:
            self.hass.async_create_task(self._fin_verification(iid))

        self._desabonner.append(
            async_call_later(self.hass, int(self.opts[CONF_VERIFY_DELAY]), _fin)
        )
        return True

    async def _fin_verification(self, iid: str) -> None:
        pb = self.problemes.get(iid)
        if not pb:
            return
        self.en_verif.pop(pb["sig"], None)
        if pb.get("recidive_verif"):
            annule = False
            try:
                annule = await self.correcteur.annuler(pb.get("retour_arriere"))
            except CorrectionErreur as err:
                pb["erreur"] = f"Retour arrière impossible : {err}"
            pb["statut"] = ST_ECHEC
            pb.setdefault(
                "erreur",
                "L'erreur est revenue pendant la vérification"
                + (" : fichier restauré." if annule else "."),
            )
            pb["historique"].append(
                {"le": _maintenant(), "resume": "Correctif sans effet",
                 "correctif": pb["analyse"]["correctif"]["type"]}
            )
            self.stats["echecs"] += 1
            self._notifier(pb, avec_actions=False)
        else:
            pb["statut"] = ST_CORRIGE
            self.stats["corrections"] += 1
            self._notifier_resolu(pb)
        pb.pop("recidive_verif", None)
        self._maj()

    # ---------- erreurs de syntaxe YAML ----------

    def _probleme_yaml(self, e: dict[str, Any]) -> dict[str, Any]:
        sig = signature("syntaxe_yaml", e["relatif"])
        maintenant = _maintenant()
        message = (
            f"Erreur de syntaxe YAML dans {e['chemin']}, ligne {e['ligne']}, "
            f"colonne {e['colonne']} : {e['message']}"
        )
        iid = self.par_sig.get(sig)
        pb = self.problemes.get(iid) if iid else None
        if pb is None:
            pb = {
                "id": uuid.uuid4().hex[:10], "sig": sig, "type": "yaml", "statut": ST_ANALYSE,
                "nom": "Syntaxe YAML", "niveau": "ERROR", "occurrences": 0,
                "premier": maintenant, "historique": [], "exception": None,
                "source": e["relatif"],
            }
            self.problemes[pb["id"]] = pb
            self.par_sig[sig] = pb["id"]
        pb.update(
            statut=ST_ANALYSE, message=message, dernier=maintenant, analyse=None, retour_arriere=None,
            fichiers=[{"chemin": e["chemin"], "ligne": e["ligne"], "origine": "vérification YAML"}],
        )
        pb["occurrences"] = pb.get("occurrences", 0) + 1
        pb.pop("erreur", None)
        return pb

    async def _yaml_par_ia(self, e: dict[str, Any]) -> dict[str, Any] | None:
        """Cas difficiles : on demande à Mistral, puis on vérifie localement le résultat."""
        opts = self.opts
        if not opts.get(CONF_API_KEY) or self.cle_invalide or not self._quota_ok():
            return None
        if Path(e["chemin"]).name in FICHIERS_EXCLUS:
            return None  # données personnelles : jamais envoyées
        attente = self.pause_jusqua - time.monotonic()
        if attente > 0:
            await asyncio.sleep(attente)

        def _lire() -> str:
            return Path(e["chemin"]).read_text(encoding="utf-8", errors="replace")

        lignes = (await self.hass.async_add_executor_job(_lire)).split("\n")
        debut = max(0, e["ligne"] - 16)
        fin = min(len(lignes), e["ligne"] + 15)
        texte = masquer(
            "ERREUR DE SYNTAXE YAML (le fichier ne peut pas être lu par Home Assistant)\n"
            f"Fichier « {e['relatif']} », ligne {e['ligne']}, colonne {e['colonne']} : {e['message']}\n\n"
            "Corrige UNIQUEMENT la syntaxe (indentation, guillemets, deux-points, crochets), "
            "sans changer le sens ni les valeurs. Réponds avec type modif_fichier.\n\n"
            f"### fichier « {e['relatif']} » (lignes {debut + 1} à {fin})\n```yaml\n"
            + "\n".join(lignes[debut:fin]) + "\n```"
        )
        self.appels.append(time.monotonic())
        self.stats["appels_ia"] += 1
        try:
            res = await analyser(
                async_get_clientsession(self.hass), opts[CONF_API_KEY], opts[CONF_MODEL], texte
            )
        except MistralQuota as err:
            if self.appels:
                self.appels.pop()
            self.intervalle = min(120.0, self.intervalle * 2)
            self.pause_jusqua = time.monotonic() + max(err.attente or 0, self.intervalle * 2, 10)
            return None
        except MistralErreur:
            return None
        c = res["correctif"]
        if c["type"] != "modif_fichier" or c["fichier"].lstrip("/") != e["relatif"]:
            return None
        ok = await self.hass.async_add_executor_job(
            yaml_doctor.simuler, Path(e["chemin"]), c["rechercher"], c["remplacer"]
        )
        if not ok:
            return None
        c["actions"] = [res.get("explication") or "correction proposée par Mistral"]
        return c

    async def corriger_yaml(self, appliquer: bool = True) -> dict[str, Any]:
        """Vérifie tous les YAML et répare les erreurs de syntaxe (une seule à la fois)."""
        async with self.verrou_yaml:
            return await self._corriger_yaml(appliquer)

    async def _corriger_yaml(self, appliquer: bool) -> dict[str, Any]:
        racine = self.correcteur.racine
        erreurs = await self.hass.async_add_executor_job(yaml_doctor.scanner, racine)
        bilan: dict[str, Any] = {
            "erreurs": len(erreurs), "corriges": 0, "proposes": 0,
            "non_repares": 0, "details": [],
        }
        # Réparations locales d'abord (instantanées), l'IA ensuite pour le reste
        locaux = {}
        for e in erreurs:
            locaux[e["chemin"]] = await self.hass.async_add_executor_job(
                yaml_doctor.preparer_correctif, Path(e["chemin"]), racine
            )
        erreurs.sort(key=lambda e: locaux[e["chemin"]] is None)
        for e in erreurs:
            pb = self._probleme_yaml(e)
            correctif = locaux[e["chemin"]]
            source = "locale (sans IA)"
            if correctif is None:
                correctif = await self._yaml_par_ia(e)
                source = "par Mistral, vérifiée localement"
            if correctif is None:
                pb["statut"] = ST_SANS
                pb["analyse"] = {
                    "resume": f"Syntaxe YAML : {e['relatif']} ligne {e['ligne']}",
                    "gravite": "critique", "cause": e["message"], "confiance": 0,
                    "correctif": {"type": "aucun"},
                    "explication": (
                        f"Réparation automatique impossible. Ouvrez {e['chemin']} "
                        f"à la ligne {e['ligne']} (colonne {e['colonne']})."
                    ),
                }
                bilan["non_repares"] += 1
                bilan["details"].append(f"❌ {e['relatif']} ligne {e['ligne']}")
                continue
            pb["analyse"] = {
                "resume": f"Syntaxe YAML : {e['relatif']} ligne {e['ligne']}",
                "gravite": "critique",
                "cause": e["message"],
                "correctif": correctif,
                "confiance": 1.0 if correctif.get("local") else 0.9,
                "explication": f"Réparation {source} : " + " ; ".join(correctif.get("actions", []))
                + ". Le fichier a été relu sans erreur avec cette correction.",
            }
            pb["analyse_le"] = _maintenant()
            pb["statut"] = ST_PROPOSE
            if appliquer and await self.appliquer(pb["id"]):
                bilan["corriges"] += 1
                bilan["details"].append(f"✅ {e['relatif']} ligne {e['ligne']}")
            elif appliquer:
                bilan["non_repares"] += 1
                bilan["details"].append(f"❌ {e['relatif']} : {pb.get('erreur', '')}")
            else:
                bilan["proposes"] += 1
                bilan["details"].append(f"🔔 {e['relatif']} ligne {e['ligne']} : correction proposée")
                self._notifier(pb, avec_actions=True)
        self._maj()
        return bilan

    # ---------- « J'ai corrigé » : vérification sans IA ----------

    @callback
    def verifier(self, iid: str) -> bool:
        """Surveille l'erreur quelques minutes : si elle ne revient pas, elle est corrigée."""
        pb = self.problemes.get(iid)
        if not pb or pb["statut"] in (ST_APPLICATION, ST_IGNORE):
            return False
        if pb["statut"] == ST_VERIF and not pb.get("verif_manuelle"):
            return False  # déjà en vérification après un correctif automatique
        minutes = max(1, int(self.opts[CONF_MANUAL_DELAY]))
        pb["statut"] = ST_VERIF
        pb["verif_manuelle"] = True
        pb["verif_debut"] = _maintenant()
        pb["verif_fin"] = datetime.fromtimestamp(
            time.time() + minutes * 60, timezone.utc
        ).isoformat()
        pb.pop("recidive_verif", None)
        pb.pop("erreur", None)
        self.en_verif[pb["sig"]] = iid
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{iid}")

        @callback
        def _fin(_now: Any) -> None:
            self._fin_verif_manuelle(iid)

        self._desabonner.append(async_call_later(self.hass, minutes * 60, _fin))
        self._maj()
        return True

    @callback
    def _fin_verif_manuelle(self, iid: str) -> None:
        pb = self.problemes.get(iid)
        if not pb or pb["statut"] != ST_VERIF or not pb.get("verif_manuelle"):
            return  # déjà terminée
        self.en_verif.pop(pb["sig"], None)
        pb.pop("verif_manuelle", None)
        pb.pop("verif_fin", None)
        if pb.pop("recidive_verif", False):
            pb["statut"] = ST_RECIDIVE
            pb["erreur"] = (
                "La correction n'a pas suffi : l'erreur est revenue pendant la vérification "
                f"({ilya_court(pb.pop('verif_debut', None))})."
            )
        else:
            pb.pop("verif_debut", None)
            pb["statut"] = ST_CORRIGE
            pb["corrige_main"] = True
            self.stats["corrections"] += 1
        self._maj()

    @callback
    def tout_verifier(self) -> int:
        n = 0
        for pb in list(self.problemes.values()):
            if pb["statut"] in (ST_PROPOSE, ST_ECHEC, ST_RECIDIVE, ST_SANS, ST_ERREUR_IA, ST_QUOTA, ST_FILE):
                n += self.verifier(pb["id"])
        return n

    async def annuler(self, iid: str) -> bool:
        pb = self.problemes.get(iid)
        if not pb:
            return False
        ok = await self.correcteur.annuler(pb.get("retour_arriere"))
        if ok:
            pb["statut"] = ST_ANNULE
            pb["retour_arriere"] = None
            self._maj()
        return ok

    @callback
    def ignorer(self, iid: str) -> None:
        pb = self.problemes.get(iid)
        if not pb:
            return
        self.ignores.add(pb["sig"])
        pb["statut"] = ST_IGNORE
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{iid}")
        self._maj()

    @callback
    def reactiver(self, iid: str) -> None:
        pb = self.problemes.get(iid)
        if not pb:
            return
        self.ignores.discard(pb["sig"])
        pb["analyse_le"] = None
        self._mettre_en_file(pb)
        self._maj()

    @callback
    def reanalyser(self, iid: str) -> None:
        pb = self.problemes.get(iid)
        if not pb or pb["statut"] in (ST_ANALYSE, ST_APPLICATION, ST_VERIF):
            return
        self.cle_invalide = False
        self._mettre_en_file(pb)
        self._maj()

    @callback
    def vider(self) -> None:
        """Efface l'historique des problèmes fermés (les ignorés restent)."""
        for iid in [
            i for i, p in self.problemes.items()
            if p["statut"] not in STATUTS_OUVERTS and p["statut"] != ST_IGNORE
        ]:
            self.problemes.pop(iid)
        self.par_sig = {p["sig"]: i for i, p in self.problemes.items()}
        self._maj()

    # ---------- notifications ----------

    def _texte_notif(self, pb: dict[str, Any]) -> tuple[str, str]:
        a = pb.get("analyse") or {}
        titre = f"🔧 {a.get('resume') or 'Erreur Home Assistant'}"
        lignes = []
        fichiers = pb.get("fichiers") or []
        if fichiers:
            f = fichiers[0]
            lignes.append(
                f"**Fichier :** `{f['chemin']}`" + (f" — ligne {f['ligne']}" if f.get("ligne") else "")
            )
        if a.get("cause"):
            lignes.append(f"**Cause :** {a['cause']}")
        if a.get("explication"):
            lignes.append(f"**Correctif :** {a['explication']}")
        if pb.get("erreur"):
            lignes.append(f"**État :** {pb['erreur']}")
        lignes.append(f"[Ouvrir le Correcteur](/{PANEL_URL})")
        return titre, "\n\n".join(lignes)

    @callback
    def _notifier(self, pb: dict[str, Any], avec_actions: bool) -> None:
        titre, texte = self._texte_notif(pb)
        persistent_notification.async_create(
            self.hass, texte, title=titre, notification_id=f"{DOMAIN}_{pb['id']}"
        )
        service = str(self.opts.get(CONF_NOTIFY) or "").strip()
        if not service:
            return
        service = service.removeprefix("notify.")
        if not self.hass.services.has_service("notify", service):
            return
        data: dict[str, Any] = {"tag": f"{DOMAIN}_{pb['id']}", "url": f"/{PANEL_URL}",
                                "clickAction": f"/{PANEL_URL}"}
        if avec_actions:
            data["actions"] = [
                {"action": f"CD_APPLIQUER_{pb['id']}", "title": "✅ Appliquer"},
                {"action": f"CD_IGNORER_{pb['id']}", "title": "🙈 Ignorer"},
            ]
        a = pb.get("analyse") or {}
        message = a.get("explication") or a.get("cause") or pb.get("erreur") or pb["message"]
        fichiers = pb.get("fichiers") or []
        if fichiers:
            f = fichiers[0]
            message = f"📄 {f['chemin']}" + (f" (ligne {f['ligne']})" if f.get("ligne") else "") + f"\n{message}"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "notify", service,
                {"title": titre, "message": message[:400], "data": data},
            )
        )

    @callback
    def _notifier_resolu(self, pb: dict[str, Any]) -> None:
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{pb['id']}")
        if pb.get("auto"):
            a = pb.get("analyse") or {}
            persistent_notification.async_create(
                self.hass,
                f"Corrigé automatiquement et vérifié : {a.get('explication', '')}\n\n"
                f"[Annuler depuis le Correcteur](/{PANEL_URL})",
                title=f"✅ {a.get('resume', 'Erreur corrigée')}",
                notification_id=f"{DOMAIN}_{pb['id']}",
            )

    @callback
    def _action_mobile(self, event: Event) -> None:
        action = str(event.data.get("action", ""))
        if action.startswith("CD_APPLIQUER_"):
            self.hass.async_create_task(self.appliquer(action.removeprefix("CD_APPLIQUER_")))
        elif action.startswith("CD_IGNORER_"):
            self.ignorer(action.removeprefix("CD_IGNORER_"))

    # ---------- export pour le panneau et les capteurs ----------

    def ouverts(self) -> list[dict[str, Any]]:
        return [p for p in self.problemes.values() if p["statut"] in STATUTS_OUVERTS]

    def exporter(self) -> dict[str, Any]:
        problemes = sorted(self.problemes.values(), key=lambda p: p["dernier"], reverse=True)
        opts = self.opts
        self._quota_ok()  # retire du compteur les appels de plus d'une heure
        return {
            "version": VERSION,
            "problemes": problemes,
            "stats": {**self.stats, "appels_derniere_heure": len(self.appels)},
            "options": {
                "modele": opts[CONF_MODEL],
                "niveau": opts[CONF_LEVEL],
                "auto_services": opts[CONF_AUTO_SERVICES],
                "auto_fichiers": opts[CONF_AUTO_FILES],
                "appels_max_heure": int(opts[CONF_MAX_CALLS]),
                "delai_verification": int(opts[CONF_VERIFY_DELAY]),
            },
            "cle_invalide": self.cle_invalide,
            "ia": {
                "pause_restante": max(0, int(self.pause_jusqua - time.monotonic())),
                "intervalle": int(self.intervalle),
                "en_file": sum(1 for p in self.problemes.values() if p["statut"] == ST_FILE),
                "derniere_erreur": self.derniere_erreur_ia,
            },
        }
