/* NOMAIA — plan du jour : creneaux horaires et charge de la journee. */

const { toast, callApi, escapeHtml, formatDuration, formatDeadline } = window.J;

// Tache dont le chronometre tourne (etiquette « En cours ») et rafraichissement de la colonne laterale.
let runningTaskId = null;
let sideTimer = null;

const bodyEl = document.getElementById("plan-body");
const overflowBox = document.getElementById("overflow-box");
const overflowList = document.getElementById("overflow-list");
const settingsModal = document.getElementById("settings-modal");

const PRIORITY_LABELS = { 1: "Critique", 2: "Haute", 3: "Normale", 4: "Basse" };
const WINDOW_LABELS = { morning: "Matin", afternoon: "Après-midi", perso: "Perso" };

let currentDate = document.getElementById("header-date").dataset.today;

const resetOrderEl = document.getElementById("pl-reset-order");
const hintEl = document.getElementById("pl-hint");
const doneToggleEl = document.getElementById("pl-done-toggle");

// Taches deja faites aujourd'hui dans le fil : choix propre a cet appareil,
// eteint par defaut pour garder le plan centre sur ce qui reste a faire.
const SHOW_DONE_KEY = "nomaia.plan.showDone";
let showDone = readShowDone();

function readShowDone() {
  try {
    return localStorage.getItem(SHOW_DONE_KEY) === "1";
  } catch (error) {
    return false;
  }
}

function saveShowDone(value) {
  try {
    localStorage.setItem(SHOW_DONE_KEY, value ? "1" : "0");
  } catch (error) {
    // Stockage indisponible : le choix vaut pour la page ouverte seulement.
  }
}

// Apparition en cascade des lignes : au chargement et au changement de jour
// seulement. Apres une action, la liste se redessine sans rejouer l'entree.
let animateRows = true;
let rowIndex = 0;

// Tache que l'on vient de cocher : sa coche apparait avec un petit rebond.
let poppedId = null;

/* Classe et decalage d'entree d'une ligne (index plafonne a 12). */
function enter() {
  if (!animateRows) return { cls: "", style: "" };
  const index = Math.min(rowIndex++, 12);
  return { cls: " anim-in", style: ' style="--i:' + index + '"' };
}

/* Case a cocher dans une zone de clic elargie (cible tactile de 38 px et plus).
 * Le label ne porte pas de data-action : seul le clic relaye a la case agit. */
function checkBox(task, action, checked, title) {
  const pop = checked && String(task.id) === poppedId ? " anim-pop" : "";
  return (
    '<label class="check-hit" title="' + title + '">' +
      '<input type="checkbox" class="task-check' + pop + '" data-action="' + action + '"' +
        (checked ? " checked" : "") + ' aria-label="' + title + '">' +
    "</label>"
  );
}

/* ---------- Rendu ---------- */

function taskMeta(task) {
  const meta = ["P" + task.priority + " · " + PRIORITY_LABELS[task.priority]];
  if (task.deadline) meta.push("échéance " + formatDeadline(task.deadline));
  if (task.project_name) meta.push(escapeHtml(task.project_name));
  if (task.needs_review) meta.push("à vérifier");
  return meta.join(" · ");
}

function placementHint(slot) {
  if (slot.energy_bonus > 0) {
    return slot.window === "morning"
      ? " · placée tôt, tâche exigeante"
      : " · placée l'après-midi, tâche légère";
  }
  return "";
}

/* Etiquettes de la maquette : en cours, anomalie, livrable, bloquee, attente client. */
function slotTags(task) {
  const tags = [];
  if (runningTaskId === task.id) tags.push('<span class="badge badge-accent">En cours</span>');
  if (task.space === "perso") tags.push('<span class="badge badge-outline">Perso</span>');
  if (task.kind === "anomaly") tags.push('<span class="badge badge-p1">Anomalie</span>');
  if (task.kind === "deliverable") tags.push('<span class="badge badge-indigo">Livrable</span>');
  if (task.blocked) tags.push('<span class="badge badge-p1">Bloquée</span>');
  else if (task.side === "client" || task.side === "both") tags.push('<span class="badge badge-p2">Attente client</span>');
  if (task.deadline_shifts >= 2) tags.push('<span class="badge badge-p2">reportée ' + task.deadline_shifts + " fois</span>");
  return tags.length ? '<span class="slot-tags">' + tags.join("") + "</span>" : "";
}

function slotRow(slot) {
  if (slot.masked) return maskedRow(slot.start, slot.end, slot.task.title);
  const task = slot.task;
  const pin = slot.pinned
    ? '<button type="button" class="icon-btn pin-on" data-action="unpin" title="Épinglée à la main : rendre au classement automatique" aria-label="Rendre au classement automatique">📌</button>'
    : '<button type="button" class="icon-btn" data-action="now" title="Faire maintenant (en tête de journée)" aria-label="Faire maintenant">⇧</button>';
  const fx = enter();
  return (
    '<div class="slot plan-slot' + (slot.pinned ? " is-pinned" : "") +
      (runningTaskId === task.id ? " is-running" : "") + fx.cls + '"' + fx.style +
      ' draggable="true" data-priority="' + task.priority + '" data-id="' + task.id + '">' +
      '<span class="drag-handle" title="Glisser pour réordonner" aria-hidden="true">⋮⋮</span>' +
      '<div class="slot-time">' +
        "<strong>" + slot.start + "</strong><span>" + slot.end + "</span>" +
      "</div>" +
      checkBox(task, "done", false, "Marquer terminée") +
      '<div class="slot-body">' +
        '<button type="button" class="slot-title as-link" data-action="detail" title="Détailler">' +
          escapeHtml(task.title) + "</button>" +
        '<span class="slot-meta">' + taskMeta(task) + " · " +
          formatDuration(slot.duration_min) + " réservées" +
          (slot.pinned ? " · ordre choisi" : placementHint(slot)) + "</span>" +
      "</div>" +
      slotTags(task) +
      '<span class="slot-prio" aria-hidden="true">P' + task.priority + "</span>" +
      '<div class="slot-actions">' +
        pin +
        '<button type="button" class="icon-btn" data-action="timer" title="Lancer le minuteur" aria-label="Lancer le minuteur">▶︎</button>' +
        '<button type="button" class="icon-btn" data-action="why" title="Pourquoi ici ?" aria-label="Pourquoi ici ?">?</button>' +
      "</div>" +
    "</div>"
  );
}

