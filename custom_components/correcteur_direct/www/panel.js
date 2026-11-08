// Panneau latéral « Correcteur en direct »
const STATUTS = {
  "en_file": ["En attente d'analyse", "#94a3b8"],
  "analyse": ["Analyse par Mistral…", "#38bdf8"],
  "proposé": ["Correctif proposé", "#fbbf24"],
  "application": ["Application…", "#38bdf8"],
  "vérification": ["Vérification en cours", "#38bdf8"],
  "corrigé": ["Corrigé", "#4ade80"],
  "échec": ["Échec", "#f87171"],
  "sans_correctif": ["Pas de correctif automatique", "#cbd5e1"],
  "erreur_ia": ["Erreur IA", "#f87171"],
  "quota": ["Quota atteint, reprise plus tard", "#fbbf24"],
  "ignoré": ["Ignoré", "#94a3b8"],
  "récidive": ["Erreur revenue", "#f87171"],
  "annulé": ["Correctif annulé", "#cbd5e1"],
};
const GRAVITE = { critique: ["Critique", "#f87171"], moyenne: ["Moyenne", "#fbbf24"], faible: ["Faible", "#4ade80"] };
const ONGLETS = [
  ["traiter", "À traiter", ["en_file", "analyse", "proposé", "application", "vérification", "échec", "quota", "récidive"]],
  ["corriges", "Corrigés", ["corrigé", "annulé"]],
  ["sans", "Sans correctif", ["sans_correctif", "erreur_ia"]],
  ["ignores", "Ignorés", ["ignoré"]],
];
const TYPES = {
  recharger_integration: "Recharger l'intégration",
  appel_service: "Appeler un service",
  modif_fichier: "Modifier un fichier YAML",
  redemarrage: "Redémarrer Home Assistant",
  aucun: "Aucune action automatique",
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Rend visibles les espaces et tabulations de début de ligne (indentation)
const espaces = (t) => String(t ?? "").split("\n").map((l) => {
  const m = l.match(/^[ \t]*/)[0];
  const marques = m.replace(/ /g, "·").replace(/\t/g, "→   ");
  return `<span class="esp">${marques}</span>${esc(l.slice(m.length))}`;
}).join("\n");

function ilYa(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "à l'instant";
  if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
  return `il y a ${Math.floor(s / 86400)} j`;
}

class CorrecteurDirectPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._onglet = "traiter";
    this._ouverts = new Set();
    this._dernier = "";
    this._donnees = null;
    this._message = "";
  }

  set hass(h) {
    this._hass = h;
    if (!this._init) {
      this._init = true;
      this._squelette();
      this._charger();
      this._minuteur = setInterval(() => this._charger(), 3000);
    }
  }
  set narrow(n) { this._narrow = n; this._majMenu(); }
  set panel(p) { this._panel = p; }

  disconnectedCallback() { clearInterval(this._minuteur); this._init = false; }
  connectedCallback() { if (this._hass && !this._init) this.hass = this._hass; }

  async _charger() {
    try {
      const d = await this._hass.callWS({ type: "correcteur_direct/liste" });
      const cle = JSON.stringify(d);
      this._donnees = d;
      if (cle !== this._dernier) { this._dernier = cle; this._rendre(); }
      else this._majTemps();
    } catch (e) {
      this._donnees = null;
      this.shadowRoot.getElementById("liste").innerHTML =
        `<div class="vide">Intégration non chargée : ${esc(e.message || e)}</div>`;
    }
  }

  async _service(service, data = {}, retour = false) {
    try {
      const r = await this._hass.callWS({
        type: "call_service", domain: "correcteur_direct", service,
        service_data: data, return_response: retour,
      });
      if (service === "corriger_yaml") {
        const b = r?.response || {};
        if (!b.erreurs) this._afficherMessage("✅ Tous vos fichiers YAML sont valides", false, 6000);
        else this._afficherMessage(
          `${b.erreurs} fichier(s) en erreur : ${b.corriges} réparé(s), ${b.non_repares} à corriger à la main\n` +
          (b.details || []).join("\n"), b.non_repares > 0, 12000);
      }
      if (service === "analyser_maintenant") {
        const n = r?.response?.nouveaux_problemes ?? 0;
        this._afficherMessage(n ? `${n} nouvelle(s) erreur(s) trouvée(s)` : "Aucune nouvelle erreur dans le journal");
      }
    } catch (e) {
      this._afficherMessage(e.message || String(e), true);
    }
    setTimeout(() => this._charger(), 400);
  }

  _afficherMessage(txt, erreur = false, duree = 5000) {
    const m = this.shadowRoot.getElementById("message");
    m.textContent = txt;
    m.className = "message visible" + (erreur ? " erreur" : "");
    clearTimeout(this._tMsg);
    this._tMsg = setTimeout(() => (m.className = "message"), duree);
  }

  _majMenu() {
    const b = this.shadowRoot?.getElementById("menu");
    if (b) b.style.display = this._narrow ? "flex" : "none";
  }

  _squelette() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; min-height:100vh; background:#0f172a; color:#f1f5f9;
          font: 17px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
        * { box-sizing:border-box; }
        header { position:sticky; top:0; z-index:2; background:#0b1222; border-bottom:2px solid #24344d;
          display:flex; align-items:center; gap:14px; padding:12px 18px; flex-wrap:wrap; }
        h1 { font-size:24px; margin:0; flex:1; min-width:200px; }
        h1 small { display:block; font-size:15px; color:#94a3b8; font-weight:400; }
        button { font:inherit; font-size:17px; font-weight:600; min-height:52px; min-width:52px; padding:0 20px;
          border-radius:14px; border:2px solid #24344d; background:#1e293b; color:#f1f5f9; cursor:pointer; }
        button:hover, button:focus-visible { border-color:#38bdf8; outline:none; }
        button.principal { background:#0369a1; border-color:#38bdf8; }
        button.danger { background:#7f1d1d; border-color:#f87171; }
        button.vert { background:#14532d; border-color:#4ade80; }
        #menu { display:none; align-items:center; justify-content:center; font-size:26px; padding:0; }
        .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; padding:16px 18px 4px; }
        .stat { background:#111c30; border:2px solid #24344d; border-radius:14px; padding:12px 16px; }
        .stat b { display:block; font-size:30px; font-family:ui-monospace, Consolas, monospace; }
        .stat span { font-size:15px; color:#cbd5e1; }
        nav { display:flex; gap:10px; padding:14px 18px; flex-wrap:wrap; }
        nav button[aria-selected="true"] { background:#38bdf8; color:#0f172a; border-color:#38bdf8; }
        #liste { padding:0 18px 40px; display:flex; flex-direction:column; gap:14px; }
        .carte { background:#111c30; border:2px solid #24344d; border-left-width:8px; border-radius:14px; padding:16px 18px; }
        .ligne1 { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:6px; }
        .badge { font-size:15px; font-weight:700; padding:4px 12px; border-radius:999px; border:2px solid currentColor; }
        .titre { font-size:20px; font-weight:700; margin:4px 0 6px; }
        .meta { font-size:15px; color:#cbd5e1; }
        .bloc { margin-top:10px; }
        .bloc h3 { font-size:15px; text-transform:uppercase; letter-spacing:.05em; color:#38bdf8; margin:0 0 4px; }
        pre { white-space:pre-wrap; word-break:break-word; font:15px/1.45 ui-monospace, Consolas, monospace;
          background:#020617; border:2px solid #24344d; border-radius:10px; padding:10px 12px; margin:4px 0; max-height:340px; overflow:auto; }
        .esp { color:#fbbf24; }
        pre.moins { border-color:#f87171; } pre.plus { border-color:#4ade80; }
        details summary { cursor:pointer; min-height:48px; display:flex; align-items:center; font-weight:600; color:#38bdf8; font-size:17px; }
        .actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
        .fichiers { margin-top:10px; padding:10px 14px; background:#020617; border:2px solid #38bdf8; border-radius:10px; }
        .fichiers div { font:16px/1.5 ui-monospace, Consolas, monospace; word-break:break-all; }
        .fichiers small { font-family:system-ui, sans-serif; font-size:14px; color:#94a3b8; }
        .vide { text-align:center; color:#cbd5e1; padding:50px 10px; font-size:19px; }
        .alerte { margin:12px 18px 0; padding:14px 16px; border:2px solid #f87171; border-radius:14px; background:#450a0a; }
        .message { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#0369a1; border:2px solid #38bdf8;
          padding:14px 22px; border-radius:14px; font-size:17px; white-space:pre-line; opacity:0; pointer-events:none; transition:opacity .2s; z-index:5; max-width:90vw; }
        .message.visible { opacity:1; } .message.erreur { background:#7f1d1d; border-color:#f87171; }
        .reglages { font-size:15px; color:#cbd5e1; padding:0 18px; }
      </style>
      <header>
        <button id="menu" aria-label="Menu">☰</button>
        <h1>🔧 Correcteur en direct<small>Erreurs détectées à l'instant où elles apparaissent · Mistral AI</small></h1>
        <button id="yaml" class="vert">🛠️ Corriger les erreurs YAML</button>
        <button id="analyser" class="principal">🔍 Analyser le journal</button>
        <button id="tester">🧪 Tester</button>
      </header>
      <div id="alerte"></div>
      <div class="stats" id="stats"></div>
      <div class="reglages" id="reglages"></div>
      <nav id="onglets"></nav>
      <div id="liste"><div class="vide">Chargement…</div></div>
      <div id="message" class="message" role="status"></div>`;
    const r = this.shadowRoot;
    r.getElementById("menu").onclick = () =>
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    r.getElementById("analyser").onclick = () => this._service("analyser_maintenant", {}, true);
    r.getElementById("yaml").onclick = () => {
      if (!confirm("Vérifier tous les fichiers YAML et réparer les erreurs de syntaxe ?\n\nUne sauvegarde est faite avant chaque modification, et chaque réparation peut être annulée.")) return;
      const b = r.getElementById("yaml");
      b.disabled = true; b.textContent = "⏳ Vérification…";
      this._service("corriger_yaml", { appliquer: true }, true).finally(() => {
        b.disabled = false; b.textContent = "🛠️ Corriger les erreurs YAML";
      });
    };
    r.getElementById("tester").onclick = () => {
      this._service("tester");
      this._afficherMessage("Fausse erreur écrite dans le journal : elle doit apparaître ici en quelques secondes");
    };
    r.getElementById("liste").addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-action]");
      if (!b) return;
      const { action, id } = b.dataset;
      if (action === "redemarrer" && !confirm("Redémarrer Home Assistant maintenant ?")) return;
      if (action === "appliquer_fichier" && !confirm("Modifier le fichier YAML ? Une sauvegarde est faite avant.")) return;
      const service = { appliquer: "appliquer_correctif", appliquer_fichier: "appliquer_correctif", redemarrer: "appliquer_correctif",
        ignorer: "ignorer", reactiver: "reactiver", reanalyser: "reanalyser", annuler: "annuler_correctif" }[action];
      b.disabled = true;
      this._service(service, { id });
    });
    r.getElementById("liste").addEventListener("toggle", (ev) => {
      const d = ev.target;
      if (d.tagName !== "DETAILS") return;
      d.open ? this._ouverts.add(d.dataset.cle) : this._ouverts.delete(d.dataset.cle);
    }, true);
    this._majMenu();
  }

  _rendre() {
    const d = this._donnees;
    if (!d) return;
    const r = this.shadowRoot;
    const pbs = d.problemes || [];
    const compte = (sts) => pbs.filter((p) => sts.includes(p.statut)).length;

    r.getElementById("alerte").innerHTML = d.cle_invalide
      ? `<div class="alerte">⚠️ La clé API Mistral est refusée. Corrigez-la dans Paramètres → Appareils et services → Correcteur en direct → Configurer.</div>` : "";

    const s = d.stats || {};
    r.getElementById("stats").innerHTML = [
      [compte(ONGLETS[0][2]), "à traiter"],
      [s.corrections || 0, "corrections réussies"],
      [s.echecs || 0, "échecs (annulés)"],
      [`${s.appels_derniere_heure || 0}/${d.options.appels_max_heure}`, "appels IA cette heure"],
    ].map(([v, l]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");

    const o = d.options;
    r.getElementById("reglages").textContent =
      `Modèle ${o.modele} · surveille ${o.niveau === "WARNING" ? "erreurs + avertissements" : "les erreurs"} · ` +
      `rechargements auto : ${o.auto_services ? "oui" : "non"} · fichiers auto : ${o.auto_fichiers ? "oui" : "non (validation)"} · ` +
      `vérification ${o.delai_verification} s · v${d.version}`;

    r.getElementById("onglets").innerHTML = ONGLETS.map(([cle, nom, sts]) =>
      `<button role="tab" data-onglet="${cle}" aria-selected="${cle === this._onglet}">${nom} (${compte(sts)})</button>`).join("");
    r.querySelectorAll("[data-onglet]").forEach((b) => (b.onclick = () => { this._onglet = b.dataset.onglet; this._rendre(); }));

    const sts = ONGLETS.find((x) => x[0] === this._onglet)[2];
    const liste = pbs.filter((p) => sts.includes(p.statut));
    r.getElementById("liste").innerHTML = liste.length
      ? liste.map((p) => this._carte(p)).join("")
      : `<div class="vide">${this._onglet === "traiter" ? "✅ Aucune erreur à traiter. Le journal est surveillé en direct." : "Rien ici pour l'instant."}</div>`;
  }

  _majTemps() {
    this.shadowRoot.querySelectorAll("[data-iso]").forEach((e) => (e.textContent = ilYa(e.dataset.iso)));
  }

  _carte(p) {
    const a = p.analyse || {};
    const c = a.correctif || {};
    const [stNom, stCoul] = STATUTS[p.statut] || [p.statut, "#cbd5e1"];
    const [grNom, grCoul] = GRAVITE[a.gravite] || ["Non analysée", "#94a3b8"];
    const titre = a.resume || p.message.split("\n")[0].slice(0, 140);
    const ouvert = (k) => (this._ouverts.has(`${p.id}-${k}`) ? "open" : "");

    let correctif = "";
    if (c.type && c.type !== "aucun") {
      let detail = "";
      if (c.type === "modif_fichier") {
        detail = `<div class="meta">Fichier modifié : <b>${esc(c.chemin_complet || c.fichier)}</b>${c.rechargement ? ` · puis rechargement ${esc(c.rechargement)}` : ""}</div>
          <div class="meta">Remplacer (· = un espace) :</div><pre class="moins">${espaces(c.rechercher)}</pre>
          <div class="meta">Par :</div><pre class="plus">${espaces(c.remplacer)}</pre>`;
      } else if (c.type === "appel_service") {
        detail = `<pre>${esc(c.domaine)}.${esc(c.service)} ${esc(JSON.stringify(c.donnees || {}))}</pre>`;
      } else if (c.type === "recharger_integration") {
        detail = `<pre>Recharger : ${esc(c.domaine)}</pre>`;
      }
      correctif = `<div class="bloc"><h3>Correctif · ${esc(TYPES[c.type] || c.type)} · confiance ${Math.round((a.confiance || 0) * 100)} %</h3>
        <div>${esc(a.explication)}</div>${detail}</div>`;
    } else if (a.explication) {
      correctif = `<div class="bloc"><h3>Ce qu'il faut faire</h3><div>${esc(a.explication)}</div></div>`;
    }

    const btn = (action, txt, cls = "") => `<button class="${cls}" data-action="${action}" data-id="${p.id}">${txt}</button>`;
    const actions = [];
    const peutAppliquer = c.type && c.type !== "aucun" && ["proposé", "échec", "récidive", "annulé"].includes(p.statut);
    if (peutAppliquer) {
      if (c.type === "redemarrage") actions.push(btn("redemarrer", "🔄 Redémarrer HA", "danger"));
      else if (c.type === "modif_fichier") actions.push(btn("appliquer_fichier", "✅ Appliquer la modification", "vert"));
      else actions.push(btn("appliquer", "✅ Appliquer", "vert"));
    }
    if (p.statut === "corrigé" && p.retour_arriere && p.retour_arriere.type === "fichier")
      actions.push(btn("annuler", "↩️ Annuler la modification", "danger"));
    if (!["analyse", "application", "vérification", "en_file", "ignoré"].includes(p.statut))
      actions.push(btn("reanalyser", "🔁 Réanalyser"));
    if (p.statut === "ignoré") actions.push(btn("reactiver", "👁️ Réactiver"));
    else if (!["application", "vérification"].includes(p.statut)) actions.push(btn("ignorer", "🙈 Ignorer ce type d'erreur"));

    return `<article class="carte" style="border-left-color:${grCoul}">
      <div class="ligne1">
        <span class="badge" style="color:${grCoul}">${grNom}</span>
        <span class="badge" style="color:${stCoul}">${stNom}</span>
        ${p.auto ? `<span class="badge" style="color:#38bdf8">Automatique</span>` : ""}
      </div>
      <div class="titre">${esc(titre)}</div>
      <div class="meta">${esc(p.nom)} · ${p.occurrences} occurrence(s) · vue <span data-iso="${esc(p.dernier)}">${ilYa(p.dernier)}</span></div>
      ${(p.fichiers || []).length ? `<div class="fichiers">${p.fichiers.map((f) =>
        `<div>📄 ${esc(f.chemin)}${f.ligne ? ` — <b>ligne ${f.ligne}</b>` : ""} <small>(${esc(f.origine)})</small></div>`).join("")}</div>`
        : (p.analyse ? `<div class="meta bloc">📄 Aucun fichier YAML concerné (configuration faite par l'interface ou intégration)</div>` : "")}
      ${p.erreur ? `<div class="bloc" style="color:#fca5a5">⚠️ ${esc(p.erreur)}</div>` : ""}
      ${a.cause ? `<div class="bloc"><h3>Cause</h3><div>${esc(a.cause)}</div></div>` : ""}
      ${correctif}
      <details data-cle="${p.id}-msg" ${ouvert("msg")}><summary>Message d'origine</summary>
        <pre>${esc(p.message)}</pre>${p.exception ? `<pre>${esc(p.exception)}</pre>` : ""}
        <div class="meta">Code Home Assistant concerné : ${esc(p.source)}</div>
        <div class="meta">Identifiant : ${p.id}${p.retour_arriere?.sauvegarde ? ` · sauvegarde : ${esc(p.retour_arriere.sauvegarde)}` : ""}</div>
      </details>
      <div class="actions">${actions.join("")}</div>
    </article>`;
  }
}

if (!customElements.get("correcteur-direct-panel")) {
  customElements.define("correcteur-direct-panel", CorrecteurDirectPanel);
}
