/* Base de connaissances : liste filtree, lecture en Markdown, edition avec
 * sauvegarde automatique. Rendu Markdown : static/js/markdown.js. */

(function () {
  const layout = document.getElementById("nt-layout");
  if (!layout) return;

  const AUTOSAVE_MS = 800;
  const JSON_HEADERS = { "Content-Type": "application/json" };
  const PHONE = window.matchMedia("(max-width: 860px)");
  const PIN_ICON =
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 17v5"/>' +
    '<path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76' +
    'a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';

  const els = {
    list: document.getElementById("nt-list"),
    facets: document.getElementById("nt-facets"),
    project: document.getElementById("nt-project"),
    pane: document.getElementById("nt-pane"),
    menu: document.getElementById("nt-menu"),
    confirm: document.getElementById("nt-confirm"),
  };

  const state = {
    filters: { project_id: "", category: "", tag: "", odoo_version: "" },
    notes: [],
    facets: { categories: [], versions: [], tags: [] },
    template: "",
    current: null,     // note ouverte (complete) ou brouillon d'une nouvelle note
    previousId: null,  // note a rouvrir si l'on abandonne un brouillon
    mode: "empty",     // empty | read | edit
    dirty: false,
    saving: false,
    editToken: 0,      // change a chaque ouverture d'editeur : une sauvegarde en retard ne touche pas au suivant
    timer: null,
    lastError: "",
  };
  let saveChain = Promise.resolve(true);
  let listTimer = null;

  const esc = J.escapeHtml;
  const api = (url, method, body) =>
    J.callApi(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });

  /* ---------- Outils ---------- */

  function safeColor(value) {
    return /^#[0-9a-f]{3,8}$/i.test(value || "") ? value : "";
  }

  function projectDot(name, color) {
    if (!name) return "";
    const tint = safeColor(color);
    return '<span class="nt-proj"><span class="nt-dot"' + (tint ? ' style="--dot:' + tint + '"' : "") +
      ' aria-hidden="true"></span>' + esc(name) + "</span>";
  }

  function categoryLabel(key) {
    const found = state.facets.categories.find((item) => item.key === key);
    return found ? found.label : key;
  }

  /* Extrait lisible : sans les marques Markdown ni les blocs de code. */
  function plainExcerpt(text) {
    return String(text || "")
      .replace(/```[\s\S]*?(```|$)/g, " ")
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/^\s*(#{1,6}|>|[-*+]\s+\[[ xX]\]|[-*+]|\d+[.)])\s+/gm, "")
      .replace(/[*_`|]+/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function modifiedLabel(value) {
    if (!value) return "";
    const day = String(value).slice(0, 10);
    const today = new Date();
    const iso = today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0") + "-" +
      String(today.getDate()).padStart(2, "0");
    return day === iso ? "Modifiée aujourd'hui à " + J.formatMoment(value) : "Modifiée le " + J.formatMoment(value);
  }

  function noteBadges(note) {
    return [
      note.category ? '<span class="badge badge-accent">' + esc(note.category_label || categoryLabel(note.category)) + "</span>" : "",
      note.module ? '<span class="badge nt-mono">' + esc(note.module) + "</span>" : "",
      note.odoo_version ? '<span class="badge badge-indigo">Odoo ' + esc(note.odoo_version) + "</span>" : "",
      projectDot(note.project_name, note.project_color),
    ].join("");
  }

  function idFromPath() {
    const match = window.location.pathname.match(/^\/notes\/(\d+)\/?$/);
    return match ? Number(match[1]) : null;
  }

  function setUrl(id, replace) {
    const url = id ? "/notes/" + id : "/notes";
    if (window.location.pathname === url) return;
    history[replace ? "replaceState" : "pushState"]({ noteId: id || null }, "", url);
  }

  function revealPane() {
    if (PHONE.matches) els.pane.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* Confirmation dans la fenetre du site ; resout true si l'action est validee. */
  function askConfirm(title, text, okLabel) {
    document.getElementById("nt-confirm-title").textContent = title;
    document.getElementById("nt-confirm-text").textContent = text;
    document.getElementById("nt-confirm-ok").textContent = okLabel;
    els.confirm.returnValue = "";
    return new Promise((resolve) => {
      els.confirm.addEventListener("close", () => resolve(els.confirm.returnValue === "ok"), { once: true });
      els.confirm.showModal();
    });
  }

  /* ---------- Liste et filtres ---------- */

  async function loadList(animate) {
    const params = new URLSearchParams();
    Object.entries(state.filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    try {
      const data = await api("/api/notes?" + params.toString(), "GET");
      state.notes = data.notes || [];
      state.facets = data.facets || state.facets;
      state.template = data.deployment_template || "";
      renderFacets();
      renderList(animate);
    } catch (error) {
      els.list.innerHTML = '<p class="empty">' + esc(error.message) + "</p>";
    }
  }

  /* Rafraichit la liste apres une sauvegarde, sans rejouer l'apparition. */
  function refreshListSoon() {
    clearTimeout(listTimer);
    listTimer = setTimeout(() => loadList(false), 300);
  }

  function chip(key, value, label, count) {
    const on = state.filters[key] === String(value);
    return '<button type="button" class="chip' + (on ? " is-on" : "") + '" data-filter="' + key +
      '" data-value="' + esc(value) + '" aria-pressed="' + on + '">' + esc(label) +
      (count != null ? '<span class="nt-count">' + count + "</span>" : "") + "</button>";
  }

  function facetGroup(title, chips) {
    if (!chips.length) return "";
    return '<div class="nt-facet"><p class="nt-facet-title">' + title + '</p><div class="nt-chips">' +
      chips.join("") + "</div></div>";
  }

  function renderFacets() {
    const { categories, versions, tags } = state.facets;
    const used = categories.filter((item) => item.count > 0 || state.filters.category === item.key);
    const active = ["category", "odoo_version", "tag"].some((key) => state.filters[key]) || state.filters.project_id;
    els.facets.innerHTML =
      facetGroup("Catégories", used.map((item) => chip("category", item.key, item.label, item.count))) +
      facetGroup("Versions d'Odoo", versions.map((version) => chip("odoo_version", version, version))) +
      facetGroup("Étiquettes", tags.map((tag) => chip("tag", tag, "#" + tag))) +
      (active ? '<button type="button" class="link nt-reset" id="nt-reset">Effacer les filtres</button>' : "");
  }

  function renderList(animate) {
    if (!state.notes.length) {
      const filtered = Object.values(state.filters).some(Boolean);
      els.list.innerHTML = '<p class="empty">' +
        (filtered ? "Aucune note pour ces filtres." : "Aucune note pour l'instant. Crée la première avec « Nouvelle note ».") +
        "</p>";
      return;
    }
    const currentId = state.current && state.current.id;
    els.list.innerHTML = state.notes.map((note, index) => {
      const active = note.id === currentId;
      return '<a class="nt-item' + (animate ? " anim-in" : "") + (note.pinned ? " is-pinned" : "") + '"' +
        (animate ? ' style="--i:' + Math.min(index, 12) + '"' : "") +
        ' href="/notes/' + note.id + '" data-id="' + note.id + '"' + (active ? ' aria-current="true"' : "") + ">" +
        '<span class="nt-item-head">' +
          (note.pinned ? '<span class="nt-pin" title="Épinglée">' + PIN_ICON + '<span class="sr-only">Épinglée</span></span>' : "") +
          '<span class="nt-item-title">' + esc(note.title) + J.spaceBadge(note.space) + "</span>" +
        "</span>" +
        (note.excerpt ? '<span class="nt-item-excerpt">' + esc(plainExcerpt(note.excerpt)) + "</span>" : "") +
        '<span class="nt-badges">' + noteBadges(note) + "</span>" +
      "</a>";
    }).join("");
  }

  function markActiveItem() {
    const currentId = state.current && state.current.id;
    els.list.querySelectorAll(".nt-item").forEach((item) => {
      if (Number(item.dataset.id) === currentId) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    });
  }

  /* ---------- Lecture ---------- */

  function renderEmpty() {
    state.mode = "empty";
    state.current = null;
    els.pane.innerHTML =
      '<div class="empty nt-empty">' +
        "<p>Ouvre une note dans la liste ou crée-en une.</p>" +
        '<button type="button" class="btn-primary" data-action="new">Nouvelle note</button>' +
      "</div>";
    markActiveItem();
  }

  function renderRead() {
    const note = state.current;
    state.mode = "read";
    const tags = (note.tags || []).map((tag) =>
      '<button type="button" class="chip nt-tag" data-tag="' + esc(tag) + '">#' + esc(tag) + "</button>").join("");
    const source = note.source_task_id
      ? '<p class="nt-source">Anomalie d\'origine : <a class="link" href="/plan?tache=' + Number(note.source_task_id) + '">' +
        esc(note.source_task_title || "tâche n° " + note.source_task_id) + "</a></p>"
      : "";
    els.pane.innerHTML =
      '<article class="nt-read">' +
        '<button type="button" class="link nt-back" data-action="back">← Liste des notes</button>' +
        '<header class="nt-read-head">' +
          '<h2 class="nt-title">' + (note.pinned ? '<span class="nt-pin" title="Épinglée">' + PIN_ICON + "</span>" : "") +
            esc(note.title) + "</h2>" +
          '<div class="nt-actions">' +
            '<button type="button" class="btn-ghost" data-action="edit">Modifier</button>' +
            '<button type="button" class="btn-ghost" data-action="pin" aria-pressed="' + Boolean(note.pinned) + '">' +
              (note.pinned ? "Désépingler" : "Épingler") + "</button>" +
            '<button type="button" class="btn-ghost danger" data-action="delete">Supprimer</button>' +
          "</div>" +
        "</header>" +
        '<div class="nt-meta"><span class="nt-badges">' + noteBadges(note) + "</span>" +
          '<span class="nt-date">' + esc(modifiedLabel(note.updated_at)) + "</span></div>" +
        (tags ? '<div class="nt-chips nt-tags">' + tags + "</div>" : "") +
        source +
        '<div class="md nt-body">' + (note.content && note.content.trim()
          ? JMarkdown.render(note.content)
          : '<p class="nt-muted">Note vide. « Modifier » pour l\'écrire.</p>') + "</div>" +
        '<section class="nt-files" aria-labelledby="nt-files-title">' +
          '<h3 class="nt-files-title" id="nt-files-title">Pièces jointes</h3>' +
          '<div id="nt-attachments"></div>' +
        "</section>" +
      "</article>";
    // Pieces jointes : composant global (static/js/attachments.js), note deja creee seulement.
    if (window.JAttachments && note.id) {
      window.JAttachments.mount(document.getElementById("nt-attachments"), "note", note.id);
    }
    markActiveItem();
  }

  /* Ouvre une note par son id ; push=false pour un chargement ou un retour arriere. */
  async function openNote(id, push) {
    if (state.current && state.current.id === id && state.mode !== "empty") {
      revealPane();
      return;
    }
    if (!(await leaveEditor())) return;
    state.editToken += 1; // l'editeur quitte ne s'enregistre plus
    try {
      state.current = await api("/api/notes/" + id, "GET");
    } catch (error) {
      J.toast(error.message, true);
      renderEmpty();
      setUrl(null, true);
      return;
    }
    renderRead();
    if (push) setUrl(id, false);
    revealPane();
  }

  async function togglePin() {
    try {
      state.current = await api("/api/notes/" + state.current.id, "PATCH", { pinned: !state.current.pinned });
      renderRead();
      loadList(false);
    } catch (error) {
      J.toast(error.message, true);
    }
  }

  async function deleteCurrent() {
    const note = state.current;
    const ok = await askConfirm("Supprimer la note ?", "« " + note.title + " » sera supprimée définitivement.", "Supprimer");
    if (!ok) return;
    try {
      await api("/api/notes/" + note.id, "DELETE");
      J.toast("Note supprimée.");
      renderEmpty();
      setUrl(null, true); // pas de retour arriere vers une note supprimee
      loadList(false);
    } catch (error) {
      J.toast(error.message, true);
    }
  }

  /* ---------- Edition ---------- */

  function categoryOptions(selected) {
    return state.facets.categories.map((item) =>
      '<option value="' + esc(item.key) + '"' + (item.key === selected ? " selected" : "") + ">" + esc(item.label) + "</option>"
    ).join("");
  }

  function projectOptions(note) {
    let html = '<option value="">Aucun projet</option>';
    let found = false;
    Array.from(els.project.options).slice(1).forEach((option) => {
      const selected = Number(option.value) === note.project_id;
      found = found || selected;
      html += '<option value="' + esc(option.value) + '"' + (selected ? " selected" : "") + ">" + esc(option.textContent) + "</option>";
    });
    // Projet termine : absent de la liste, mais garde pour ne pas le perdre a l'enregistrement.
    if (note.project_id && !found) {
      html += '<option value="' + Number(note.project_id) + '" selected>' + esc(note.project_name || "Projet n° " + note.project_id) + "</option>";
    }
    return html;
  }

  function field(id, label, control, wide) {
    return '<div class="nt-field' + (wide ? " nt-field-wide" : "") + '"><label for="' + id + '">' + label + "</label>" + control + "</div>";
  }

  function startEdit(note) {
    state.mode = "edit";
    state.current = note;
    state.dirty = false;
    state.lastError = "";
    state.editToken += 1;
    const isNew = !note.id;
    els.pane.innerHTML =
      '<form class="nt-editor" id="ne-form" autocomplete="off" novalidate>' +
        '<button type="button" class="link nt-back" data-action="back">← Liste des notes</button>' +
        '<div class="nt-editor-head">' +
          '<input type="text" id="ne-title" class="nt-title-input" maxlength="200" placeholder="Titre de la note" aria-label="Titre" required>' +
          '<span class="nt-save-state" id="ne-state" role="status" aria-live="polite">' + (isNew ? "Nouvelle note" : "Enregistré") + "</span>" +
        "</div>" +
        '<div class="nt-fields">' +
          field("ne-category", "Catégorie", '<select id="ne-category">' + categoryOptions(note.category) + "</select>") +
          field("ne-project", "Projet", '<select id="ne-project">' + projectOptions(note) + "</select>") +
          field("ne-space", "Espace", '<select id="ne-space" title="Une note rattachée à un projet suit l’espace du projet">' +
            [["pro", "Pro"], ["perso", "Perso"], ["commun", "Commun"]].map(([v, l]) =>
              '<option value="' + v + '"' + ((note.space || J.space) === v ? " selected" : "") + ">" + l + "</option>").join("") +
            "</select>") +
          field("ne-version", "Version d'Odoo", '<input type="text" id="ne-version" maxlength="20" list="ne-versions" placeholder="17.0">' +
            '<datalist id="ne-versions">' + state.facets.versions.map((v) => '<option value="' + esc(v) + '">').join("") + "</datalist>") +
          field("ne-module", "Module", '<input type="text" id="ne-module" maxlength="120" placeholder="sale_stock" spellcheck="false">') +
          field("ne-tags", "Étiquettes", '<input type="text" id="ne-tags" placeholder="stock, migration (séparées par des virgules)">', true) +
        "</div>" +
        '<div class="nt-tabs" role="tablist" aria-label="Affichage du contenu">' +
          '<button type="button" role="tab" data-tab="write" aria-selected="true">Écrire</button>' +
          '<button type="button" role="tab" data-tab="preview" aria-selected="false">Aperçu</button>' +
        "</div>" +
        '<div class="nt-split" id="ne-split" data-tab="write">' +
          '<textarea id="ne-content" class="nt-source-input" spellcheck="false" aria-label="Contenu en Markdown"' +
            ' placeholder="Écris en Markdown : # titre, **gras**, `code`, ```python …"></textarea>' +
          '<div class="md nt-preview" id="ne-preview" aria-label="Aperçu"></div>' +
        "</div>" +
        '<div class="nt-editor-foot">' +
          '<span class="nt-hint">Markdown · <kbd>Ctrl</kbd> + <kbd>S</kbd> enregistre' + (isNew ? "" : " · sauvegarde automatique") + "</span>" +
          '<button type="button" class="btn-ghost" data-action="close">' + (isNew ? "Annuler" : "Terminer") + "</button>" +
          '<button type="submit" class="btn-primary">Enregistrer</button>' +
        "</div>" +
      "</form>";
    // Valeurs posees par propriete : aucun echappement a gerer.
    document.getElementById("ne-title").value = note.title || "";
    document.getElementById("ne-version").value = note.odoo_version || "";
    document.getElementById("ne-module").value = note.module || "";
    document.getElementById("ne-tags").value = (note.tags || []).join(", ");
    document.getElementById("ne-content").value = note.content || "";
    renderPreview();
    markActiveItem();
    (isNew ? document.getElementById("ne-title") : document.getElementById("ne-content")).focus({ preventScroll: true });
    revealPane();
  }

  function formValues() {
    const value = (id) => document.getElementById(id).value;
    const values = {
      title: value("ne-title").trim(),
      category: value("ne-category"),
      project_id: value("ne-project") ? Number(value("ne-project")) : null,
      space: value("ne-space"),
      odoo_version: value("ne-version").trim(),
      module: value("ne-module").trim(),
      tags: value("ne-tags"),
      content: value("ne-content"),
    };
    // Liste des categories indisponible (liste en erreur) : la categorie actuelle est gardee.
    if (!values.category) delete values.category;
    return values;
  }

  let previewFrame = 0;
  function renderPreview() {
    cancelAnimationFrame(previewFrame);
    previewFrame = requestAnimationFrame(() => {
      const preview = document.getElementById("ne-preview");
      const content = document.getElementById("ne-content");
      if (!preview || !content) return;
      preview.innerHTML = content.value.trim()
        ? JMarkdown.render(content.value)
        : '<p class="nt-muted">L\'aperçu apparaît ici.</p>';
    });
  }

  function setSaveState(text, kind) {
    const node = document.getElementById("ne-state");
    if (!node) return;
    node.textContent = text;
    node.dataset.kind = kind || "";
  }

  function onEditorInput(event) {
    state.dirty = true;
    if (event.target.id === "ne-content") renderPreview();
    if (!state.current.id) {
      setSaveState("Non enregistrée", "pending");
      return;
    }
    setSaveState("Modifications en attente…", "pending");
    clearTimeout(state.timer);
    state.timer = setTimeout(() => save(false), AUTOSAVE_MS);
  }

  /* Enregistre l'editeur ; les sauvegardes s'enchainent sans se chevaucher.
   * explicit : demande de l'utilisateur (bouton, Ctrl+S), erreurs toujours signalees. */
  function save(explicit) {
    clearTimeout(state.timer);
    const token = state.editToken;
    saveChain = saveChain.then(() => doSave(explicit, token));
    return saveChain;
  }

  async function doSave(explicit, token) {
    if (state.mode !== "edit" || token !== state.editToken) return true;
    const isNew = !state.current.id;
    if (!isNew && !state.dirty) {
      setSaveState("Enregistré", "ok");
      return true;
    }
    const payload = formValues();
    if (!payload.title) {
      setSaveState("Titre obligatoire", "error");
      if (explicit) {
        J.toast("Le titre est obligatoire.", true);
        document.getElementById("ne-title").focus();
      }
      return false;
    }
    const sent = JSON.stringify(payload);
    state.saving = true;
    setSaveState("Enregistrement…", "busy");
    try {
      const note = isNew
        ? await api("/api/notes", "POST", payload)
        : await api("/api/notes/" + state.current.id, "PATCH", payload);
      if (token !== state.editToken) return true;
      state.current = note;
      // Frappes arrivees pendant l'envoi : elles attendent leur propre sauvegarde.
      state.dirty = JSON.stringify(formValues()) !== sent;
      state.lastError = "";
      setSaveState(state.dirty ? "Modifications en attente…" : "Enregistré", state.dirty ? "pending" : "ok");
      if (isNew) {
        setUrl(note.id, false);
        J.toast("Note créée.");
        const hint = els.pane.querySelector(".nt-hint");
        if (hint) hint.insertAdjacentText("beforeend", " · sauvegarde automatique");
        const close = els.pane.querySelector('[data-action="close"]');
        if (close) close.textContent = "Terminer";
      }
      refreshListSoon();
      return true;
    } catch (error) {
      if (token !== state.editToken) return false;
      // Message de l'API tel quel (dont la detection d'un secret en clair).
      setSaveState("Non enregistré", "error");
      if (explicit || error.message !== state.lastError) J.toast(error.message, true);
      state.lastError = error.message;
      return false;
    } finally {
      state.saving = false;
    }
  }

  /* Quitte l'editeur proprement : enregistre une note existante, propose
   * d'abandonner un brouillon. Resout false s'il faut rester. */
  async function leaveEditor() {
    if (state.mode !== "edit") return true;
    if (state.current.id) {
      const ok = await save(false);
      if (!ok) J.toast("Modifications non enregistrées : corrige-les avant de quitter.", true);
      return ok;
    }
    if (!state.dirty) return true;
    return askConfirm("Abandonner la nouvelle note ?", "Elle n'a pas encore été enregistrée.", "Abandonner");
  }

  async function closeEditor() {
    const draft = !state.current.id;
    if (!(await leaveEditor())) return;
    state.editToken += 1;
    state.dirty = false;
    if (!draft) {
      renderRead();
      return;
    }
    const previous = state.previousId;
    state.mode = "empty";
    state.current = null;
    if (previous) {
      openNote(previous, false);
      setUrl(previous, true);
    } else {
      renderEmpty();
    }
  }

  async function newNote(preset) {
    if (!(await leaveEditor())) return;
    // Brouillon remplace par un autre : on garde la note a rouvrir du premier.
    const draft = state.mode === "edit" && state.current && !state.current.id;
    state.previousId = state.current && state.current.id ? state.current.id : (draft ? state.previousId : null);
    startEdit(Object.assign({
      id: null,
      title: "",
      content: "",
      category: state.filters.category || "other",
      project_id: state.filters.project_id ? Number(state.filters.project_id) : null,
      odoo_version: state.filters.odoo_version || "",
      module: "",
      tags: state.filters.tag ? [state.filters.tag] : [],
    }, preset || {}));
    setUrl(null, false);
  }

  /* ---------- Evenements ---------- */

  els.list.addEventListener("click", (event) => {
    const item = event.target.closest(".nt-item");
    if (!item || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    openNote(Number(item.dataset.id), true);
  });

  els.facets.addEventListener("click", (event) => {
    if (event.target.closest("#nt-reset")) {
      state.filters = { project_id: "", category: "", tag: "", odoo_version: "" };
      els.project.value = "";
      loadList(true);
      return;
    }
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    const key = button.dataset.filter;
    state.filters[key] = state.filters[key] === button.dataset.value ? "" : button.dataset.value;
    loadList(true);
  });

  els.project.addEventListener("change", () => {
    state.filters.project_id = els.project.value;
    loadList(true);
  });

  els.pane.addEventListener("click", (event) => {
    const tag = event.target.closest("[data-tag]");
    if (tag) {
      state.filters.tag = tag.dataset.tag;
      loadList(true);
      if (PHONE.matches) layout.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const tab = event.target.closest("[data-tab]");
    if (tab && tab.getAttribute("role") === "tab") {
      document.getElementById("ne-split").dataset.tab = tab.dataset.tab;
      els.pane.querySelectorAll('[role="tab"]').forEach((other) =>
        other.setAttribute("aria-selected", String(other === tab)));
      return;
    }
    const action = event.target.closest("[data-action]");
    if (!action) return;
    const name = action.dataset.action;
    if (name === "new") newNote();
    else if (name === "edit") startEdit(Object.assign({}, state.current));
    else if (name === "pin") togglePin();
    else if (name === "delete") deleteCurrent();
    else if (name === "close") closeEditor();
    else if (name === "back") layout.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  els.pane.addEventListener("input", (event) => {
    if (state.mode === "edit" && event.target.closest("#ne-form")) onEditorInput(event);
  });
  els.pane.addEventListener("change", (event) => {
    // Les listes deroulantes ne declenchent pas toujours « input ».
    if (state.mode === "edit" && event.target.tagName === "SELECT") onEditorInput(event);
  });

  els.pane.addEventListener("submit", (event) => {
    event.preventDefault();
    save(true);
  });

  // Ctrl+S : seulement depuis l'editeur, pour ne gener ni la palette ni le navigateur ailleurs.
  els.pane.addEventListener("keydown", (event) => {
    if (state.mode !== "edit" || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "s") return;
    if (!event.target.closest("#ne-form")) return;
    event.preventDefault();
    save(true);
  });

  document.getElementById("nt-new").addEventListener("click", () => newNote());

  document.getElementById("nt-template-deploy").addEventListener("click", () => {
    els.menu.open = false;
    newNote({ category: "deployment", content: state.template });
  });

  // Menu des modeles : se ferme au clic ailleurs ou avec Echap.
  document.addEventListener("click", (event) => {
    if (els.menu.open && !els.menu.contains(event.target)) els.menu.open = false;
  });
  els.menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && els.menu.open) {
      els.menu.open = false;
      els.menu.querySelector("summary").focus();
    }
  });

  window.addEventListener("popstate", async () => {
    const id = idFromPath();
    const currentId = state.current && state.current.id;
    if (id === currentId && state.mode !== "empty") return;
    if (!(await leaveEditor())) {
      setUrl(currentId, false); // on reste : l'adresse revient a la note ouverte
      return;
    }
    state.editToken += 1;
    state.mode = "empty";
    if (id) openNote(id, false);
    else renderEmpty();
  });

  window.addEventListener("beforeunload", (event) => {
    if (state.mode === "edit" && (state.dirty || state.saving)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  /* ---------- Rechargement a chaud ---------- */

  /* Liste rechargee sans rejouer l'apparition, filtres gardes ; note ouverte en
   * lecture relue et redessinee seulement si elle a change. L'editeur, lui,
   * n'est jamais touche : sa sauvegarde automatique fait foi. */
  async function liveReload() {
    await loadList(false);
    if (state.mode !== "read" || !state.current || !state.current.id) return;
    const id = state.current.id;
    let fresh;
    try {
      fresh = await api("/api/notes/" + id, "GET");
    } catch (error) {
      return; // supprimee ailleurs ou serveur injoignable : la lecture reste affichee
    }
    // Une autre note a pu etre ouverte, ou l'edition commencee, pendant l'appel.
    if (state.mode !== "read" || !state.current || state.current.id !== id) return;
    if (JSON.stringify(fresh) === JSON.stringify(state.current)) return;
    state.current = fresh;
    renderRead();
  }

  J.live.onChange(liveReload);

  /* ---------- Demarrage ---------- */

  (async function start() {
    const initialId = Number(layout.dataset.noteId) || null;
    history.replaceState({ noteId: initialId }, "", window.location.href);
    await loadList(true);
    if (initialId) openNote(initialId, false);
    else renderEmpty();
  })();
})();