// Creneau de l'autre espace (selecteur Pro ou Perso) : le temps reste pris,
// le detail est cache, aucune action possible.
function maskedRow(start, end, label) {
  const fx = enter();
  return (
    '<div class="slot plan-slot is-masked' + fx.cls + '"' + fx.style + '>' +
      '<div class="slot-time"><strong>' + escapeHtml(start) + "</strong><span>" + escapeHtml(end) + "</span></div>" +
      '<div class="slot-body"><span class="slot-title">' + escapeHtml(label) + "</span></div>" +
    "</div>"
  );
}

const EVENT_LABELS = { meeting: "Rendez-vous", focus: "Bloque", absence: "Absent", personal: "Perso" };

// Rendu d'une reunion dans le fil du jour. Meme gabarit qu'un creneau de
// travail, sans case a cocher : une reunion ne se termine pas, elle se passe.
function eventRow(item) {
  if (item.masked) return maskedRow(item.starts_at.slice(11, 16), item.ends_at.slice(11, 16), item.title);
  const lieu = item.location ? " · " + escapeHtml(item.location) : "";
  const projet = item.project_name ? " · " + escapeHtml(item.project_name) : "";
  const fx = enter();
  return (
    '<div class="slot event' + fx.cls + '"' + fx.style + ' data-kind="' + escapeHtml(item.kind) + '">' +
      '<span class="slot-hour">' + escapeHtml(item.starts_at.slice(11, 16)) + "</span>" +
      '<div class="slot-body">' +
        '<span class="slot-title">' + escapeHtml(item.title) + "</span>" +
        '<span class="slot-meta">' +
          escapeHtml(EVENT_LABELS[item.kind] || item.kind) + " · " +
          escapeHtml(item.starts_at.slice(11, 16)) + "-" +
          escapeHtml(item.ends_at.slice(11, 16)) + lieu + projet +
        "</span>" +
      "</div>" +
    "</div>"
  );
}

// Tache deja faite aujourd'hui, a son heure dans le fil : grisee et cochee
// comme au bilan d'un jour passe ; decocher la rouvre. Pas de glisser-deposer.
function doneRow(task) {
  const spent = task.tracked_today_min ? " · " + formatDuration(task.tracked_today_min) + " passées" : "";
  const fx = enter();
  return (
    '<div class="slot plan-slot plan-done is-done' + fx.cls + '"' + fx.style +
      ' data-priority="' + task.priority + '" data-id="' + task.id + '">' +
      '<div class="slot-time"><strong>' + escapeHtml(task.at) + "</strong></div>" +
      checkBox(task, "undo-past", true, "Décocher : rouvrir la tâche") +
      '<div class="slot-body">' +
        '<button type="button" class="slot-title as-link" data-action="detail" title="Détailler">' +
          escapeHtml(task.title) + "</button>" +
        '<span class="slot-meta">' + taskMeta(task) + spent + "</span>" +
      "</div>" +
    "</div>"
  );
}

// Creneaux de travail et reunions dans un seul fil horaire : la journee se
// lit comme elle se deroule, pas en deux listes separees.
function timeline(plan) {
  const entrees = (plan.slots || []).map((slot) => ({ at: slot.start, kind: "task", slot: slot }));
  (plan.events || []).forEach((item) => {
    entrees.push({ at: item.starts_at.slice(11, 16), kind: "event", event: item });
  });
  if (showDone) {
    (plan.done_today || []).forEach((task) => {
      entrees.push({ at: task.at, kind: "done", task: task });
    });
  }
  return entrees.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
}

// Bouton « Faites (N) » : visible aujourd'hui seulement, s'il y a du travail fait.
function renderDoneToggle(plan) {
  const count = (plan.done_today || []).length;
  doneToggleEl.hidden = !count;
  doneToggleEl.textContent = "Faites (" + count + ")";
  doneToggleEl.setAttribute("aria-pressed", String(showDone));
}

// Echeances en danger : travail restant superieur au temps libre d'ici la.
// Calculees par le serveur pour aujourd'hui seulement.
function renderRisks(risks) {
  const box = document.getElementById("pl-risks");
  if (!box) return;
  if (!risks || !risks.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }

  const lignes = risks.map((risk) => {
    const quand = fromIsoDay(risk.deadline).toLocaleDateString("fr-FR", {
      weekday: "short",
      day: "numeric",
      month: "short",
    });
    const titres = (risk.tasks || []).map((task) => escapeHtml(task.title)).join(", ");
    return (
      "<li><strong>" + escapeHtml(quand) + "</strong> — " + titres + " : " +
      formatDuration(risk.work_min) + " de travail, " +
      formatDuration(risk.free_min) + " libres d'ici là</li>"
    );
  });

  box.innerHTML =
    '<p class="plan-risks-head">Échéances en danger</p><ul>' + lignes.join("") +
    "</ul><p class=\"plan-risks-hint\">Décale, délègue ou réduis : en l'état, tout ne tiendra pas.</p>";
  box.hidden = false;
}

function fromIsoDay(iso) {
  const [annee, mois, jour] = String(iso).split("-").map(Number);
  return new Date(annee, mois - 1, jour);
}

