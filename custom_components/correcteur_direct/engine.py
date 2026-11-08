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


def signature(nom: str, message: str) -> str:
    """Empreinte d'une erreur, indépendante des nombres et horodatages."""
    norm = message.lower()
    norm = re.sub(r"0x[0-9a-f]+", "#", norm)
    norm = re.sub(r"\d+(?:[.,:]\d+)*", "#", norm)
    norm = re.sub(r"\s+", " ", norm).strip()[:300]
    return hashlib.sha1(f"{nom}|{norm}".encode()).hexdigest()[:16]


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
            elif pb.get("statut") == ST_VERIF:
                pb["statut"] = ST_CORRIGE
            self.problemes[pb["id"]] = pb
            self.par_sig[pb["sig"]] = pb["id"]

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

        if self.hass.is_running:
            self._desabonner.append(async_call_later(self.hass, 20, _balayage_differe))
        else:

            @callback
            def _demarre(_ev: Event) -> None:
                self._desabonner.append(async_call_later(self.hass, 180, _balayage_differe))

            self._desabonner.append(
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _demarre)
            )

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
        sig = signature(d["nom"], d["message"])
        if sig in self.ignores:
            return
        self.stats["erreurs_vues"] += 1
        maintenant = _maintenant()

        if sig in self.en_verif:
            pb = self.problemes.get(self.en_verif[sig])
            if pb:
                pb["recidive_verif"] = True

        iid = self.par_sig.get(sig)
        pb = self.problemes.get(iid) if iid else None
        if pb:
            if balayage:
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
        }
        self.problemes[pb["id"]] = pb
        self.par_sig[sig] = pb["id"]
        self._purger()
        self._mettre_en_file(pb)
        self._maj()

    def _reanalysable(self, pb: dict[str, Any]) -> bool:
        return _age_s(pb.get("analyse_le")) > float(self.opts[CONF_COOLDOWN]) * 3600

    @callback
    def _mettre_en_file(self, pb: dict[str, Any]) -> None:
        pb["statut"] = ST_FILE
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
            try:
                await self._traiter(pb)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                pb["statut"] = ST_ERREUR_IA
                pb["erreur"] = f"Erreur interne : {err}"[:300]
            self._maj()
            await asyncio.sleep(1)

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
        if not opts.get(CONF_API_KEY) or self.cle_invalide:
            pb["statut"] = ST_ERREUR_IA
            pb["erreur"] = "Clé API Mistral absente ou refusée (voir Options de l'intégration)."
            return
        if not self._quota_ok():
            pb["statut"] = ST_QUOTA
            self._reprendre_plus_tard(pb["id"], 300)
            return

        pb["statut"] = ST_ANALYSE
        self._maj()
        ctx = await collecter_contexte(self.hass, pb)
        texte = self._construire_texte(pb, ctx)
        pb["contexte_fichiers"] = [x["fichier"] for x in ctx.get("extraits", [])]

        self.appels.append(time.monotonic())
        self.stats["appels_ia"] += 1
        session = async_get_clientsession(self.hass)
        try:
            res = await analyser(session, opts[CONF_API_KEY], opts[CONF_MODEL], texte)
        except MistralQuota:
            pb["statut"] = ST_QUOTA
            self._reprendre_plus_tard(pb["id"], 900)
            return
        except MistralCleInvalide as err:
            self.cle_invalide = True
            pb["statut"] = ST_ERREUR_IA
            pb["erreur"] = str(err)
            return
        except MistralErreur as err:
            pb["statut"] = ST_ERREUR_IA
            pb["erreur"] = str(err)[:300]
            return

        if pb.get("analyse"):
            pb["historique"].append(
                {"le": pb.get("analyse_le"), "resume": pb["analyse"].get("resume"),
                 "correctif": pb["analyse"].get("correctif", {}).get("type")}
            )
            pb["historique"] = pb["historique"][-5:]
        pb["analyse"] = res
        pb["analyse_le"] = _maintenant()
        pb.pop("erreur", None)

        correctif = res["correctif"]
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
        }
