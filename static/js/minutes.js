/* NOMAIA — comptes rendus de reunion, d'intervention et de formation.
 *
 * Une seule page pour la liste (/comptes-rendus) et l'editeur
 * (/comptes-rendus/<id>) : le passage de l'une a l'autre se fait par
 * pushState, le bouton Retour du navigateur est gere par popstate.
 *
 * L'editeur enregistre seul, 800 ms apres la derniere frappe, et n'envoie
 * que les champs modifies. Les textes viennent de la saisie ou d'un agenda
 * importe : tout passe par escapeHtml avant innerHTML.
 */

(function () {
  const { toast, callApi, escapeHtml } = window.J;

  const SAVE_DELAY_MS = 800;
  const LIST_URL = "/comptes-rendus";
  const HEX_COLOR = /^#[0-9a-fA-F]{3,8}$/;
  const ITEM_TYPES = ["decision", "action", "request"];

  const KIND_LABELS = { meeting: "Réunion", intervention: "Intervention", training: "Formation" };
  const KIND_BADGES = { meeting: "badge-indigo", intervention: "badge-p2", training: "badge-accent" };
  const TASK_STATUS_LABELS = { todo: "À faire", done: "Terminée" };
  const ITEM_LABELS = { decision: "la décision", action: "l'action", request: "la demande" };
  const EMPTY_ITEMS = {
    decision: "Aucune décision pour l'instant.",
    action: "Aucune action pour l'instant.",
    request: "Aucune demande du client.",
  };
  const NO_PROJECT_HINT = "Rattache d'abord le compte rendu à un projet pour inscrire ses décisions au registre.";

  const rootEl = document.getElementById("mn-root");
  const listViewEl = document.getElementById("mn-list-view");
  const listEl = document.getElementById("mn-list");
  const filterEl = document.getElementById("mn-filter");

  const editorViewEl = document.getElementById("mn-editor-view");
  const missingEl = document.getElementById("mn-missing");
  const docEl = document.getElementById("mn-doc");
  const saveStateEl = document.getElementById("mn-save-state");
  const statusEl = document.getElementById("mn-status");
  const printMetaEl = document.getElementById("mn-print-meta");
  const printTitleEl = document.getElementById("mn-print-title");
  const projectEl = document.getElementById("mn-project");
  const notesLabelEl = document.getElementById("mn-notes-label");
  const finalizeEl = document.getElementById("mn-finalize");
  const reopenEl = document.getElementById("mn-reopen");

  const newModalEl = document.getElementById("mn-new-modal");
  const newFormEl = document.getElementById("mn-new-form");
  const newErrorEl = document.getElementById("mn-new-error");
  const newFields = {
    title: document.getElementById("mn-new-title"),
    date: document.getElementById("mn-new-date"),
    kind: document.getElementById("mn-new-kind"),
    project: document.getElementById("mn-new-project"),
  };

  const deleteModalEl = document.getElementById("mn-delete-modal");
  const pendingModalEl = document.getElementById("mn-pending-modal");
  const pendingTextEl = document.getElementById("mn-pending-text");
  const pendingListEl = document.getElementById("mn-pending-list");
  const pendingHintEl = document.getElementById("mn-pending-hint");
  const pendingConvertEl = document.getElementById("mn-pending-convert");

  const fieldEls = Array.from(docEl.querySelectorAll("[data-field]"));

  const state = {
    minutes: null,      // compte rendu ouvert dans l'editeur
    dirty: {},          // champs modifies, pas encore envoyes
    timer: null,        // sauvegarde programmee
    saving: null,       // envoi en cours (promesse)
    failed: false,      // dernier envoi refuse
    listToken: 0,       // ignore les reponses d'un filtre depasse
    pending: null,      // elements a convertir proposes apres validation
  };

  /* ---------- Outils ---------- */

  function send(method, body) {
    return {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    };
  }

  function fromIso(iso) {
    const parts = String(iso || "").slice(0, 10).split("-").map(Number);
    return new Date(parts[0], (parts[1] || 1) - 1, parts[2] || 1);
  }

  function longDate(iso) {
    if (!iso) return "";
    return fromIso(iso).toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }

  function plural(count, word) {
    return count + " " + word + (count > 1 ? "s" : "");
  }

  function idFromPath(pathname) {
    const match = /^\/comptes-rendus\/(\d+)\/?$/.exec(pathname);
    return match ? Number(match[1]) : null;
  }

  function projectDot(color, name) {
    if (!name) return "";
    const dot = HEX_COLOR.test(color || "") ? ' style="--dot:' + color + '"' : "";
    return '<span class="mn-project"><span class="mn-dot"' + dot + ' aria-hidden="true"></span>' +
      escapeHtml(name) + "</span>";
  }

  function statusBadge(status) {
    return status === "final"
      ? '<span class="badge badge-ok">Validé</span>'
      : '<span class="badge badge-p4">Brouillon</span>';
  }

  /* Hauteur d'une zone de texte ajustee a son contenu : rien n'est coupe,
   * ni a l'ecran ni a l'impression. Une zone masquee reprend sa hauteur. */
  function autosize(el) {
    el.style.height = "auto";
    if (!el.scrollHeight) {
      el.style.height = "";
      return;
    }
    el.style.height = el.scrollHeight + 2 + "px";
  }

  /* ---------- Navigation ---------- */

  function showView(name) {
    listViewEl.hidden = name !== "list";
    editorViewEl.hidden = name !== "editor";
    window.scrollTo(0, 0);
  }

  function route(id) {
    if (id) openEditor(id);
    else openList();
  }

  /* Quitte l'editeur proprement : les modifications en attente partent avant.
   * Renvoie false si elles n'ont pas pu etre enregistrees et que l'on reste. */
  async function leaveEditor() {
    if (!state.minutes) return true;
    const saved = await flush();
    if (!saved && !window.confirm("Des modifications n'ont pas été enregistrées. Quitter quand même ?")) {
      return false;
    }
    closeEditor();
    return true;
  }

  async function navigate(id) {
    if (!(await leaveEditor())) return;
    const url = id ? LIST_URL + "/" + id : LIST_URL;
    if (location.pathname !== url) history.pushState({ minutesId: id || null }, "", url);
    route(id);
  }

  window.addEventListener("popstate", async () => {
    const target = idFromPath(location.pathname);
    if (state.minutes && state.minutes.id === target) return;
    if (state.minutes) {
      const openId = state.minutes.id;
      if (!(await leaveEditor())) {
        history.pushState({ minutesId: openId }, "", LIST_URL + "/" + openId);
        return;
      }
    }
    route(target);
  });

  window.addEventListener("beforeunload", (event) => {
    if (hasPendingChanges()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // Liens internes (retour, liste) : sans rechargement, sauf Ctrl/Maj-clic.
  rootEl.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-open], #mn-back, [data-nav-list]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    navigate(link.dataset.open ? Number(link.dataset.open) : null);
  });

  /* ---------- Liste ---------- */

  function openList() {
    document.title = "NOMAIA — Comptes rendus";
    showView("list");
    loadList();
  }

  async function loadList() {
    const token = ++state.listToken;
    const projectId = filterEl.value;
    listEl.innerHTML = '<div class="skel skel-row"></div><div class="skel skel-row"></div><div class="skel skel-row"></div>';
    try {
      const data = await callApi("/api/minutes" + (projectId ? "?project_id=" + encodeURIComponent(projectId) : ""));
      if (token === state.listToken) renderList(data.minutes || []);
    } catch (error) {
      if (token === state.listToken) listEl.innerHTML = '<p class="error-box">' + escapeHtml(error.message) + "</p>";
    }
  }

  function countsText(minutes) {
    const bits = [plural(minutes.decisions, "décision"), plural(minutes.actions, "action")];
    let text = bits.join(" · ");
    if (minutes.actions_open) text += " (dont " + minutes.actions_open + " à transformer)";
    return text;
  }

  function rowHtml(minutes, rank, animate) {
    const day = fromIso(minutes.held_on);
    return (
      '<a class="card is-hoverable mn-row' + (animate ? " anim-in" : "") + '" style="--i:' + Math.min(rank, 12) + '"' +
        ' href="' + LIST_URL + "/" + minutes.id + '" data-open="' + minutes.id + '">' +
        '<span class="mn-row-date" aria-hidden="true">' +
          "<strong>" + day.getDate() + "</strong>" +
          "<span>" + escapeHtml(day.toLocaleDateString("fr-FR", { month: "short", year: "numeric" })) + "</span>" +
        "</span>" +
        '<span class="mn-row-body">' +
          '<span class="mn-row-title">' + escapeHtml(minutes.title) + window.J.spaceBadge(minutes.space) + "</span>" +
          '<span class="sr-only">, ' + escapeHtml(longDate(minutes.held_on)) + "</span>" +
          '<span class="mn-row-meta">' +
            '<span class="badge ' + (KIND_BADGES[minutes.kind] || "") + '">' +
              escapeHtml(KIND_LABELS[minutes.kind] || minutes.kind) + "</span>" +
            projectDot(minutes.project_color, minutes.project_name) +
            statusBadge(minutes.status) +
          "</span>" +
        "</span>" +
        '<span class="mn-row-counts' + (minutes.actions_open ? " has-open" : "") + '">' +
          escapeHtml(countsText(minutes)) + "</span>" +
      "</a>"
    );
  }

  /* animate : apparition en cascade (coupee pour un rechargement a chaud). */
  function renderList(minutes, animate) {
    if (!minutes.length) {
      listEl.innerHTML = filterEl.value
        ? '<div class="card empty mn-empty"><p class="mn-empty-title">Aucun compte rendu pour ce projet.</p>' +
            '<p>Choisis « Tous les projets » ou crée le premier compte rendu de ce projet.</p></div>'
        : '<div class="card empty mn-empty">' +
            '<p class="mn-empty-title">Pas encore de compte rendu.</p>' +
            "<p>Réunion, intervention chez le client ou formation : note les participants, ce qui s'est dit, " +
            "les décisions et les actions. Les actions deviennent des tâches, les décisions rejoignent le " +
            "registre du projet.</p>" +
            "<p>Tu peux aussi partir d'un événement de l'agenda : ouvre-le et choisis « Compte rendu », " +
            "le titre, la date et le projet sont repris.</p>" +
            '<div class="mn-empty-actions">' +
              '<button type="button" class="btn-primary" data-new>Nouveau compte rendu</button>' +
              '<a class="btn-ghost" href="/agenda">Ouvrir l\'agenda</a>' +
            "</div>" +
          "</div>";
      return;
    }
    listEl.innerHTML = minutes.map((item, rank) => rowHtml(item, rank, animate !== false)).join("");
  }

  filterEl.addEventListener("change", loadList);

  /* ---------- Creation ---------- */

  function openNew() {
    newFormEl.reset();
    newErrorEl.hidden = true;
    newFields.date.value = rootEl.dataset.today;
    newFields.project.value = filterEl.value;
    newModalEl.showModal();
    newFields.title.focus();
  }

  document.getElementById("mn-new").addEventListener("click", openNew);
  listEl.addEventListener("click", (event) => {
    if (event.target.closest("[data-new]")) openNew();
  });
  document.getElementById("mn-new-cancel").addEventListener("click", () => newModalEl.close());

  newFormEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const saveEl = document.getElementById("mn-new-save");
    saveEl.disabled = true;
    newErrorEl.hidden = true;
    try {
      const created = await callApi("/api/minutes", send("POST", {
        title: newFields.title.value,
        held_on: newFields.date.value,
        kind: newFields.kind.value,
        project_id: newFields.project.value ? Number(newFields.project.value) : null,
      }));
      newModalEl.close();
      navigate(created.id);
    } catch (error) {
      newErrorEl.textContent = error.message;
      newErrorEl.hidden = false;
    } finally {
      saveEl.disabled = false;
    }
  });

  /* ---------- Editeur : chargement ---------- */

  async function openEditor(id) {
    showView("editor");
    missingEl.hidden = true;
    docEl.hidden = true;
    try {
      const minutes = await callApi("/api/minutes/" + id);
      if (idFromPath(location.pathname) !== id) return;  // l'utilisateur est deja reparti
      fillEditor(minutes);
    } catch (error) {
      missingEl.hidden = false;
      missingEl.querySelector("p").textContent = error.message === "Compte rendu introuvable."
        ? "Ce compte rendu est introuvable : il a peut-être été supprimé."
        : error.message;
    }
  }

  function closeEditor() {
    clearTimeout(state.timer);
    state.timer = null;
    state.minutes = null;
    state.dirty = {};
    state.failed = false;
  }

  /* Le projet d'un compte rendu peut etre termine, donc absent de la liste. */
  function ensureProjectOption(minutes) {
    if (!minutes.project_id) return;
    const value = String(minutes.project_id);
    if (Array.from(projectEl.options).some((option) => option.value === value)) return;
    const option = document.createElement("option");
    option.value = value;
    option.textContent = minutes.project_name || "Projet " + value;
    projectEl.appendChild(option);
  }

  /* quiet : rechargement a chaud, sans cascade ni remontage des pieces jointes. */
  function fillEditor(minutes, quiet) {
    state.minutes = minutes;
    state.dirty = {};
    state.failed = false;
    ensureProjectOption(minutes);
    fieldEls.forEach((el) => {
      const value = minutes[el.dataset.field];
      el.value = value == null ? "" : String(value);
    });
    docEl.hidden = false;
    applyKind();
    refreshHead();
    renderItems(!quiet);
    fieldEls.filter((el) => el.tagName === "TEXTAREA").forEach(autosize);
    setSaveState("saved");
    if (!quiet) mountAttachments(minutes.id);
  }

  /* Pieces jointes (composant commun, static/js/attachments.js). Le conteneur
   * est remplace a chaque ouverture : mount() y pose ses ecouteurs de
   * glisser-deposer, qui s'empileraient d'un compte rendu a l'autre. */
  function mountAttachments(id) {
    const current = document.getElementById("mn-attachments");
    if (!current || !window.JAttachments) return;
    const fresh = document.createElement("div");
    fresh.id = "mn-attachments";
    current.replaceWith(fresh);
    window.JAttachments.mount(fresh, "minutes", id);
  }

  /* Sections propres au type : formation (sujets, points a revoir) ;
   * pour une intervention, les notes deviennent le travail realise. */
  function applyKind() {
    const kind = state.minutes.kind;
    docEl.querySelectorAll("[data-kinds]").forEach((section) => {
      section.hidden = !section.dataset.kinds.split(" ").includes(kind);
      if (!section.hidden) section.querySelectorAll("textarea").forEach(autosize);
    });
    notesLabelEl.textContent = kind === "intervention" ? "Travail réalisé" : "Notes";
    document.getElementById("mn-notes").placeholder = kind === "intervention"
      ? "Ce qui a été fait sur place : paramétrage, corrections, livraisons."
      : "Tout le reste.";
    markEmptySections();
  }

  /* Les sections vides ne s'impriment pas. */
  function markEmptySections() {
    docEl.querySelectorAll(".mn-section").forEach((section) => {
      const area = section.querySelector("textarea");
      section.classList.toggle("is-empty", !area.value.trim());
    });
  }

  function refreshHead() {
    const minutes = state.minutes;
    const final = minutes.status === "final";
    statusEl.className = "badge " + (final ? "badge-ok" : "badge-p4");
    statusEl.textContent = final ? "Validé" : "Brouillon";
    finalizeEl.hidden = final;
    reopenEl.hidden = !final;
    document.title = "NOMAIA — " + (minutes.title || "Compte rendu");
    printTitleEl.textContent = minutes.title || "Compte rendu";

    const projectName = minutes.project_id
      ? (projectEl.selectedOptions[0] ? projectEl.selectedOptions[0].textContent : minutes.project_name)
      : "";
    printMetaEl.textContent = [
      KIND_LABELS[minutes.kind] || "",
      longDate(minutes.held_on),
      projectName ? "Projet " + projectName : "",
      final ? "Validé" : "Brouillon",
    ].filter(Boolean).join(" · ");
  }

  /* ---------- Editeur : sauvegarde automatique ---------- */

  function hasPendingChanges() {
    return Boolean(state.minutes) &&
      (Object.keys(state.dirty).length > 0 || Boolean(state.saving) || Boolean(state.timer));
  }

  function setSaveState(name) {
    const labels = { saved: "Enregistré", saving: "Enregistrement…", error: "Non enregistré" };
    saveStateEl.dataset.state = name;
    saveStateEl.textContent = labels[name];
  }

  function scheduleSave() {
    clearTimeout(state.timer);
    setSaveState("saving");
    state.timer = setTimeout(flush, SAVE_DELAY_MS);
  }

  /* Envoie les champs modifies. Renvoie true si plus rien n'attend. */
  async function flush() {
    clearTimeout(state.timer);
    state.timer = null;
    while (state.saving) await state.saving;
    if (!state.minutes) return true;
    if (!Object.keys(state.dirty).length) return !state.failed;

    const id = state.minutes.id;
    const payload = state.dirty;
    state.dirty = {};
    setSaveState("saving");
    state.saving = callApi("/api/minutes/" + id, send("PATCH", payload))
      .then((data) => {
        state.failed = false;
        if (state.minutes && state.minutes.id === id) applySaved(data);
        return true;
      })
      .catch((error) => {
        // Les champs refuses restent a envoyer : la prochaine frappe les renverra.
        if (state.minutes && state.minutes.id === id) state.dirty = Object.assign({}, payload, state.dirty);
        state.failed = true;
        toast(error.message, true);
        return false;
      })
      .finally(() => { state.saving = null; });
    const ok = await state.saving;
    if (state.minutes && state.minutes.id === id && !state.timer) {
      setSaveState(state.failed ? "error" : Object.keys(state.dirty).length ? "saving" : "saved");
    }
    return ok && !Object.keys(state.dirty).length;
  }

  /* Reponse du serveur : on garde ce que l'utilisateur a tape depuis l'envoi. */
  function applySaved(data) {
    Object.keys(data).forEach((key) => {
      if (key !== "items" && !(key in state.dirty)) state.minutes[key] = data[key];
    });
    refreshHead();
  }

  fieldEls.forEach((el) => {
    const isText = el.tagName === "TEXTAREA" || el.type === "text";
    el.addEventListener(isText ? "input" : "change", () => {
      if (!state.minutes) return;
      const field = el.dataset.field;
      let value = el.value;
      if (field === "project_id") value = value ? Number(value) : null;
      state.dirty[field] = value;
      state.minutes[field] = value;
      if (el.tagName === "TEXTAREA") {
        autosize(el);
        el.closest(".mn-section").classList.toggle("is-empty", !value.trim());
      }
      if (field === "kind") applyKind();
      if (field === "project_id") {
        state.minutes.project_name = value ? projectEl.selectedOptions[0].textContent : null;
        renderItems(false, "decision");
      }
      refreshHead();
      if (isText) scheduleSave();
      else flush();
    });
  });

  // Fenetre redimensionnee : les retours a la ligne changent.
  window.addEventListener("resize", () => {
    if (state.minutes) docEl.querySelectorAll("textarea").forEach(autosize);
  });

  /* ---------- Editeur : elements (decisions, actions, demandes) ---------- */

  function conversionHtml(item) {
    if (item.type === "decision") {
      if (item.note_id) return '<span class="badge badge-ok">Au registre</span>';
      const blocked = !state.minutes.project_id;
      return '<button type="button" class="btn-ghost mn-convert" data-act="register"' +
        (blocked ? ' aria-disabled="true" title="' + escapeHtml(NO_PROJECT_HINT) + '"' : "") +
        ">Inscrire au registre</button>";
    }
    if (!item.task_id) {
      return '<button type="button" class="btn-ghost mn-convert" data-act="task">Créer la tâche</button>';
    }
    const done = item.task_status === "done";
    const title = item.task_title || "tâche n° " + item.task_id;
    return '<a class="mn-task-link" href="/plan?tache=' + item.task_id + '">Tâche : ' + escapeHtml(title) + "</a>" +
      (item.task_status
        ? ' <span class="badge ' + (done ? "badge-ok" : "badge-p3") + '">' +
            escapeHtml(TASK_STATUS_LABELS[item.task_status] || item.task_status) + "</span>"
        : ' <span class="badge badge-p4">Supprimée</span>');
  }

  /* Responsable et echeance en clair, pour l'impression (les champs sont masques). */
  function printDetails(item) {
    return [
      item.owner ? "Responsable : " + item.owner : "",
      item.due ? "Échéance : " + J.formatDeadline(item.due) : "",
    ].filter(Boolean).join(" · ");
  }

  function itemHtml(item, rank, animate) {
    const label = ITEM_LABELS[item.type];
    const isAction = item.type === "action";
    return (
      '<li class="mn-item' + (animate ? " anim-in" : "") + '" style="--i:' + Math.min(rank, 12) + '"' +
        ' data-id="' + item.id + '">' +
        '<div class="mn-item-row">' +
          '<textarea class="mn-item-text" rows="1" maxlength="2000" data-item-field="text"' +
            ' aria-label="Texte de ' + label + '">' + escapeHtml(item.text) + "</textarea>" +
          '<button type="button" class="icon-btn danger mn-item-del no-print" data-act="delete"' +
            ' title="Supprimer ' + label + '" aria-label="Supprimer ' + label + '">✕</button>' +
        "</div>" +
        (isAction
          ? '<div class="mn-item-meta no-print">' +
              '<input type="text" maxlength="200" data-item-field="owner" placeholder="Responsable"' +
                ' aria-label="Responsable" value="' + escapeHtml(item.owner || "") + '">' +
              '<input type="date" data-item-field="due" aria-label="Échéance" title="Échéance"' +
                ' value="' + escapeHtml(item.due || "") + '">' +
            "</div>"
          : "") +
        '<p class="mn-item-print print-only">' + escapeHtml(printDetails(item)) + "</p>" +
        '<div class="mn-item-foot">' + conversionHtml(item) + "</div>" +
      "</li>"
    );
  }

  function itemsOf(type) {
    return state.minutes.items.filter((item) => item.type === type);
  }

  /* Rend une liste d'elements (ou les trois). animate : apparition en cascade. */
  function renderItems(animate, onlyType) {
    (onlyType ? [onlyType] : ITEM_TYPES).forEach((type) => {
      const listNode = docEl.querySelector('[data-list="' + type + '"]');
      const items = itemsOf(type);
      listNode.innerHTML = items.length
        ? items.map((item, rank) => itemHtml(item, rank, animate)).join("")
        : '<li class="mn-item-empty">' + EMPTY_ITEMS[type] + "</li>";
      listNode.closest(".mn-item-block").classList.toggle("is-empty", !items.length);
      docEl.querySelector('[data-count="' + type + '"]').textContent = items.length ? String(items.length) : "";
      listNode.querySelectorAll("textarea").forEach(autosize);
    });
  }

  function findItem(id) {
    return state.minutes.items.find((item) => item.id === id);
  }

  // Ajout rapide : texte puis Entree (responsable et echeance pour une action).
  docEl.querySelectorAll("form[data-add]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!state.minutes) return;
      const type = form.dataset.add;
      const textEl = form.elements.text;
      const text = textEl.value.trim();
      if (!text) {
        textEl.focus();
        return;
      }
      const body = { type: type, text: text };
      if (form.elements.owner) body.owner = form.elements.owner.value;
      if (form.elements.due) body.due = form.elements.due.value || null;
      const minutesId = state.minutes.id;
      textEl.disabled = true;
      try {
        const item = await callApi("/api/minutes/" + minutesId + "/items", send("POST", body));
        if (!state.minutes || state.minutes.id !== minutesId) return;
        state.minutes.items.push(item);
        renderItems(false, type);
        form.reset();
      } catch (error) {
        toast(error.message, true);
      } finally {
        textEl.disabled = false;
        textEl.focus();
      }
    });
  });

  // Modification en place : enregistree a la sortie du champ (ou Entree).
  docEl.addEventListener("change", async (event) => {
    const input = event.target.closest("[data-item-field]");
    if (!input || !state.minutes) return;
    const id = Number(input.closest(".mn-item").dataset.id);
    const item = findItem(id);
    const field = input.dataset.itemField;
    if (!item) return;
    const value = field === "due" ? input.value || null : input.value;
    if ((item[field] || "") === (value || "")) return;
    try {
      const saved = await callApi("/api/minutes/items/" + id, send("PATCH", { [field]: value }));
      item.text = saved.text;
      item.owner = saved.owner;
      item.due = saved.due;
      if (field === "text") input.value = saved.text;
      input.closest(".mn-item").querySelector(".mn-item-print").textContent = printDetails(item);
    } catch (error) {
      input.value = item[field] || "";
      if (input.tagName === "TEXTAREA") autosize(input);
      toast(error.message, true);
    }
  });

  docEl.addEventListener("input", (event) => {
    if (event.target.matches(".mn-item-text")) autosize(event.target);
  });

  docEl.addEventListener("keydown", (event) => {
    const input = event.target.closest(".mn-item-text");
    if (!input) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      input.blur();
    } else if (event.key === "Escape") {
      const item = findItem(Number(input.closest(".mn-item").dataset.id));
      if (item) input.value = item.text;
      autosize(input);
      input.blur();
    }
  });

  // Boutons d'un element : suppression et conversions.
  docEl.addEventListener("click", async (event) => {
    const button = event.target.closest(".mn-item [data-act]");
    if (!button || !state.minutes) return;
    const id = Number(button.closest(".mn-item").dataset.id);
    const item = findItem(id);
    if (!item) return;
    const act = button.dataset.act;

    if (button.getAttribute("aria-disabled") === "true") {
      toast(NO_PROJECT_HINT, true);
      return;
    }
    button.disabled = true;
    try {
      if (act === "delete") {
        await callApi("/api/minutes/items/" + id, send("DELETE"));
        state.minutes.items = state.minutes.items.filter((other) => other.id !== id);
      } else {
        await convertItem(item);
        toast(act === "task" ? "Tâche créée." : "Décision inscrite au registre.");
      }
      renderItems(false, item.type);
    } catch (error) {
      button.disabled = false;
      toast(error.message, true);
    }
  });

  /* Transforme un element : tache pour une action ou une demande, registre pour une decision. */
  async function convertItem(item) {
    if (item.type === "decision") {
      const entry = await callApi("/api/minutes/items/" + item.id + "/register", send("POST"));
      item.note_id = entry.id;
      return;
    }
    const task = await callApi("/api/minutes/items/" + item.id + "/task", send("POST"));
    item.task_id = task.id;
    item.task_title = task.title;
    item.task_status = task.status;
  }

  /* ---------- Editeur : validation ---------- */

  finalizeEl.addEventListener("click", async () => {
    if (!state.minutes) return;
    const id = state.minutes.id;
    finalizeEl.disabled = true;
    try {
      if (!(await flush())) return;
      const result = await callApi("/api/minutes/" + id + "/finalize", send("POST"));
      if (!state.minutes || state.minutes.id !== id) return;
      applySaved(result.minutes);
      state.minutes.items = result.minutes.items;
      renderItems(false);
      const actions = result.pending_actions || [];
      const decisions = result.pending_decisions || [];
      if (!actions.length && !decisions.length) {
        toast("Compte rendu validé.");
        return;
      }
      openPending(actions, decisions);
    } catch (error) {
      toast(error.message, true);
    } finally {
      finalizeEl.disabled = false;
    }
  });

  reopenEl.addEventListener("click", () => {
    if (!state.minutes) return;
    state.dirty.status = "draft";
    state.minutes.status = "draft";
    refreshHead();
    flush();
  });

  function openPending(actions, decisions) {
    const hasProject = Boolean(state.minutes.project_id);
    const convertible = actions.concat(hasProject ? decisions : []);
    state.pending = convertible;

    const parts = [];
    if (actions.length) parts.push(plural(actions.length, "action") + " sans tâche");
    if (decisions.length) parts.push(plural(decisions.length, "décision") + " hors registre");
    pendingTextEl.textContent = "Il reste " + parts.join(" et ") + ".";

    pendingListEl.innerHTML = actions.concat(decisions).map((item) =>
      "<li><span class=\"badge " + (item.type === "decision" ? "badge-indigo" : "badge-p3") + "\">" +
        (item.type === "decision" ? "Décision" : "Action") + "</span> " + escapeHtml(item.text) + "</li>"
    ).join("");

    pendingHintEl.hidden = hasProject || !decisions.length;
    pendingHintEl.textContent = actions.length
      ? "Sans projet, seules les actions seront converties. " + NO_PROJECT_HINT
      : NO_PROJECT_HINT;
    pendingConvertEl.hidden = !convertible.length;
    pendingConvertEl.disabled = false;
    pendingModalEl.showModal();
  }

  document.getElementById("mn-pending-later").addEventListener("click", () => {
    pendingModalEl.close();
    toast("Compte rendu validé.");
  });

  pendingConvertEl.addEventListener("click", async () => {
    if (!state.minutes || !state.pending) return;
    const id = state.minutes.id;
    pendingConvertEl.disabled = true;
    let done = 0;
    const errors = [];
    // Un appel apres l'autre : chaque conversion relit le compte rendu cote serveur.
    for (const pending of state.pending) {
      const item = findItem(pending.id) || pending;
      try {
        await convertItem(item);
        done += 1;
      } catch (error) {
        errors.push(error.message);
      }
    }
    pendingModalEl.close();
    state.pending = null;
    try {
      const fresh = await callApi("/api/minutes/" + id);
      if (state.minutes && state.minutes.id === id) state.minutes.items = fresh.items;
    } catch (error) {
      // Le rendu local suffit : les conversions reussies y sont deja.
    }
    if (state.minutes && state.minutes.id === id) renderItems(false);
    if (errors.length) {
      toast(plural(done, "élément") + " converti" + (done > 1 ? "s" : "") + ", " + errors.length +
        " en échec : " + errors[0], true);
    } else {
      toast("Compte rendu validé, " + plural(done, "élément") + " converti" + (done > 1 ? "s" : "") + ".");
    }
  });

  /* ---------- Editeur : impression et suppression ---------- */

  /* Recopie chaque zone de texte dans un bloc imprimable : une zone de
   * saisie s'imprime a sa hauteur d'ecran, donc parfois tronquee. */
  function preparePrint() {
    if (!state.minutes) return;
    markEmptySections();
    docEl.querySelectorAll("textarea").forEach((area) => {
      let copy = area.nextElementSibling;
      if (!copy || !copy.classList.contains("mn-print-text")) {
        copy = document.createElement("p");
        copy.className = "mn-print-text print-only";
        area.after(copy);
      }
      copy.textContent = area.value;
    });
  }

  // beforeprint couvre aussi Ctrl+P.
  window.addEventListener("beforeprint", preparePrint);
  document.getElementById("mn-print").addEventListener("click", () => window.print());

  document.getElementById("mn-delete").addEventListener("click", () => deleteModalEl.showModal());
  document.getElementById("mn-delete-cancel").addEventListener("click", () => deleteModalEl.close());

  document.getElementById("mn-delete-confirm").addEventListener("click", async (event) => {
    if (!state.minutes) return;
    const button = event.currentTarget;
    const id = state.minutes.id;
    button.disabled = true;
    try {
      clearTimeout(state.timer);
      state.timer = null;
      while (state.saving) await state.saving;
      await callApi("/api/minutes/" + id, send("DELETE"));
      deleteModalEl.close();
      closeEditor();
      toast("Compte rendu supprimé.");
      navigate(null);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  /* ---------- Rechargement a chaud ---------- */

  /* Liste : relue sans squelette ni cascade, filtre garde. Editeur : relu
   * seulement si rien n'est en cours (aucune modification en attente, aucun
   * champ de l'editeur actif), sinon differe ; il prend alors la version
   * enregistree. */
  function editorBusy() {
    return hasPendingChanges() || docEl.contains(document.activeElement);
  }

  async function liveReload() {
    if (!state.minutes) {
      if (listViewEl.hidden) return true; // editeur en cours d'ouverture
      const token = ++state.listToken;
      const projectId = filterEl.value;
      try {
        const data = await callApi("/api/minutes" + (projectId ? "?project_id=" + encodeURIComponent(projectId) : ""));
        if (token === state.listToken) renderList(data.minutes || [], false);
      } catch (error) {
        // Serveur injoignable : la liste affichee reste en place.
      }
      return true;
    }
    if (editorBusy()) return false;
    const id = state.minutes.id;
    let fresh;
    try {
      fresh = await callApi("/api/minutes/" + id);
    } catch (error) {
      return true; // supprime ailleurs ou serveur injoignable : l'editeur reste tel quel
    }
    if (!state.minutes || state.minutes.id !== id) return true;
    if (editorBusy()) return false;
    if (JSON.stringify(fresh) !== JSON.stringify(state.minutes)) fillEditor(fresh, true);
    return true;
  }

  J.live.onChange(liveReload);

  /* ---------- Demarrage ---------- */

  const initialId = Number(rootEl.dataset.minutesId) || null;
  history.replaceState({ minutesId: initialId }, "", location.href);
  route(initialId);
})();