function render(plan) {
  renderDoneToggle(plan);
  if (plan.past) {
    renderPast(plan);
    return;
  }
  hintEl.hidden = false;
  resetOrderEl.hidden = !plan.pinned_count;
  renderRisks(plan.risks);

  const day = new Date(plan.date + "T00:00:00");
  document.getElementById("pl-date").textContent = day.toLocaleDateString("fr-FR", {
    weekday: "long",
    day: "numeric",
    month: "long",
  });

  const load = plan.load_pct;
  document.getElementById("pl-load-text").textContent =
    formatDuration(plan.planned_min) + " planifiées sur " +
    formatDuration(plan.capacity_min) + " (" + load + " %)";

  const agenda = document.getElementById("pl-agenda");
  if (agenda) {
    // Une capacite reduite sans explication passe pour une erreur de calcul.
    if (plan.busy_min) {
      agenda.textContent =
        formatDuration(plan.busy_min) + " d'agenda, " +
        (plan.events || []).length + " rendez-vous";
      agenda.hidden = false;
    } else {
      agenda.hidden = true;
    }
  }

  const fill = document.getElementById("pl-load-fill");
  fill.style.width = Math.min(load, 100) + "%";
  fill.classList.toggle("full", load >= 90);
  renderLoadValues(plan.planned_min, plan.capacity_min, plan.tracked_day_min);

  const fil = timeline(plan);

  if (plan.day_off) {
    renderDayOff(plan, fil);
    return;
  }

  if (!fil.length) {
    bodyEl.innerHTML = '<p class="empty">Rien à placer aujourd\'hui. Journée libre.</p>';
  } else {
    let html = "";
    let window = null;
    fil.forEach((entree) => {
      if (entree.kind === "event") {
        html += eventRow(entree.event);
        return;
      }
      // Une periode qui n'a que du travail fait garde son titre.
      const entryWindow = entree.kind === "done" ? entree.task.window : entree.slot.window;
      if (entryWindow !== window && WINDOW_LABELS[entryWindow]) {
        window = entryWindow;
        html += '<h3 class="window-title">' + WINDOW_LABELS[window] + "</h3>";
      }
      html += entree.kind === "done" ? doneRow(entree.task) : slotRow(entree.slot);
    });
    bodyEl.innerHTML = html;
  }

  if (plan.overflow.length) {
    overflowBox.hidden = false;
    overflowList.innerHTML = plan.overflow
      .map(
        (task) => {
          const fx = enter();
          return '<div class="slot compact' + fx.cls + '"' + fx.style +
            ' data-priority="' + task.priority + '" data-id="' + task.id + '">' +
            checkBox(task, "done", false, "Marquer terminée") +
            '<div class="slot-body">' +
              '<button type="button" class="slot-title as-link" data-action="detail" title="Détailler">' +
                escapeHtml(task.title) + "</button>" +
              '<span class="slot-meta">' + taskMeta(task) + " · " +
                formatDuration(task.estimate_min) + "</span>" +
            "</div>" +
            '<button type="button" class="btn-ghost slot-now" data-action="now" title="La placer en tête de journée">Maintenant</button>' +
          "</div>";
        }
      )
      .join("");
  } else {
    overflowBox.hidden = true;
  }
}

// Jour non travaille : le serveur ne planifie rien (capacite nulle). Le bandeau
// remplace la charge et l'etat vide ; les evenements du jour restent visibles.
function renderDayOff(plan, fil) {
  document.getElementById("pl-load-text").textContent = "Jour de repos";
  document.getElementById("pl-agenda").hidden = true;
  const fill = document.getElementById("pl-load-fill");
  fill.style.width = "0%";
  fill.classList.remove("full");
  renderLoadValues(null, 0, plan.tracked_day_min);

  let html =
    '<div class="plan-dayoff"><strong>Jour de repos</strong> — ' +
    escapeHtml(plan.day_off) + " : rien n'est planifié.</div>";
  // Travail fait un jour de repos (bouton allume) : a son heure, sans periode.
  fil
    .filter((entree) => entree.kind === "event" || entree.kind === "done")
    .forEach((entree) => { html += entree.kind === "done" ? doneRow(entree.task) : eventRow(entree.event); });
  bodyEl.innerHTML = html;

  // Les taches non placees ne sont pas en retard un jour de repos : pas d'alerte.
  overflowBox.hidden = true;
}

/* ---------- Jour passe : bilan et rattrapage ---------- */

function pastRow(task, done) {
  const meta = taskMeta(task) + " · " + formatDuration(task.estimate_min) +
    (task.tracked_min ? " · " + formatDuration(task.tracked_min) + " suivies" : "");
  const action = done
    ? checkBox(task, "undo-past", true, "Décocher : elle n'était pas finie")
    : checkBox(task, "done-on", false, "Faite ce jour-là");
  const fx = enter();
  return (
    '<div class="slot compact' + (done ? " is-done" : "") + fx.cls + '"' + fx.style +
      ' data-priority="' + task.priority + '" data-id="' + task.id + '">' +
      action +
      '<div class="slot-body">' +
        '<button type="button" class="slot-title as-link" data-action="detail" title="Détailler">' +
          escapeHtml(task.title) + "</button>" +
        '<span class="slot-meta">' + meta + "</span>" +
      "</div>" +
    "</div>"
  );
}

