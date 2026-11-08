// Panneau latéral « Correcteur en direct » — tient sur un seul écran (liste + détail)
const STATUTS = {
  "en_file": ["En attente", "#94a3b8"],
  "analyse": ["Analyse IA…", "#38bdf8"],
  "proposé": ["À valider", "#fbbf24"],
  "application": ["Application…", "#38bdf8"],
  "vérification": ["Vérification…", "#38bdf8"],
  "corrigé": ["Corrigé", "#4ade80"],
  "échec": ["Échec", "#f87171"],
  "sans_correctif": ["Pas de correctif auto", "#cbd5e1"],
  "erreur_ia": ["Erreur IA", "#f87171"],
  "quota": ["Pause (quota)", "#fbbf24"],
  "ignoré": ["Ignoré", "#94a3b8"],
  "récidive": ["Revenue", "#f87171"],
  "annulé": ["Annulé", "#cbd5e1"],
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
  modif_fichier: "Modifier le fichier",
  redemarrage: "Redémarrer Home Assistant",
  aucun: "Aucune action automatique",
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const espaces = (t) => String(t ?? "").split("\n").map((l) => {
  const m = l.match(/^[ \t]*/)[0];
  return `<span class="esp">${m.replace(/ /g, "·").replace(/\t/g, "→   ")}</span>${esc(l.slice(m.length))}`;
}).join("\n");
const court = (chemin) => String(chemin || "").replace(/^\/config\//, "");

function ilYa(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "à l'instant";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} j`;
}

class CorrecteurDirectPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._onglet = "traiter";
    this._choisi = null;
    this._dernier = "";
    this._donnees = null;
    this._ouvert = false; // détail ouvert (mode étroit)
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
  set narrow(n) { this._narrow = n; const b = this.shadowRoot?.getElementById("menu"); if (b) b.hidden = !n; }
  set panel(p) { this._panel = p; }
  disconnectedCallback() { clearInterval(this._minuteur); this._init = false; }
  connectedCallback() { if (this._hass && !this._init) this.hass = this._hass; }

  async _charger() {
    try {
      const d = await this._hass.callWS({ type: "correcteur_direct/liste" });
      this._donnees = d;
      const cle = JSON.stringify(d) + this._onglet + this._choisi;
      if (cle !== this._dernier) { this._dernier = cle; this._rendre(); }
      else this.shadowRoot.querySelectorAll("[data-iso]").forEach((e) => (e.textContent = ilYa(e.dataset.iso)));
    } catch (e) {
      this.shadowRoot.getElementById("liste").innerHTML = `<div class="vide">Intégration non chargée : ${esc(e.message || e)}</div>`;
    }
  }

  async _service(service, data = {}, retour = false) {
    try {
      const r = await this._hass.callWS({
        type: "call_service", domain: "correcteur_direct", service, service_data: data, return_response: retour,
      });
      const b = r?.response || {};
      if (service === "corriger_yaml") {
        if (!b.erreurs) this._toast("✅ Tous vos fichiers YAML sont valides");
        else this._toast(`${b.erreurs} fichier(s) en erreur : ${b.corriges} réparé(s), ${b.non_repares} à corriger à la main\n` + (b.details || []).join("\n"), b.non_repares > 0, 12000);
      } else if (service === "analyser_maintenant") {
        const n = b.nouveaux_problemes ?? 0;
        this._toast(n ? `${n} nouvelle(s) erreur(s) trouvée(s)` : "Aucune nouvelle erreur dans le journal");
      }
    } catch (e) {
      this._toast(e.message || String(e), true);
    }
    setTimeout(() => { this._dernier = ""; this._charger(); }, 400);
  }

  _toast(txt, erreur = false, duree = 5000) {
    const m = this.shadowRoot.getElementById("toast");
    m.textContent = txt;
    m.className = "toast visible" + (erreur ? " erreur" : "");
    clearTimeout(this._tMsg);
    this._tMsg = setTimeout(() => (m.className = "toast"), duree);
  }

  _squelette() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; height:100vh; overflow:hidden; background:#0f172a; color:#f1f5f9;
          font:16px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
        * { box-sizing:border-box; }
        .app { height:100vh; display:grid; grid-template-rows:auto auto 1fr; }
        button { font:inherit; font-weight:600; min-height:48px; min-width:48px; padding:0 14px; border-radius:12px;
          border:2px solid #24344d; background:#1e293b; color:#f1f5f9; cursor:pointer; white-space:nowrap; }
        button:hover, button:focus-visible { border-color:#38bdf8; outline:none; }
        button:disabled { opacity:.6; }
        .vert { background:#14532d; border-color:#4ade80; }
        .bleu { background:#0369a1; border-color:#38bdf8; }
        .rouge { background:#7f1d1d; border-color:#f87171; }

        /* Barre du haut : titre, compteurs, actions */
        header { display:flex; align-items:center; gap:10px; padding:8px 12px; background:#0b1222;
          border-bottom:2px solid #24344d; flex-wrap:wrap; }
        h1 { font-size:20px; margin:0 6px 0 0; white-space:nowrap; }
        .chiffres { display:flex; gap:8px; flex:1; flex-wrap:wrap; min-width:0; }
        .chiffre { display:flex; align-items:baseline; gap:6px; padding:6px 12px; border:2px solid #24344d;
          border-radius:12px; background:#111c30; font-size:15px; color:#cbd5e1; min-height:48px; align-items:center; }
        .chiffre b { font:700 22px ui-monospace, Consolas, monospace; color:#f1f5f9; }
        .actions-haut { display:flex; gap:8px; }

        /* Onglets */
        nav { display:flex; gap:8px; padding:8px 12px; border-bottom:2px solid #24344d; overflow-x:auto; }
        nav button[aria-selected="true"] { background:#38bdf8; color:#0f172a; border-color:#38bdf8; }

        /* Corps : liste | détail */
        main { display:grid; grid-template-columns:minmax(300px, 40%) 1fr; min-height:0; }
        #liste { overflow-y:auto; border-right:2px solid #24344d; padding:8px; display:flex; flex-direction:column; gap:6px; }
        .ligne { flex-shrink:0; display:grid; grid-template-columns:8px 1fr auto; gap:10px; align-items:center; text-align:left;
          min-height:64px; padding:8px 10px 8px 0; border-radius:12px; background:#111c30; font-weight:400; white-space:normal; }
        .ligne[aria-current="true"] { border-color:#38bdf8; background:#13233d; }
        .barre { align-self:stretch; border-radius:12px 0 0 12px; }
        .titre { font-weight:700; font-size:16px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .sous { font-size:14px; color:#cbd5e1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .statut { font-size:14px; font-weight:700; padding:3px 10px; border-radius:999px; border:2px solid currentColor; white-space:nowrap; }
        .resume { min-width:0; }
        .raison { color:#fca5a5; }

        #detail { overflow-y:auto; padding:14px 18px; }
        #detail h2 { font-size:20px; margin:4px 0 8px; }
        .badges { display:flex; gap:8px; flex-wrap:wrap; }
        .fichier { margin:10px 0; padding:8px 12px; background:#020617; border:2px solid #38bdf8; border-radius:10px;
          font:15px/1.5 ui-monospace, Consolas, monospace; word-break:break-all; }
        .fichier small { font:14px system-ui, sans-serif; color:#94a3b8; }
        h3 { font-size:14px; text-transform:uppercase; letter-spacing:.05em; color:#38bdf8; margin:12px 0 4px; }
        pre { white-space:pre-wrap; word-break:break-word; font:15px/1.45 ui-monospace, Consolas, monospace;
          background:#020617; border:2px solid #24344d; border-radius:10px; padding:8px 10px; margin:4px 0; max-height:30vh; overflow:auto; }
        pre.moins { border-color:#f87171; } pre.plus { border-color:#4ade80; }
        .esp { color:#fbbf24; }
        .meta { font-size:14px; color:#cbd5e1; }
        .alerte { color:#fca5a5; margin-top:8px; }
        .boutons { display:flex; gap:8px; flex-wrap:wrap; margin:14px 0 6px; position:sticky; bottom:0;
          background:#0f172a; padding:8px 0; }
        details summary { cursor:pointer; min-height:48px; display:flex; align-items:center; color:#38bdf8; font-weight:600; }
        .vide { text-align:center; color:#cbd5e1; padding:40px 10px; font-size:17px; }
        #retour { display:none; }
        .bandeau { padding:10px 14px; margin:8px 12px 0; border:2px solid #fbbf24; border-radius:12px; background:#422006; font-size:15px; }

        .toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:#0369a1; border:2px solid #38bdf8;
          padding:12px 20px; border-radius:12px; font-size:16px; white-space:pre-line; opacity:0; pointer-events:none;
          transition:opacity .2s; z-index:5; max-width:90vw; }
        .toast.visible { opacity:1; } .toast.erreur { background:#7f1d1d; border-color:#f87171; }

        /* Écran étroit : la liste occupe tout, le détail s'ouvre par-dessus */
        @media (max-width: 760px) {
          main { grid-template-columns:1fr; }
          #liste { border-right:none; }
          #detail { display:none; }
          :host([ouvert]) #liste { display:none; }
          :host([ouvert]) #detail { display:block; }
          #retour { display:inline-block; }
          .actions-haut button span { display:none; }
          h1 { flex:1; }
          .chiffres { order:3; flex-basis:100%; flex-wrap:nowrap; }
          .chiffre { flex:1; justify-content:center; padding:4px 8px; }
        }
      </style>
      <div class="app">
        <header>
          <button id="menu" hidden aria-label="Menu">☰</button>
          <h1>🔧 Correcteur</h1>
          <div class="chiffres" id="chiffres"></div>
          <div class="actions-haut">
            <button id="yaml" class="vert" title="Corriger les erreurs YAML">🛠️ <span>Corriger YAML</span></button>
            <button id="analyser" class="bleu" title="Analyser le journal">🔍 <span>Journal</span></button>
            <button id="tester" title="Écrire une fausse erreur pour tester">🧪 <span>Tester</span></button>
          </div>
        </header>
        <div>
          <div id="bandeau"></div>
          <nav id="onglets"></nav>
        </div>
        <main>
          <div id="liste"><div class="vide">Chargement…</div></div>
          <div id="detail"></div>
        </main>
      </div>
      <div id="toast" class="toast" role="status"></div>`;

    const r = this.shadowRoot;
    r.getElementById("menu").onclick = () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    r.getElementById("analyser").onclick = () => this._service("analyser_maintenant", {}, true);
    r.getElementById("tester").onclick = () => { this._service("tester"); this._toast("Fausse erreur écrite : elle doit apparaître dans quelques secondes"); };
    r.getElementById("yaml").onclick = () => {
      if (!confirm("Vérifier tous les fichiers YAML et réparer les erreurs de syntaxe ?\n\nUne sauvegarde est faite avant chaque modification.")) return;
      const b = r.getElementById("yaml");
      b.disabled = true;
      this._service("corriger_yaml", { appliquer: true }, true).finally(() => (b.disabled = false));
    };
    r.getElementById("liste").addEventListener("click", (ev) => {
      const l = ev.target.closest("[data-choix]");
      if (!l) return;
      this._choisi = l.dataset.choix;
      this._ouvrir(true);
      this._dernier = ""; this._rendre();
    });
    r.getElementById("detail").addEventListener("click", (ev) => {
      if (ev.target.closest("#retour")) { this._ouvrir(false); return; }
      const b = ev.target.closest("button[data-action]");
      if (!b) return;
      const { action, id } = b.dataset;
      if (action === "redemarrer" && !confirm("Redémarrer Home Assistant maintenant ?")) return;
      if (action === "appliquer_fichier" && !confirm("Modifier le fichier ? Une sauvegarde est faite avant.")) return;
      const service = { appliquer: "appliquer_correctif", appliquer_fichier: "appliquer_correctif", redemarrer: "appliquer_correctif",
        ignorer: "ignorer", reactiver: "reactiver", reanalyser: "reanalyser", annuler: "annuler_correctif" }[action];
      b.disabled = true;
      this._service(service, { id });
    });
  }

  _ouvrir(oui) {
    this._ouvert = oui;
    oui ? this.setAttribute("ouvert", "") : this.removeAttribute("ouvert");
  }

  _rendre() {
    const d = this._donnees;
    if (!d) return;
    const r = this.shadowRoot;
    const pbs = d.problemes || [];
    const compte = (sts) => pbs.filter((p) => sts.includes(p.statut)).length;
    const s = d.stats || {};

    r.getElementById("chiffres").innerHTML = [
      [compte(ONGLETS[0][2]), "à traiter"],
      [s.corrections || 0, "corrigées"],
      [`${s.appels_derniere_heure || 0}/${d.options.appels_max_heure}`, "IA / h"],
    ].map(([v, l]) => `<div class="chiffre"><b>${esc(v)}</b>${esc(l)}</div>`).join("");

    const ia = d.ia || {};
    const minutes = (sec) => sec >= 60 ? `${Math.floor(sec / 60)} min ${sec % 60} s` : `${sec} s`;
    let bandeau = "";
    if (d.cle_invalide) {
      bandeau = `<div class="bandeau">⚠️ Clé Mistral refusée : Paramètres → Appareils et services → Correcteur en direct → Configurer.</div>`;
    } else if (ia.derniere_erreur && (ia.pause_restante > 0 || ia.en_file > 0)) {
      bandeau = `<div class="bandeau">⏸️ <b>Analyses IA ralenties</b> — ${ia.en_file} en attente` +
        (ia.pause_restante > 0 ? ` · reprise dans ${minutes(ia.pause_restante)}` : "") +
        ` · un appel toutes les ${ia.intervalle} s<br>${esc(ia.derniere_erreur.message)}</div>`;
    }
    r.getElementById("bandeau").innerHTML = bandeau;

    r.getElementById("onglets").innerHTML = ONGLETS.map(([cle, nom, sts]) =>
      `<button data-onglet="${cle}" aria-selected="${cle === this._onglet}">${nom} (${compte(sts)})</button>`).join("");
    r.querySelectorAll("[data-onglet]").forEach((b) => (b.onclick = () => {
      this._onglet = b.dataset.onglet; this._choisi = null; this._ouvrir(false); this._dernier = ""; this._rendre();
    }));

    const sts = ONGLETS.find((x) => x[0] === this._onglet)[2];
    const liste = pbs.filter((p) => sts.includes(p.statut));
    if (!liste.find((p) => p.id === this._choisi)) this._choisi = liste[0]?.id || null;

    r.getElementById("liste").innerHTML = liste.length ? liste.map((p) => {
      const a = p.analyse || {};
      const [stNom, stCoul] = STATUTS[p.statut] || [p.statut, "#cbd5e1"];
      const grCoul = (GRAVITE[a.gravite] || ["", "#475569"])[1];
      const f = (p.fichiers || [])[0];
      const ou = f ? `📄 ${court(f.chemin)}${f.ligne ? ` : ${f.ligne}` : ""}` : esc(p.nom);
      return `<button class="ligne" data-choix="${p.id}" aria-current="${p.id === this._choisi}">
        <span class="barre" style="background:${grCoul}"></span>
        <span class="resume"><div class="titre">${esc(a.resume || p.message.split("\n")[0])}</div>
          <div class="sous">${esc(ou)} · ${p.occurrences}× · <span data-iso="${esc(p.dernier)}">${ilYa(p.dernier)}</span></div>
          ${["quota", "erreur_ia", "échec"].includes(p.statut) && p.erreur ? `<div class="sous raison">⚠️ ${esc(p.erreur)}</div>` : ""}</span>
        <span class="statut" style="color:${stCoul}">${stNom}</span>
      </button>`;
    }).join("") : `<div class="vide">${this._onglet === "traiter" ? "✅ Aucune erreur à traiter.<br>Le journal est surveillé en direct." : "Rien ici."}</div>`;

    const p = liste.find((x) => x.id === this._choisi);
    r.getElementById("detail").innerHTML = p ? this._detail(p) : `<div class="vide">Choisissez une erreur dans la liste.</div>`;
  }

  _detail(p) {
    const a = p.analyse || {};
    const c = a.correctif || {};
    const [stNom, stCoul] = STATUTS[p.statut] || [p.statut, "#cbd5e1"];
    const [grNom, grCoul] = GRAVITE[a.gravite] || ["Non analysée", "#94a3b8"];

    const fichiers = (p.fichiers || []).map((f) =>
      `<div>📄 ${esc(f.chemin)}${f.ligne ? ` — <b>ligne ${f.ligne}</b>` : ""} <small>(${esc(f.origine)})</small></div>`).join("");

    let correctif = "";
    if (c.type && c.type !== "aucun") {
      let detail = "";
      if (c.type === "modif_fichier") {
        detail = `<div class="meta">Remplacer (· = un espace) :</div><pre class="moins">${espaces(c.rechercher)}</pre>
          <div class="meta">Par :</div><pre class="plus">${espaces(c.remplacer)}</pre>`;
      } else if (c.type === "appel_service") {
        detail = `<pre>${esc(c.domaine)}.${esc(c.service)} ${esc(JSON.stringify(c.donnees || {}))}</pre>`;
      } else if (c.type === "recharger_integration") {
        detail = `<pre>Recharger : ${esc(c.domaine)}</pre>`;
      }
      correctif = `<h3>Correctif · ${esc(TYPES[c.type] || c.type)} · confiance ${Math.round((a.confiance || 0) * 100)} %</h3>
        <div>${esc(a.explication)}</div>${detail}`;
    } else if (a.explication) {
      correctif = `<h3>Ce qu'il faut faire</h3><div>${esc(a.explication)}</div>`;
    }

    const btn = (action, txt, cls = "") => `<button class="${cls}" data-action="${action}" data-id="${p.id}">${txt}</button>`;
    const actions = [];
    if (c.type && c.type !== "aucun" && ["proposé", "échec", "récidive", "annulé"].includes(p.statut)) {
      if (c.type === "redemarrage") actions.push(btn("redemarrer", "🔄 Redémarrer HA", "rouge"));
      else if (c.type === "modif_fichier") actions.push(btn("appliquer_fichier", "✅ Appliquer", "vert"));
      else actions.push(btn("appliquer", "✅ Appliquer", "vert"));
    }
    if (p.statut === "corrigé" && p.retour_arriere && p.retour_arriere.type === "fichier") actions.push(btn("annuler", "↩️ Annuler", "rouge"));
    if (!["analyse", "application", "vérification", "en_file", "ignoré"].includes(p.statut) && p.type !== "yaml") actions.push(btn("reanalyser", "🔁 Réanalyser"));
    if (p.statut === "ignoré") actions.push(btn("reactiver", "👁️ Réactiver"));
    else if (!["application", "vérification"].includes(p.statut)) actions.push(btn("ignorer", "🙈 Ignorer"));

    return `<button id="retour">← Liste</button>
      <div class="badges">
        <span class="statut" style="color:${grCoul}">${grNom}</span>
        <span class="statut" style="color:${stCoul}">${stNom}</span>
        ${p.auto ? `<span class="statut" style="color:#38bdf8">Automatique</span>` : ""}
      </div>
      <h2>${esc(a.resume || p.message.split("\n")[0])}</h2>
      <div class="meta">${esc(p.nom)} · ${p.occurrences} fois · vue <span data-iso="${esc(p.dernier)}">${ilYa(p.dernier)}</span></div>
      ${fichiers ? `<div class="fichier">${fichiers}</div>` : ""}
      ${p.erreur ? `<div class="alerte">⚠️ ${esc(p.erreur)}</div>` : ""}
      ${a.cause ? `<h3>Cause</h3><div>${esc(a.cause)}</div>` : ""}
      ${correctif}
      <div class="boutons">${actions.join("")}</div>
      <details><summary>Message d'origine</summary>
        <pre>${esc(p.message)}</pre>${p.exception ? `<pre>${esc(p.exception)}</pre>` : ""}
        <div class="meta">Code : ${esc(p.source)} · identifiant ${p.id}${p.retour_arriere?.sauvegarde ? ` · sauvegarde ${esc(p.retour_arriere.sauvegarde)}` : ""}</div>
      </details>`;
  }
}

if (!customElements.get("correcteur-direct-panel")) {
  customElements.define("correcteur-direct-panel", CorrecteurDirectPanel);
}
