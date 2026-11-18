/* NOMAIA — coquille : preferences d'affichage, chronometre global, palette de commandes.
 *
 * Preferences (theme, fond anime, animations reduites) : propres a chaque
 * appareil, gardees dans localStorage et appliquees avant le premier rendu
 * par le script en tete de layout.html.
 *
 * Palette (Ctrl+K, ou « / » hors d'un champ) : les resultats viennent de
 * fournisseurs enregistres par J.palette.register(). Un fournisseur expose
 * search(requete) -> liste (ou promesse de liste) d'elements
 * { label, sub, kind, glyph, url | run }. La recherche dans les donnees
 * (lot 3) s'y branche sans toucher a ce fichier.
 */
(function () {
  const J = window.J;
  const root = document.documentElement;

  /* ---------- Preferences de cet appareil ---------- */

  const PREFS = {
    theme: { key: "nomaia.theme", attr: "theme", fallback: "dark" },
    ambient: { key: "nomaia.ambient", attr: "ambient", fallback: "off" },
    motion: { key: "nomaia.motion", attr: "motion", fallback: "full" },
  };
  const THEME_COLORS = { dark: "#060910", light: "#f3f6fa" };

  function getPref(name) {
    const pref = PREFS[name];
    try {
      return localStorage.getItem(pref.key) || pref.fallback;
    } catch (error) {
      return root.dataset[pref.attr] || pref.fallback;
    }
  }

  function setPref(name, value) {
    const pref = PREFS[name];
    root.dataset[pref.attr] = value;
    if (name === "theme") {
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.content = THEME_COLORS[value] || THEME_COLORS.dark;
    }
    try {
      localStorage.setItem(pref.key, value);
    } catch (error) {
      // Stockage indisponible : le choix vaut pour la page ouverte seulement.
    }
    document.dispatchEvent(new CustomEvent("nomaia:prefs", { detail: { name, value } }));
  }

  function toggleTheme() {
    setPref("theme", getPref("theme") === "light" ? "dark" : "light");
  }

  root.dataset.theme = getPref("theme");
  setPref("theme", getPref("theme"));

  const themeButton = document.getElementById("theme-toggle");
  if (themeButton) themeButton.addEventListener("click", toggleTheme);

  /* Interrupteurs de preference poses dans les pages (Config > Affichage) :
   * <button class="switch" data-pref="ambient" data-on="on" data-off="off">. */
  function syncSwitches() {
    document.querySelectorAll("[data-pref]").forEach((element) => {
      const on = getPref(element.dataset.pref) === element.dataset.on;
      element.setAttribute("aria-checked", String(on));
    });
  }

  document.addEventListener("click", (event) => {
    const element = event.target.closest("[data-pref]");
    if (!element) return;
    const on = getPref(element.dataset.pref) === element.dataset.on;
    setPref(element.dataset.pref, on ? element.dataset.off : element.dataset.on);
  });
  document.addEventListener("nomaia:prefs", syncSwitches);
  syncSwitches();

  /* ---------- Chronometre global ---------- */

  const pill = document.getElementById("chrono-pill");
  const pillTitle = document.getElementById("chrono-title");
  const pillTime = document.getElementById("chrono-time");
  const pillStop = document.getElementById("chrono-stop");
  let session = null;
  let tickTimer = null;

  function startedAt(value) {
    const moment = new Date(String(value || "").replace(" ", "T"));
    return Number.isNaN(moment.getTime()) ? null : moment;
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function renderClock() {
    if (!session) return;
    const start = startedAt(session.started_at);
    const seconds = start ? Math.max(0, Math.floor((Date.now() - start.getTime()) / 1000)) : 0;
    pillTime.textContent =
      pad(Math.floor(seconds / 3600)) + ":" + pad(Math.floor(seconds / 60) % 60) + ":" + pad(seconds % 60);
  }

  function renderSession(next) {
    session = next || null;
    clearInterval(tickTimer);
    if (!pill) return;
    pill.hidden = !session;
    if (!session) return;
    pillTitle.textContent = session.task_title || "Tâche en cours";
    pillTitle.title = session.task_title || "";
    renderClock();
    tickTimer = setInterval(renderClock, 1000);
  }

  async function refreshTimer() {
    try {
      renderSession((await J.callApi("/api/tasks/timer")).session);
    } catch (error) {
      // Serveur momentanement injoignable : l'etat precedent reste affiche.
    }
  }

  async function stopTimer() {
    try {
      const data = await J.callApi("/api/tasks/timer/stop", { method: "POST" });
      renderSession(null);
      J.toast("Chronomètre arrêté : " + J.formatDuration(Math.max(1, data.duration_min || 0)));
      document.dispatchEvent(new CustomEvent("nomaia:timer"));
    } catch (error) {
      J.toast(error.message, true);
    }
  }

  if (pill) {
    pillStop.addEventListener("click", stopTimer);
    // Les pages qui demarrent ou arretent un chronometre emettent nomaia:timer.
    document.addEventListener("nomaia:timer", refreshTimer);
    refreshTimer();
    setInterval(refreshTimer, 30000);
  }

  /* ---------- Palette de commandes ---------- */

  const dialog = document.getElementById("cmdk");
  const input = document.getElementById("cmdk-input");
  const list = document.getElementById("cmdk-list");
  const providers = [];
  let results = [];
  let selected = 0;
  let searchToken = 0;
  let debounceTimer = null;

  /* Texte sans accents ni casse, pour comparer « reception » et « Réception ». */
  function fold(value) {
    return String(value || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  /* Score de correspondance : 0 = absent. Debut de libelle > debut de mot > ailleurs. */
  function score(query, item) {
    if (!query) return 1;
    const label = fold(item.label);
    const haystack = label + " " + fold(item.sub) + " " + fold(item.keywords);
    const words = fold(query).split(/\s+/).filter(Boolean);
    let total = 0;
    for (const word of words) {
      const index = haystack.indexOf(word);
      if (index === -1) return 0;
      total += label.startsWith(word) ? 6 : (index === 0 || /\s/.test(haystack[index - 1]) ? 4 : 1);
    }
    return total;
  }

  /* Libelle avec le premier mot recherche mis en evidence (texte echappe). */
  function highlight(label, query) {
    const word = fold(query).split(/\s+/).filter(Boolean)[0];
    const text = String(label || "");
    const index = word ? fold(text).indexOf(word) : -1;
    if (index === -1) return J.escapeHtml(text);
    return (
      J.escapeHtml(text.slice(0, index)) +
      '<mark class="cmdk-mark">' + J.escapeHtml(text.slice(index, index + word.length)) + "</mark>" +
      J.escapeHtml(text.slice(index + word.length))
    );
  }

  function register(provider) {
    providers.push(provider);
    providers.sort((a, b) => (a.order || 50) - (b.order || 50));
  }

  function render(query) {
    if (!results.length) {
      list.innerHTML = '<p class="cmdk-empty">Aucun résultat pour « ' + J.escapeHtml(query) + " ».</p>";
      input.removeAttribute("aria-activedescendant");
      return;
    }
    let html = "";
    let lastGroup = null;
    results.forEach((item, index) => {
      if (item.group !== lastGroup) {
        html += '<p class="cmdk-group" role="presentation">' + J.escapeHtml(item.group) + "</p>";
        lastGroup = item.group;
      }
      html +=
        '<button type="button" class="cmdk-item anim-in" role="option" id="cmdk-opt-' + index +
        '" data-index="' + index + '" style="--i:' + Math.min(index, 8) + '" aria-selected="' + (index === selected) + '">' +
        '<span class="cmdk-glyph k-' + J.escapeHtml(item.kind || "page") + '" aria-hidden="true">' +
        J.escapeHtml(item.glyph || "→") + "</span>" +
        '<span class="cmdk-text"><span class="cmdk-label">' + highlight(item.label, query) + "</span>" +
        (item.sub ? '<span class="cmdk-sub">' + J.escapeHtml(item.sub) + "</span>" : "") + "</span>" +
        (item.hint ? '<span class="cmdk-hint">' + J.escapeHtml(item.hint) + "</span>" : "") +
        "</button>";
    });
    list.innerHTML = html;
    input.setAttribute("aria-activedescendant", "cmdk-opt-" + selected);
  }

  function select(index) {
    if (!results.length) return;
    selected = (index + results.length) % results.length;
    list.querySelectorAll(".cmdk-item").forEach((element) => {
      element.setAttribute("aria-selected", String(Number(element.dataset.index) === selected));
    });
    const current = document.getElementById("cmdk-opt-" + selected);
    if (current) current.scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", "cmdk-opt-" + selected);
  }

  async function search() {
    const query = input.value.trim();
    const token = ++searchToken;
    const batches = await Promise.all(providers.map(async (provider) => {
      try {
        const items = await provider.search(query);
        return (items || []).map((item) => ({ group: provider.group, ...item }));
      } catch (error) {
        return [];
      }
    }));
    if (token !== searchToken) return; // une frappe plus recente a pris le relais
    // Elements d'un meme groupe contigus (un fournisseur peut proposer une action).
    const groups = new Map();
    batches.flat().forEach((item) => {
      if (!groups.has(item.group)) groups.set(item.group, []);
      groups.get(item.group).push(item);
    });
    results = [...groups.values()].flat().slice(0, 40);
    selected = 0;
    render(query);
  }

  function run(item) {
    if (!item) return;
    dialog.close();
    if (item.run) item.run();
    else if (item.url) window.location.href = item.url;
  }

  function captureNow() {
    const field = document.getElementById("cp-text");
    if (field) {
      field.focus();
      field.scrollIntoView({ block: "center", behavior: "smooth" });
    } else {
      window.location.href = "/?capturer=1";
    }
  }

  function open() {
    if (!dialog || dialog.open) return;
    input.value = "";
    dialog.showModal();
    input.focus();
    search();
  }

  if (dialog) {
    document.getElementById("cmdk-open").addEventListener("click", open);

    document.addEventListener("keydown", (event) => {
      const typing = event.target.closest("input, textarea, select, [contenteditable='true']");
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (dialog.open) dialog.close();
        else open();
      } else if (event.key === "/" && !typing && !dialog.open) {
        event.preventDefault();
        open();
      } else if (event.altKey && !event.ctrlKey && event.key.toLowerCase() === "n") {
        // Alt+N : capturer une idee (boite de reception), depuis n'importe quelle page.
        event.preventDefault();
        captureNow();
      }
    });

    input.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(search, 90);
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); select(selected + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); select(selected - 1); }
      else if (event.key === "Enter") { event.preventDefault(); run(results[selected]); }
    });

    list.addEventListener("mousemove", (event) => {
      const item = event.target.closest(".cmdk-item");
      if (item && Number(item.dataset.index) !== selected) select(Number(item.dataset.index));
    });

    list.addEventListener("click", (event) => {
      const item = event.target.closest(".cmdk-item");
      if (item) run(results[Number(item.dataset.index)]);
    });

    // Clic sur le fond assombri : la cible est le dialog lui-meme.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }

  /* --- Fournisseurs de base : actions et pages --- */

  function rank(items, query) {
    return items
      .map((item) => ({ item, points: score(query, item) }))
      .filter((entry) => entry.points > 0)
      .sort((a, b) => b.points - a.points)
      .map((entry) => entry.item);
  }

  register({
    group: "Actions",
    order: 10,
    search(query) {
      const light = getPref("theme") === "light";
      const ambient = getPref("ambient") === "on";
      const actions = [
        { kind: "action", glyph: light ? "☾" : "☀", label: light ? "Passer en thème sombre" : "Passer en thème clair",
          keywords: "theme apparence couleur clair sombre", run: toggleTheme },
        { kind: "action", glyph: "≈", label: ambient ? "Désactiver le fond animé" : "Activer le fond animé",
          keywords: "fond anime aurore animation", run: () => setPref("ambient", ambient ? "off" : "on") },
        { kind: "action", glyph: "+", label: "Capturer une idée", sub: "Boîte de réception", hint: "Alt+N",
          keywords: "nouvelle capture idee note tache ajouter", run: captureNow },
        { kind: "action", glyph: "✓", label: "Clôturer ma journée", sub: "Récapitulatif et note pour demain",
          keywords: "cloture fin journee soir demain recap", url: "/plan?cloturer=1" },
      ];
      if (session) {
        actions.unshift({ kind: "action", glyph: "■", label: "Arrêter le chronomètre", sub: session.task_title,
          keywords: "chrono chronometre stop arreter temps", run: stopTimer });
      }
      // Sans requete : seulement les actions les plus utiles, pour laisser voir les pages.
      return query ? rank(actions, query) : actions.slice(0, session ? 2 : 1);
    },
  });

  /* --- Recherche dans les donnees (lot 3) : taches, projets, journal, contacts, agenda --- */

  const SEARCH_KINDS = {
    tache: { glyph: "T", css: "tache" },
    projet: { glyph: "P", css: "projet" },
    journal: { glyph: "J", css: "note" },
    contact: { glyph: "C", css: "page" },
    evenement: { glyph: "E", css: "page" },
    cr: { glyph: "R", css: "cr" },
    cr_element: { glyph: "R", css: "cr" },
    note: { glyph: "N", css: "note" },
  };

  async function startTimer(taskId, title) {
    try {
      await J.callApi("/api/tasks/" + taskId + "/timer/start", { method: "POST" });
      document.dispatchEvent(new CustomEvent("nomaia:timer"));
      J.toast("Chronomètre démarré : " + title);
    } catch (error) {
      J.toast(error.message, true);
    }
  }

  register({
    group: "Résultats",
    order: 30,
    async search(query) {
      if (fold(query).replace(/\W/g, "").length < 2) return [];
      const data = await J.callApi("/api/search?q=" + encodeURIComponent(query) + "&limit=15");
      const items = data.results.map((result) => {
        const kind = SEARCH_KINDS[result.kind] || SEARCH_KINDS.tache;
        return {
          kind: kind.css, glyph: kind.glyph, label: result.label, sub: result.sub,
          hint: result.kind_label, url: result.url,
        };
      });
      // Raccourci sur la tache ouverte la plus pertinente : demarrer son chronometre.
      const firstTask = data.results.find((result) => result.kind === "tache" && !result.done);
      if (firstTask) {
        items.unshift({
          group: "Actions", kind: "action", glyph: "▶", label: "Démarrer le chrono sur « " + firstTask.label + " »",
          sub: firstTask.sub, run: () => startTimer(firstTask.id, firstTask.label),
        });
      }
      return items;
    },
  });

  const pagesScript = document.getElementById("cmdk-pages");
  const pages = pagesScript ? JSON.parse(pagesScript.textContent) : [];
  register({
    group: "Pages",
    order: 90,
    search(query) {
      const items = pages.map((page) => ({
        kind: "page", glyph: "→", label: page.label, sub: page.group, url: page.url, keywords: page.group,
      }));
      return rank(items, query);
    },
  });

  /* ---------- Cloche de notifications ---------- */

  const bell = document.getElementById("notif-button");
  const bellPanel = document.getElementById("notif-panel");
  const bellList = document.getElementById("notif-list");
  const bellDot = document.getElementById("notif-dot");

  async function refreshBell() {
    try {
      const { items } = await J.callApi("/api/notifications");
      bellDot.hidden = !items.length;
      bellDot.classList.toggle("is-danger", items.some((item) => item.level === "danger"));
      bell.setAttribute("aria-label", items.length ? "Notifications (" + items.length + ")" : "Notifications");
      bellList.innerHTML = items.length
        ? items.map((item) => '<li class="is-' + J.escapeHtml(item.level) + '"><a href="' + J.escapeHtml(item.url) + '">' +
            J.escapeHtml(item.text) + "</a></li>").join("")
        : '<li class="notif-empty">Rien d’urgent. Bonne continuation.</li>';
    } catch (error) {
      // Serveur momentanement injoignable : la cloche garde son dernier etat.
    }
  }

  function toggleBell(open) {
    bellPanel.hidden = !open;
    bell.setAttribute("aria-expanded", String(open));
    if (open) refreshBell();
  }

  if (bell) {
    bell.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleBell(bellPanel.hidden);
    });
    document.addEventListener("click", (event) => {
      if (!bellPanel.hidden && !event.target.closest(".notif")) toggleBell(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !bellPanel.hidden) toggleBell(false);
    });
    refreshBell();
    setInterval(refreshBell, 60000);
  }

  /* ---------- Rechargement a chaud ---------- */

  /* La base tient un compteur de revision, incremente par triggers a chaque
   * ecriture sur les donnees affichees, quel que soit l'ecrivain (interface,
   * session Claude, Telegram, triage, synchronisations). Toutes les 10 s quand
   * la fenetre est visible, et des son retour au premier plan, il est relu :
   * s'il a bouge, les fonctions enregistrees par J.live.onChange sont appelees.
   *
   * Jamais pendant une saisie : fenetre modale ouverte, champ de saisie actif
   * ou glisser-deposer en cours differe la mise a jour, reverifiee ensuite
   * toutes les 1,5 s. Une fonction qui renvoie false (ou une promesse de
   * false) signale qu'elle n'a rien applique : elle sera rappelee de meme. */
  const LIVE_POLL_MS = 10000;
  const LIVE_RETRY_MS = 1500;
  const liveHandlers = [];
  let liveRev = null; // derniere revision lue (null : pas encore lue)
  let livePauses = 0;
  let liveFetching = false;
  let liveApplying = false;
  let liveRetryTimer = null;

  /* Saisie en cours : liste deroulante ou zone editable active, ou champ texte
   * actif et non vide. Un champ vide n'a rien a perdre : sans cette nuance, la
   * saisie rapide des taches ou la zone du chat, actives des l'ouverture,
   * bloqueraient tout rechargement. Cases a cocher et boutons ne comptent pas. */
  function isTyping() {
    const active = document.activeElement;
    if (!active || active === document.body) return false;
    if (active.isContentEditable || active.matches("select")) return true;
    const textField = active.matches("textarea") || (active.matches("input") &&
      !/^(checkbox|radio|button|submit|reset|range|color|file|image)$/i.test(active.type));
    return textField && active.value !== "";
  }

  function liveBlocked() {
    return livePauses > 0 || isTyping() || Boolean(document.querySelector("dialog[open], .is-dragging"));
  }

  function scheduleLiveRetry() {
    clearTimeout(liveRetryTimer);
    liveRetryTimer = setTimeout(applyLive, LIVE_RETRY_MS);
  }

  async function applyLive() {
    clearTimeout(liveRetryTimer);
    if (liveRev === null || liveApplying) return;
    const stale = liveHandlers.filter((handler) => handler.rev !== liveRev);
    if (!stale.length) return;
    if (liveBlocked()) {
      scheduleLiveRetry();
      return;
    }
    liveApplying = true;
    const target = liveRev;
    let deferred = false;
    try {
      for (const handler of stale) {
        try {
          if ((await handler.fn()) === false) deferred = true;
          else handler.rev = target;
        } catch (error) {
          // Echec de rechargement : nouvel essai au prochain sondage.
        }
      }
    } finally {
      liveApplying = false;
    }
    if (deferred || liveRev !== target) scheduleLiveRetry();
  }

  async function pollLive() {
    if (!liveHandlers.length || liveFetching || document.visibilityState !== "visible") return;
    liveFetching = true;
    try {
      const rev = Number((await J.callApi("/api/revision")).rev);
      // Premiere lecture : simple reference, les donnees de la page sont fraiches.
      if (liveRev === null) liveHandlers.forEach((handler) => { if (handler.rev === null) handler.rev = rev; });
      liveRev = rev;
    } catch (error) {
      return; // serveur momentanement injoignable : nouvel essai au prochain tour
    } finally {
      liveFetching = false;
    }
    applyLive();
  }

  /* Enregistre la fonction de rechargement d'une page (ou d'un composant). */
  function onLiveChange(fn) {
    liveHandlers.push({ fn, rev: liveRev });
    if (liveHandlers.length === 1) pollLive();
  }

  function pauseLive() {
    livePauses += 1;
  }

  function resumeLive() {
    livePauses = Math.max(0, livePauses - 1);
    if (!livePauses) applyLive();
  }

  setInterval(pollLive, LIVE_POLL_MS);
  document.addEventListener("visibilitychange", pollLive);
  window.addEventListener("focus", pollLive);

  // Cloche et chronometre suivent les changements ; leurs intervalles restent un filet.
  if (bell) onLiveChange(refreshBell);
  if (pill) onLiveChange(refreshTimer);

  /* Date de l'en-tete : les pages qui la gerent (plan, agenda...) la
   * remplacent ensuite ; les autres gardent celle du jour, au lieu d'un vide. */
  const headerDate = document.getElementById("header-date");
  if (headerDate && !headerDate.textContent.trim()) {
    headerDate.textContent = new Date().toLocaleDateString("fr-FR", {
      weekday: "long", day: "numeric", month: "long", year: "numeric",
    });
  }

  /* ---------- Espace affiche (Tout / Pro / Perso) ---------- */

  // Reglage global en base : la page se recharge pour que listes, compteurs
  // et menus deroulants servis par le serveur suivent le meme perimetre.
  const spaceSwitch = document.getElementById("space-switch");
  const currentSpace = (spaceSwitch && spaceSwitch.dataset.current) || "all";
  if (spaceSwitch) {
    spaceSwitch.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-space]");
      if (!button || button.dataset.space === currentSpace) return;
      try {
        await J.callApi("/api/settings", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ space_filter: button.dataset.space }),
        });
        location.reload();
      } catch (error) {
        J.toast(error.message, true);
      }
    });
  }

  const createSelect = document.getElementById("space-create");
  if (createSelect) {
    createSelect.addEventListener("change", async () => {
      try {
        await J.callApi("/api/settings", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ create_space: createSelect.value }),
        });
        J.toast("Les nouveaux éléments iront dans l'espace " + createSelect.selectedOptions[0].text.slice(2) + ".");
      } catch (error) {
        J.toast(error.message, true);
      }
    });
  }

  /* Badge d'espace d'un element : en mode Tout seulement (sinon tout est du
   * meme espace), sauf « Commun », toujours signale. */
  const SPACE_LABELS = { pro: "Pro", perso: "Perso", commun: "Commun" };
  function spaceBadge(value) {
    if (!value || !SPACE_LABELS[value] || (currentSpace !== "all" && value !== "commun")) return "";
    const cls = value === "perso" ? "badge-outline" : value === "commun" ? "badge-ok" : "badge-indigo";
    return '<span class="badge ' + cls + ' space-badge">' + SPACE_LABELS[value] + "</span>";
  }

  J.space = currentSpace;
  J.spaceBadge = spaceBadge;
  J.prefs = { get: getPref, set: setPref };
  J.palette = { register, open, fold, score, rank };
  J.timer = { refresh: refreshTimer, stop: stopTimer };
  J.live = { onChange: onLiveChange, pause: pauseLive, resume: resumeLive };
})();