function renderPast(plan) {
  renderRisks([]);
  hintEl.hidden = true;
  resetOrderEl.hidden = true;
  overflowBox.hidden = true;

  const day = fromIsoDay(plan.date);
  document.getElementById("pl-date").textContent = day.toLocaleDateString("fr-FR", {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
  document.getElementById("pl-load-text").textContent =
    plan.done.length + " tâche(s) bouclée(s) · " + formatDuration(plan.done_min) + " estimées" +
    (plan.tracked_min ? " · " + formatDuration(plan.tracked_min) + " suivies" : "");
  document.getElementById("pl-agenda").hidden = true;
  const fill = document.getElementById("pl-load-fill");
  const load = loadPct(plan.planned_day_min, plan.day_capacity_min);
  fill.style.width = Math.min(load, 100) + "%";
  fill.classList.toggle("full", load >= 90);
  renderLoadValues(plan.planned_day_min, plan.day_capacity_min, plan.tracked_day_min);

  let html = '<h3 class="window-title">Bouclées ce jour-là</h3>';
  html += plan.done.length
    ? plan.done.map((task) => pastRow(task, true)).join("")
    : '<p class="empty">Rien de coché ce jour-là.</p>';

  if ((plan.events || []).length) {
    html += '<h3 class="window-title">Agenda</h3>';
    plan.events.forEach((item) => { html += eventRow(item); });
  }

  html += '<h3 class="window-title">Oublié de cocher ?</h3>' +
    '<p class="plan-hint">Coche ce qui a été fait ce jour-là : la tâche est close à cette date, ' +
    "pour un bilan juste.</p>";
  html += plan.open.length
    ? plan.open.map((task) => pastRow(task, false)).join("")
    : '<p class="empty">Aucune tâche ouverte à rattraper.</p>';

  bodyEl.innerHTML = html;
}

function renderTimer(session) {
  const banner = document.getElementById("timer-banner");
  if (!session) {
    banner.hidden = true;
    return;
  }
  banner.innerHTML =
    '<span class="timer-dot"></span>En cours : <strong>' + escapeHtml(session.task_title) +
    "</strong> — " + formatDuration(session.elapsed_min || 1) +
    ' <button type="button" class="btn-ghost" id="timer-stop">Arrêter</button>';
  banner.hidden = false;
}

/* Recharge le plan du jour affiche. `animate` rejoue l'entree en cascade
 * des lignes (chargement, changement de jour) ; sans lui, rendu immediat. */
async function reload(animate) {
  animateRows = animate === true;
  rowIndex = 0;
  // Changement de jour (animate) : la note du soir precedent suit le jour affiche.
  if (animateRows) loadMorningNote();
  // L'anneau du logo (shell.js) suit le jour affiche.
  document.dispatchEvent(new CustomEvent("nomaia:plan-day", { detail: { date: currentDate } }));
  try {
    // Le chronometre est lu avant le rendu : la ligne en cours porte l'etiquette « En cours ».
    const [plan, timer] = await Promise.all([
      callApi("/api/plan?date=" + currentDate),
      callApi("/api/tasks/timer"),
    ]);
    runningTaskId = timer.session ? timer.session.task_id : null;
    render(plan);
    poppedId = null;
    fillSettings(plan.settings);
    renderTimer(timer.session);
    renderPlannedKpi(plan);
    loadSide();
  } catch (error) {
    bodyEl.innerHTML = '<p class="empty">' + escapeHtml(error.message) + "</p>";
  }
}

/* ---------- Actions ---------- */

document.addEventListener("click", async (event) => {
  const trigger = event.target.closest("[data-action]");
  if (!trigger) return;

  const taskId = trigger.closest(".slot")?.dataset.id;
  if (!taskId) return;

  // Coche : rebond immediat, rejoue sur la ligne redessinee si elle reste.
  if (trigger.classList.contains("task-check") && trigger.checked) {
    trigger.classList.add("anim-pop");
    poppedId = taskId;
  }

  try {
    if (trigger.dataset.action === "detail") {
      await openDetail(taskId);
      return;
    } else if (trigger.dataset.action === "now") {
      await callApi("/api/plan/pin/" + taskId, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: currentDate, position: "now" }),
      });
      await reload();
      toast("Placée en tête de journée.");
    } else if (trigger.dataset.action === "unpin") {
      await callApi("/api/plan/pin/" + taskId, { method: "DELETE" });
      await reload();
      toast("Rendue au classement automatique.");
    } else if (trigger.dataset.action === "done-on") {
      await callApi("/api/tasks/" + taskId + "/done-on", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ day: currentDate }),
      });
      await reload();
      toast("Notée comme faite ce jour-là.");
    } else if (trigger.dataset.action === "undo-past") {
      await callApi("/api/tasks/" + taskId + "/toggle", { method: "POST" });
      await reload();
      toast("Tâche rouverte.");
    } else if (trigger.dataset.action === "done") {
      await callApi("/api/tasks/" + taskId + "/toggle", { method: "POST" });
      await reload();
      toast("Tâche terminée, le plan se réorganise.");
    } else if (trigger.dataset.action === "timer") {
      await callApi("/api/tasks/" + taskId + "/timer/start", { method: "POST" });
      document.dispatchEvent(new CustomEvent("nomaia:timer"));
      await reload();
      toast("Minuteur lancé.");
    } else if (trigger.dataset.action === "why") {
      const data = await callApi("/api/plan/why/" + taskId);
      const detail = data.detail;
      toast(
        "Score " + detail.total + " = priorité " + detail.priorite +
        " + urgence " + detail.urgence +
        (detail.tache_courte ? " + tâche courte " + detail.tache_courte : "") +
        (detail.a_verifier ? " " + detail.a_verifier + " à vérifier" : "")
      );
    }
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Ordre a la main : glisser-deposer ---------- */

let draggedId = null;

function taskSlots() {
  return Array.from(bodyEl.querySelectorAll(".slot[draggable='true']"));
}

function clearDropMarks() {
  bodyEl.querySelectorAll(".drop-before, .drop-after").forEach((node) => {
    node.classList.remove("drop-before", "drop-after");
  });
}

bodyEl.addEventListener("dragstart", (event) => {
  const slot = event.target.closest(".slot[draggable='true']");
  if (!slot) return;
  draggedId = slot.dataset.id;
  slot.classList.add("is-dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", draggedId);
});

bodyEl.addEventListener("dragend", () => {
  draggedId = null;
  clearDropMarks();
  bodyEl.querySelectorAll(".is-dragging").forEach((node) => node.classList.remove("is-dragging"));
});

bodyEl.addEventListener("dragover", (event) => {
  const target = event.target.closest(".slot[draggable='true']");
  if (!draggedId || !target || target.dataset.id === draggedId) return;
  event.preventDefault();
  const box = target.getBoundingClientRect();
  const after = event.clientY > box.top + box.height / 2;
  clearDropMarks();
  target.classList.add(after ? "drop-after" : "drop-before");
});

bodyEl.addEventListener("drop", async (event) => {
  const target = event.target.closest(".slot[draggable='true']");
  if (!draggedId || !target || target.dataset.id === draggedId) return;
  event.preventDefault();

  const after = target.classList.contains("drop-after");
  const ordre = taskSlots().map((node) => node.dataset.id).filter((id) => id !== draggedId);
  const index = ordre.indexOf(target.dataset.id) + (after ? 1 : 0);
  ordre.splice(index, 0, draggedId);
  clearDropMarks();

  try {
    await callApi("/api/plan/order", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: currentDate, task_ids: ordre.map(Number) }),
    });
    await reload();
    toast("Ordre de la journée enregistré.");
  } catch (error) {
    toast(error.message, true);
  }
});

resetOrderEl.addEventListener("click", async () => {
  try {
    await callApi("/api/plan/order?date=" + currentDate, { method: "DELETE" });
    await reload();
    toast("NOMAIA reprend la main sur l'ordre.");
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Detail d'une tache (static/js/task_detail.js) ---------- */

function openDetail(taskId) {
  return window.TaskDetail.open(taskId, { day: currentDate, onChange: reload });
}

/* ---------- Navigation entre les jours ---------- */

function shiftDate(days) {
  const day = new Date(currentDate + "T00:00:00");
  day.setDate(day.getDate() + days);
  // Date locale : toISOString (UTC) decalerait d'un jour en fuseau UTC+.
  currentDate = day.getFullYear() + "-" + String(day.getMonth() + 1).padStart(2, "0") + "-" + String(day.getDate()).padStart(2, "0");
  reload(true);
}

document.getElementById("timer-banner").addEventListener("click", async (event) => {
  if (!event.target.closest("#timer-stop")) return;
  try {
    const data = await callApi("/api/tasks/timer/stop", { method: "POST" });
    document.dispatchEvent(new CustomEvent("nomaia:timer"));
    await reload();
    toast(formatDuration(data.duration_min) + " enregistrées.");
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("pl-prev").addEventListener("click", () => shiftDate(-1));
document.getElementById("pl-next").addEventListener("click", () => shiftDate(1));
document.getElementById("pl-today").addEventListener("click", () => {
  currentDate = document.getElementById("header-date").dataset.today;
  reload(true);
});

/* ---------- Reglages d'horaires ---------- */

function fillSettings(settings) {
  document.getElementById("st-work-start").value = settings.work_start;
  document.getElementById("st-work-end").value = settings.work_end;
  document.getElementById("st-lunch-start").value = settings.lunch_start;
  document.getElementById("st-lunch-end").value = settings.lunch_end;
  document.getElementById("st-buffer").value = settings.plan_buffer_pct;
  document.getElementById("st-horizon").value = settings.plan_horizon_days;
}

doneToggleEl.addEventListener("click", () => {
  showDone = !showDone;
  saveShowDone(showDone);
  reload();
});

document.getElementById("pl-settings-open").addEventListener("click", () => settingsModal.showModal());
document.getElementById("st-cancel").addEventListener("click", () => settingsModal.close());

document.getElementById("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();

  const payload = {
    work_start: document.getElementById("st-work-start").value,
    work_end: document.getElementById("st-work-end").value,
    lunch_start: document.getElementById("st-lunch-start").value,
    lunch_end: document.getElementById("st-lunch-end").value,
    plan_buffer_pct: document.getElementById("st-buffer").value,
    plan_horizon_days: document.getElementById("st-horizon").value,
  };

  try {
    await callApi("/api/plan/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    settingsModal.close();
    await reload();
    toast("Horaires enregistrés.");
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Initialisation ---------- */

document.getElementById("header-date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

reload(true);

/* ---------- Indicateurs et colonne laterale (maquette) ---------- */

/* Defilement d'un chiffre jusqu'a sa valeur (700 ms), sauf animations reduites. */
function countTo(element, value, format) {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
    document.documentElement.dataset.motion === "reduced";
  const previous = Number(element.dataset.value || 0);
  element.dataset.value = value;
  if (reduced || previous === value) { element.textContent = format(value); return; }
  const start = performance.now();
  function step(now) {
    const t = Math.min(1, (now - start) / 700);
    element.textContent = format(Math.round(previous + (value - previous) * (1 - Math.pow(1 - t, 3))));
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

const asDuration = (minutes) => (minutes ? formatDuration(minutes) : "0 min");
const asCount = (count) => String(count);

/* Position du jour affiche : 'past', 'today' ou 'future'. */
function dayPosition() {
  const today = document.getElementById("header-date").dataset.today;
  return currentDate < today ? "past" : currentDate === today ? "today" : "future";
}

// Dernier plan affiche : le detail « Planifie » se lit dedans, sans requete.
let lastPlan = null;

function renderPlannedKpi(plan) {
  lastPlan = plan;
  const value = document.getElementById("kpi-planned");
  const hint = document.getElementById("kpi-planned-hint");
  document.getElementById("kpi-planned-label").textContent =
    dayPosition() === "today" ? "Planifié aujourd'hui" : "Planifié ce jour-là";

  // Jour passe : le planifie note ce jour-la, s'il l'a ete.
  if (plan.past) {
    if (plan.planned_day_min === null || plan.planned_day_min === undefined) {
      value.textContent = "—";
      value.dataset.value = 0;
      hint.textContent = "non noté ce jour-là";
    } else {
      countTo(value, plan.planned_day_min, asDuration);
      hint.textContent = (plan.done || []).length + " tâche(s) bouclée(s)";
    }
    return;
  }

  const slots = (plan.slots || []).length;
  const events = (plan.events || []).length;
  countTo(value, plan.planned_min || 0, asDuration);
  hint.textContent =
    plan.capacity_min ? slots + " créneau" + (slots > 1 ? "x" : "") + ", " + events + " rendez-vous" : "jour de repos";
}

function sideEmpty(text) {
  return '<p class="ps-empty">' + text + "</p>";
}

// Derniere sortie posee par bloc de la colonne : un rendu identique
// (rechargement a chaud, rafraichissement periodique) ne rejoue pas l'animation.
const sideHtml = new Map();

function setSideHtml(id, html) {
  if (sideHtml.get(id) === html) return;
  sideHtml.set(id, html);
  document.getElementById(id).innerHTML = html;
}

function renderSide(data) {
  const kpis = data.kpis;
  const tracked = document.getElementById("kpi-tracked");
  // Reponse d'un jour quitte entre-temps : les indicateurs affiches restent.
  if (data.day !== currentDate) return;
  const position = dayPosition();
  if (kpis.tracked_min === null) {
    tracked.textContent = "—";
    tracked.dataset.value = 0;
    document.getElementById("kpi-tracked-hint").textContent = "jour à venir";
  } else {
    countTo(tracked, kpis.tracked_min, asDuration);
    document.getElementById("kpi-tracked-hint").textContent = position === "past"
      ? "chronométré ce jour-là"
      : runningTaskId ? "chronomètre en cours" : "chronomètre arrêté";
    // Temps suivi rafraichi chaque minute : la barre temps reel suit.
    renderLoadValues(loadState.planned, loadState.capacity, kpis.tracked_min);
  }
  countTo(document.getElementById("kpi-late"), kpis.late_count, asCount);
  document.getElementById("kpi-late-hint").textContent = position === "past"
    ? "en retard à la fin de ce jour-là"
    : position === "future" ? "en retard ce jour-là si rien ne bouge" : "tâches à échéance dépassée";
  countTo(document.getElementById("kpi-client"), kpis.client.count, asCount);
  document.getElementById("kpi-client-hint").textContent = kpis.client.oldest_days !== null
    ? "plus ancienne : " + kpis.client.oldest_days + " jour" + (kpis.client.oldest_days > 1 ? "s" : "") : "rien en attente";

  setSideHtml("ps-agenda", data.agenda.length
    ? '<ul class="ps-list">' + data.agenda.map((ev) =>
        '<li><span class="ps-time">' + escapeHtml(ev.starts_at.slice(11, 16)) + "</span><span>" +
          '<span class="ps-title">' + escapeHtml(ev.title) + "</span>" +
          '<span class="ps-sub">' + escapeHtml([ev.project_name, ev.location].filter(Boolean).join(" · ") ||
            ev.starts_at.slice(11, 16) + " – " + ev.ends_at.slice(11, 16)) + "</span></span></li>").join("") + "</ul>"
    : sideEmpty("Aucun rendez-vous ce jour-là."));

  setSideHtml("ps-signals", data.signals.length
    ? data.signals.map((sig) =>
        '<a class="ps-signal is-' + escapeHtml(sig.level) + '" href="/projets/' + sig.project_id + '">' +
          '<span class="ps-title">' + escapeHtml(sig.name) + "</span>" +
          '<span class="ps-sub">' + escapeHtml(sig.label) + "</span>" +
          '<span class="ps-bar"><span style="--pct:' + Math.min(sig.progress_pct, 100) / 100 + '"></span></span>' +
          '<span class="ps-sub">' + sig.progress_pct + " % fait</span></a>").join("")
    : sideEmpty("Aucun projet à surveiller."));

  const inbox = data.inbox;
  document.getElementById("ps-inbox-link").textContent = inbox.pending ? "Trier les " + inbox.pending : "Ouvrir";
  setSideHtml("ps-inbox", inbox.last
    ? '<p class="ps-capture">' + escapeHtml(inbox.last.text || "(pièce jointe)") + "</p>" +
      '<p class="ps-sub">' + (inbox.last.source === "telegram" ? "Téléphone" : "Saisie") + " · " +
        escapeHtml(String(inbox.last.created_at).slice(0, 16)) + "</p>"
    : sideEmpty("Rien de déposé pour l'instant."));
}

async function loadSide() {
  clearTimeout(sideTimer);
  try {
    renderSide(await callApi("/api/plan/side?date=" + currentDate));
  } catch (error) {
    // Colonne d'appoint : le plan reste utilisable sans elle.
  }
  // Temps suivi et compteurs a jour pendant que la page reste ouverte.
  sideTimer = setTimeout(loadSide, 60000);
}

/* ---------- Detail d'un indicateur (clic sur une tuile) ---------- */

const kpiDialog = document.getElementById("kpi-detail");
const kpiBody = document.getElementById("kd-body");
const KPI_TITLES = { planned: "Planifié", tracked: "Temps suivi", late: "En retard", client: "Attente client" };

/* Une ligne du detail : titre (ouvre la fiche si c'est une tache), precisions, valeur. */
function kpiRow(taskId, title, meta, value, extraClass) {
  const name = taskId
    ? '<button type="button" class="kd-title as-link" data-open-task="' + taskId + '">' + escapeHtml(title) + "</button>"
    : '<span class="kd-title">' + escapeHtml(title) + "</span>";
  return (
    '<li class="kd-row' + (extraClass ? " " + extraClass : "") + '">' +
      '<span class="kd-main">' + name +
        (meta ? '<span class="kd-meta">' + escapeHtml(meta) + "</span>" : "") +
      "</span>" +
      '<span class="kd-value">' + escapeHtml(value) + "</span>" +
    "</li>"
  );
}

function kpiList(rows, emptyText) {
  return rows.length ? '<ul class="kd-list">' + rows.join("") + "</ul>" : '<p class="kd-empty">' + emptyText + "</p>";
}

function kpiTotal(label, value) {
  return '<p class="kd-total"><span>' + label + "</span><strong>" + escapeHtml(value) + "</strong></p>";
}

const plural = (count, word) => count + " " + word + (count > 1 ? "s" : "");

/* Planifie : creneaux du plan (aujourd'hui, a venir) ou taches bouclees (jour passe). */
function plannedDetail(plan) {
  if (plan.past) {
    const rows = (plan.done || []).map((task) =>
      kpiRow(task.id, task.title, task.project_name || "",
        formatDuration(task.estimate_min) + (task.tracked_min ? " · " + formatDuration(task.tracked_min) + " suivies" : "")));
    return (
      (plan.planned_day_min === null || plan.planned_day_min === undefined
        ? '<p class="kd-note">Le planifié n\'a pas été noté ce jour-là (plan du jour non ouvert).</p>'
        : kpiTotal("Planifié ce jour-là", asDuration(plan.planned_day_min))) +
      '<h3 class="kd-section">Bouclées ce jour-là</h3>' +
      kpiList(rows, "Aucune tâche cochée ce jour-là.") +
      '<p class="kd-note">Les créneaux d\'un jour passé ne sont pas conservés : seul le total planifié est noté.</p>'
    );
  }
  if (plan.day_off) return '<p class="kd-empty">Jour de repos : rien n\'est planifié.</p>';
  const rows = (plan.slots || []).map((slot) =>
    kpiRow(slot.task.id, slot.task.title, slot.start + " – " + slot.end + (slot.task.project_name ? " · " + slot.task.project_name : ""),
      formatDuration(slot.duration_min)));
  const overflow = (plan.overflow || []).length;
  return (
    kpiList(rows, "Aucun créneau planifié.") +
    kpiTotal("Total planifié", asDuration(plan.planned_min) + " sur " + asDuration(plan.capacity_min)) +
    (overflow ? '<p class="kd-note">' + plural(overflow, "tâche") + " ne rentre" + (overflow > 1 ? "nt" : "") +
      " pas dans la journée.</p>" : "")
  );
}

function trackedDetail(data) {
  const rows = data.items.map((item) =>
    kpiRow(item.task_id, item.title,
      item.start + " – " + item.end + (item.project_name ? " · " + item.project_name : ""),
      formatDuration(item.minutes) || "< 1 min", item.running ? "is-running" : ""));
  return kpiList(rows, "Aucune session chronométrée ce jour-là.") +
    (rows.length ? kpiTotal("Total suivi", asDuration(data.total_min)) : "");
}

function lateDetail(data) {
  const rows = data.items.map((item) =>
    kpiRow(item.task_id, item.title,
      "échéance " + formatDeadline(item.deadline) + (item.project_name ? " · " + item.project_name : ""),
      plural(item.days_late, "jour") + " de retard"));
  return kpiList(rows, "Aucune tâche en retard ce jour-là.");
}

function clientDetail(data) {
  const rows = data.items.map((item) =>
    kpiRow(item.task_id, item.title,
      (item.kind === "waiting" ? "Attente" : "Tâche côté client") +
        (item.person_name ? " · " + item.person_name : "") + (item.project_name ? " · " + item.project_name : ""),
      "depuis " + plural(item.days, "jour")));
  return kpiList(rows, "Rien n'attend le client ce jour-là.");
}

async function openKpi(kind) {
  const day = fromIsoDay(currentDate).toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
  document.getElementById("kd-title").textContent = KPI_TITLES[kind];
  document.getElementById("kd-day").textContent = day.charAt(0).toUpperCase() + day.slice(1);
  if (kind === "planned") {
    kpiBody.innerHTML = lastPlan ? plannedDetail(lastPlan) : "";
    if (!kpiDialog.open) kpiDialog.showModal();
    return;
  }
  kpiBody.innerHTML = '<div class="skel skel-line"></div><div class="skel skel-line"></div>';
  if (!kpiDialog.open) kpiDialog.showModal();
  try {
    const data = await callApi("/api/plan/kpi/" + kind + "?date=" + currentDate);
    kpiBody.innerHTML = kind === "tracked" ? trackedDetail(data) : kind === "late" ? lateDetail(data) : clientDetail(data);
  } catch (error) {
    kpiBody.innerHTML = '<p class="kd-empty">' + escapeHtml(error.message) + "</p>";
  }
}

document.getElementById("pl-kpis").addEventListener("click", (event) => {
  const tile = event.target.closest("[data-kpi]");
  if (tile) openKpi(tile.dataset.kpi);
});

document.getElementById("pl-kpis").addEventListener("keydown", (event) => {
  const tile = event.target.closest("[data-kpi]");
  if (!tile || (event.key !== "Enter" && event.key !== " ")) return;
  event.preventDefault();
  openKpi(tile.dataset.kpi);
});

document.getElementById("kd-close").addEventListener("click", () => kpiDialog.close());

// Clic sur le fond : ferme. Clic sur une tache : ouvre sa fiche.
kpiDialog.addEventListener("click", (event) => {
  if (event.target === kpiDialog) {
    kpiDialog.close();
    return;
  }
  const link = event.target.closest("[data-open-task]");
  if (!link) return;
  kpiDialog.close();
  openDetail(Number(link.dataset.openTask)).catch((error) => toast(error.message, true));
});

/* ---------- Charge : planifie et temps reel ---------- */

// Derniere charge affichee : le rafraichissement du temps suivi la reutilise.
const loadState = { planned: null, capacity: 0 };

/* Pourcentage d'une duree sur la capacite du jour (0 sans capacite). */
function loadPct(minutes, capacity) {
  return capacity ? Math.round((minutes || 0) / capacity * 100) : 0;
}

/* Valeurs des barres de charge. `planned` null : planifie non note ce jour-la ;
 * `tracked` absent : pas de temps reel (jour futur), la ligne est masquee. */
function renderLoadValues(planned, capacity, tracked) {
  loadState.planned = planned;
  loadState.capacity = capacity || 0;

  document.getElementById("pl-load-value").textContent = planned === null || planned === undefined
    ? "non noté"
    : asDuration(planned) + (capacity ? " · " + loadPct(planned, capacity) + " %" : "");

  const row = document.getElementById("pl-real-row");
  if (tracked === undefined || tracked === null) {
    row.hidden = true;
    return;
  }
  row.hidden = false;
  const pct = loadPct(tracked, capacity);
  const fill = document.getElementById("pl-real-fill");
  fill.style.width = Math.min(pct, 100) + "%";
  fill.classList.toggle("over", Boolean(planned) && tracked > planned);
  document.getElementById("pl-real-value").textContent =
    asDuration(tracked) + (capacity ? " · " + pct + " %" : "");
}

/* ---------- Cloture de journee et note du matin (lot 8) ---------- */

const dayDialog = document.getElementById("day-close");

function dayLabel(iso) {
  const label = new Date(iso + "T00:00:00").toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function renderDayRecap(data) {
  const done = data.done.length
    ? "<ul>" + data.done.map((task) => "<li>" + escapeHtml(task.title) +
        (task.project_name ? ' <span class="dc-muted">· ' + escapeHtml(task.project_name) + "</span>" : "") + "</li>").join("") + "</ul>"
    : '<p class="dc-muted">Aucune tâche terminée.</p>';
  const tracked = data.tracked_by_project.map((row) => escapeHtml(row.project) + " " + formatDuration(row.minutes)).join(" · ");
  const work = data.journal.filter((entry) => entry.kind === "work" || entry.kind === "progress").slice(0, 6);
  return '<p class="dc-title">Terminé (' + data.done.length + ")</p>" + done +
    '<p class="dc-title">Temps suivi : ' + (data.tracked_min ? formatDuration(data.tracked_min) : "aucun") + "</p>" +
    (tracked ? '<p class="dc-muted">' + tracked + "</p>" : "") +
    (work.length ? '<p class="dc-title">Journal</p><ul>' + work.map((entry) =>
      "<li>" + escapeHtml(entry.content.slice(0, 160)) + "</li>").join("") + "</ul>" : "");
}

async function openDayClose() {
  document.getElementById("dc-day").textContent = dayLabel(currentDate);
  document.getElementById("dc-error").hidden = true;
  dayDialog.showModal();
  try {
    const data = await callApi("/api/day/recap?date=" + currentDate);
    document.getElementById("dc-recap").innerHTML = renderDayRecap(data);
    document.getElementById("dc-tomorrow").value = data.tomorrow || "";
    document.getElementById("dc-tomorrow").focus();
  } catch (error) {
    document.getElementById("dc-recap").innerHTML = '<p class="error-box">' + escapeHtml(error.message) + "</p>";
  }
}

document.getElementById("pl-close-day").addEventListener("click", openDayClose);
document.getElementById("dc-cancel").addEventListener("click", () => dayDialog.close());
document.getElementById("dc-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await callApi("/api/day/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: currentDate, tomorrow: document.getElementById("dc-tomorrow").value }),
    });
    dayDialog.close();
    toast("Journée clôturée. La note vous attendra demain matin.");
  } catch (error) {
    const box = document.getElementById("dc-error");
    box.textContent = error.message;
    box.hidden = false;
  }
});

async function loadMorningNote() {
  const box = document.getElementById("pl-morning");
  try {
    const { note } = await callApi("/api/day/morning?date=" + currentDate);
    box.hidden = !note;
    if (!note) return;
    document.getElementById("pl-morning-label").textContent = "Noté le " + dayLabel(note.day).toLowerCase() + " pour la suite";
    document.getElementById("pl-morning-text").textContent = note.text;
  } catch (error) {
    box.hidden = true;
  }
}

// Palette Ctrl+K « Cloturer ma journee » : /plan?cloturer=1.
if (new URLSearchParams(window.location.search).has("cloturer")) {
  history.replaceState(null, "", window.location.pathname);
  openDayClose();
}

// Lien profond /plan?tache=<id> (palette, notes, comptes rendus) : ouvre la fiche de la tache.
(function openRequestedTask() {
  const params = new URLSearchParams(window.location.search);
  const taskId = Number(params.get("tache"));
  if (!taskId) return;
  params.delete("tache");
  const rest = params.toString();
  history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : ""));
  openDetail(taskId).catch((error) => toast(error.message, true));
})();

// Rechargement a chaud : plan, colonne laterale (via reload) et note du matin,
// sans rejouer l'entree ; le jour affiche est garde.
J.live.onChange(() => Promise.all([reload(), loadMorningNote()]));

// Chronometre arrete depuis l'en-tete (coquille) : le bandeau du plan suit.
document.addEventListener("nomaia:timer", () => {
  callApi("/api/tasks/timer").then((data) => renderTimer(data.session)).catch(() => {});
});
