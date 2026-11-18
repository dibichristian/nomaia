/* NOMAIA — agenda : semaine de sept colonnes, saisie et repetitions.
 *
 * Les titres, lieux et notes viennent de l'API : ils peuvent contenir du HTML
 * saisi a la main ou produit par un modele de langage. Tout est donc construit
 * en DOM, jamais avec innerHTML.
 */

const { toast, callApi } = window.J;

const headEl = document.getElementById("header-date");
const bodyEl = document.getElementById("agenda-body");
const rangeEl = document.getElementById("ag-range");
const errorEl = document.getElementById("ag-error");

const modalEl = document.getElementById("event-modal");
const formEl = document.getElementById("event-form");
const modalTitleEl = document.getElementById("ev-modal-title");
const seriesHintEl = document.getElementById("ev-series-hint");
const formErrorEl = document.getElementById("ev-error");
const untilBoxEl = document.getElementById("ev-until-box");
const deleteBoxEl = document.getElementById("ev-delete-box");
const deleteOneEl = document.getElementById("ev-delete-one");
const deleteAllEl = document.getElementById("ev-delete-all");
const deleteEl = document.getElementById("ev-delete");
const saveEl = document.getElementById("ev-save");

const detailEl = document.getElementById("event-detail");
const detailTitleEl = document.getElementById("evd-title");
const detailWhenEl = document.getElementById("evd-when");
const detailFactsEl = document.getElementById("evd-facts");
const detailNotesEl = document.getElementById("evd-notes");
const detailReadOnlyEl = document.getElementById("evd-readonly");
const detailEditEl = document.getElementById("evd-edit");

const dayOffHeadingEl = document.getElementById("do-heading");
const dayOffErrorEl = document.getElementById("do-error");
const dayOffListEl = document.getElementById("do-list");
const dayOffFormEl = document.getElementById("do-form");
const dayOffSaveEl = document.getElementById("do-save");
const holidaysEl = document.getElementById("do-holidays");

const dayOffFields = {
  title: document.getElementById("do-title"),
  start: document.getElementById("do-start"),
  end: document.getElementById("do-end"),
  kind: document.getElementById("do-kind"),
};

const fields = {
  title: document.getElementById("ev-title"),
  date: document.getElementById("ev-date"),
  kind: document.getElementById("ev-kind"),
  start: document.getElementById("ev-start"),
  end: document.getElementById("ev-end"),
  location: document.getElementById("ev-location"),
  project: document.getElementById("ev-project"),
  repeat: document.getElementById("ev-repeat"),
  until: document.getElementById("ev-until"),
  notes: document.getElementById("ev-notes"),
};

const DAY_LABELS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];
const KIND_LABELS = { meeting: "Réunion", focus: "Créneau bloqué", absence: "Absence", personal: "Personnel" };
const KIND_ORDER = ["meeting", "focus", "absence", "personal"];
const DAY_OFF_LABELS = { leave: "Congé", holiday: "Férié" };

// Plage horaire affichee par defaut : elle s'etire si un evenement deborde,
// jamais l'inverse, pour qu'aucun evenement ne disparaisse de la grille.
const DEFAULT_FIRST_MIN = 7 * 60;
const DEFAULT_LAST_MIN = 21 * 60;
const PX_PER_MIN = 0.85;
const MIN_BLOCK_PX = 24;
// En dessous de cette hauteur, heure et titre ne tiennent pas l'un sous
// l'autre : le bloc passe sur une seule ligne, titre en tete.
const SHORT_BLOCK_PX = 44;

const HEX_COLOR = /^#[0-9a-fA-F]{3,8}$/;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

const narrowQuery = window.matchMedia("(max-width: 860px)");

const projectColors = new Map();

let weekStart = "";
let events = [];
// Jours non travailles de la semaine affichee : date ISO -> motif.
let offDays = new Map();
// Dates de la semaine couvertes par un conge ou un ferie saisi.
let leaveDays = new Set();
// Conges en attente de validation (Odoo) : date ISO -> intitule. Affiches,
// ils ne rendent pas le jour chome.
let pendingDays = new Map();
// Annee dont la liste des jours de repos est affichee.
let dayOffYear = 0;
// Apparition en cascade des lignes (vue liste) : coupee pendant un
// rechargement a chaud, pour qu'une semaine inchangee ne clignote pas.
let animateRows = true;
// Evenement en cours de modification : null en creation.
let editing = null;
// Evenement affiche dans la fiche de detail, et le jour clique.
let detailed = null;

/* ---------- Dates ---------- */

function pad(value) {
  return String(value).padStart(2, "0");
}

function toIso(date) {
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
}

function fromIso(iso) {
  const parts = String(iso).split("-");
  return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
}

function addDays(iso, count) {
  const date = fromIso(iso);
  date.setDate(date.getDate() + count);
  return toIso(date);
}

function mondayOf(iso) {
  const date = fromIso(iso);
  // getDay() renvoie 0 pour dimanche : on ramene la semaine au lundi.
  const shift = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - shift);
  return toIso(date);
}

function todayIso() {
  const attribute = headEl.dataset.today;
  return ISO_DAY.test(attribute || "") ? attribute : toIso(new Date());
}

function clock(minutes) {
  const total = Math.max(0, Math.min(minutes, 1440));
  return pad(Math.floor(total / 60)) + ":" + pad(total % 60);
}

function parseMoment(value) {
  if (!value) return null;
  const text = String(value).replace("T", " ").trim();
  const parts = text.split(" ");
  const day = parts[0];
  if (!ISO_DAY.test(day)) return null;
  const clockParts = (parts[1] || "00:00").split(":");
  const minutes = Number(clockParts[0]) * 60 + Number(clockParts[1] || 0);
  if (Number.isNaN(minutes)) return null;
  return { day: day, minutes: Math.max(0, Math.min(minutes, 1440)) };
}

function weekDays() {
  const days = [];
  for (let index = 0; index < 7; index += 1) days.push(addDays(weekStart, index));
  return days;
}

function weekLabel(days) {
  const first = fromIso(days[0]);
  const last = fromIso(days[6]);
  const sameMonth = first.getMonth() === last.getMonth();
  const head = first.toLocaleDateString("fr-FR", sameMonth ? { day: "numeric" } : { day: "numeric", month: "long" });
  const tail = last.toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
  return head + " – " + tail;
}

/* ---------- Mise en forme des evenements ---------- */

function kindOf(event) {
  return KIND_ORDER.indexOf(event.kind) === -1 ? "meeting" : event.kind;
}

function colorOf(event) {
  const color = projectColors.get(Number(event.project_id));
  return HEX_COLOR.test(color || "") ? color : null;
}

function applyColor(node, event) {
  const color = colorOf(event);
  if (color) node.style.setProperty("--ev-color", color);
}

/* Decoupe un evenement en un morceau par journee traversee, borne a la semaine. */
function segmentsOf(event, days) {
  const start = parseMoment(event.starts_at);
  if (!start) return [];

  let end = parseMoment(event.ends_at);
  if (!end || end.day < start.day || (end.day === start.day && end.minutes <= start.minutes)) {
    end = { day: start.day, minutes: Math.min(start.minutes + 60, 1440) };
  }

  const segments = [];
  let day = start.day;
  // Garde-fou : une donnee aberrante ne doit pas boucler sans fin.
  for (let guard = 0; guard < 40; guard += 1) {
    const from = day === start.day ? start.minutes : 0;
    const to = day === end.day ? end.minutes : 1440;
    if (days.indexOf(day) !== -1 && to > from) {
      segments.push({ event: event, day: day, from: from, to: to, lane: 0, lanes: 1 });
    }
    if (day === end.day) break;
    day = addDays(day, 1);
  }
  return segments;
}

/* Repartit les morceaux d'une journee en colonnes pour qu'aucun n'en cache un autre. */
function layoutDay(segments) {
  const sorted = segments.slice().sort((a, b) => a.from - b.from || a.to - b.to);

  let group = [];
  let groupEnd = -1;
  const groups = [];

  sorted.forEach((segment) => {
    if (group.length && segment.from >= groupEnd) {
      groups.push(group);
      group = [];
      groupEnd = -1;
    }
    group.push(segment);
    groupEnd = Math.max(groupEnd, segment.to);
  });
  if (group.length) groups.push(group);

  groups.forEach((members) => {
    const laneEnds = [];
    members.forEach((segment) => {
      let lane = 0;
      while (lane < laneEnds.length && laneEnds[lane] > segment.from) lane += 1;
      laneEnds[lane] = segment.to;
      segment.lane = lane;
    });
    members.forEach((segment) => { segment.lanes = laneEnds.length; });
  });

  return sorted;
}

function boundsOf(allSegments) {
  let first = DEFAULT_FIRST_MIN;
  let last = DEFAULT_LAST_MIN;
  allSegments.forEach((segment) => {
    first = Math.min(first, Math.floor(segment.from / 60) * 60);
    last = Math.max(last, Math.ceil(segment.to / 60) * 60);
  });
  return { first: first, last: Math.max(last, first + 60) };
}

/* ---------- Construction du DOM ---------- */

function metaLine(event) {
  const bits = [KIND_LABELS[kindOf(event)]];
  if (event.location) bits.push(String(event.location));
  if (event.project_name) bits.push(String(event.project_name));
  if (event.is_occurrence || (event.repeat && event.repeat !== "none")) bits.push("série");
  return bits.join(" · ");
}

/* Evenements importes d'un agenda externe : lecture seule, modifies a la source. */
function sourceLabel(event) {
  return event.source_name ? String(event.source_name) : "Agenda externe";
}

function sourceBadge(event, className) {
  const badge = document.createElement("span");
  badge.className = "ag-source " + className;
  badge.textContent = sourceLabel(event);
  badge.title = "Importé de " + sourceLabel(event) + " (lecture seule)";
  return badge;
}

function showReadOnly(event) {
  toast("Importé de " + sourceLabel(event) + " : modifie-le dans " + sourceLabel(event) + ".");
}

/* Texte de l'info-bulle : tout ce qu'un bloc trop petit ne peut pas montrer. */
function tooltipOf(event, segment) {
  const lines = [
    clock(segment.from) + " – " + clock(segment.to) + "  " + (event.title ? String(event.title) : "(sans titre)"),
  ];
  const meta = metaLine(event);
  if (meta) lines.push(meta);
  if (event.notes) lines.push(String(event.notes).slice(0, 200));
  return lines.join("\n");
}

function eventButton(segment, first) {
  const event = segment.event;
  const node = document.createElement("button");
  node.type = "button";
  node.className = "ag-event ev-" + kindOf(event);
  if (event.read_only) node.classList.add("is-readonly");
  applyColor(node, event);

  const height = Math.max(Math.round((segment.to - segment.from) * PX_PER_MIN), MIN_BLOCK_PX);
  node.style.top = Math.round((segment.from - first) * PX_PER_MIN) + "px";
  node.style.height = height + "px";
  if (height < SHORT_BLOCK_PX) node.classList.add("is-short");
  node.title = tooltipOf(event, segment);
  const width = 100 / segment.lanes;
  node.style.left = segment.lane * width + "%";
  node.style.width = width + "%";

  const time = document.createElement("span");
  time.className = "ag-event-time";
  time.textContent = clock(segment.from) + " – " + clock(segment.to);
  node.appendChild(time);

  const title = document.createElement("span");
  title.className = "ag-event-title";
  title.textContent = event.title ? String(event.title) : "(sans titre)";
  node.appendChild(title);

  const details = [];
  if (event.location) details.push(String(event.location));
  if (event.project_name) details.push(String(event.project_name));
  if (details.length) {
    const meta = document.createElement("span");
    meta.className = "ag-event-meta";
    meta.textContent = details.join(" · ");
    node.appendChild(meta);
  }

  if (event.is_occurrence || (event.repeat && event.repeat !== "none")) {
    const mark = document.createElement("span");
    mark.className = "ag-event-repeat";
    mark.textContent = "↻";
    node.appendChild(mark);
  }

  if (event.read_only) node.appendChild(sourceBadge(event, "ag-event-source"));

  node.addEventListener("click", () => {
    // Un glisser vient de se terminer sur ce bloc : ce n'est pas un clic.
    if (node.dataset.dragged) { delete node.dataset.dragged; return; }
    openDetail(event, segment);
  });
  if (isMovable(event)) enableDrag(node, event, segment);
  return node;
}

/* ---------- Glisser-deposer dans la grille (lot 8) ----------
 * Un evenement simple (ni importe, ni repete, sur un seul jour) se deplace a
 * la souris : vers un autre jour et/ou une autre heure, par pas de 15 min,
 * duree conservee. Les autres restent a modifier par leur fiche. */

const SNAP_MIN = 15;
const DRAG_THRESHOLD_PX = 5;

function isMovable(event) {
  if (event.read_only || event.is_occurrence || (event.repeat && event.repeat !== "none")) return false;
  return String(event.starts_at).slice(0, 10) === String(event.ends_at).slice(0, 10);
}

function minutesOf(stamp) {
  const time = String(stamp).slice(11, 16).split(":");
  return Number(time[0]) * 60 + Number(time[1]);
}

function stampOf(day, minutes) {
  return day + " " + String(Math.floor(minutes / 60)).padStart(2, "0") + ":" + String(minutes % 60).padStart(2, "0");
}

function enableDrag(node, event, segment) {
  node.classList.add("is-movable");
  node.addEventListener("pointerdown", (down) => {
    if (down.button !== 0) return;
    const startX = down.clientX;
    const startY = down.clientY;
    const fromColumn = node.closest(".ag-col");
    let target = fromColumn;
    let delta = 0;
    let dragging = false;

    function move(pointer) {
      const dx = pointer.clientX - startX;
      const dy = pointer.clientY - startY;
      if (!dragging && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
      if (!dragging) {
        dragging = true;
        node.classList.add("is-dragging");
      }
      delta = Math.round(dy / PX_PER_MIN / SNAP_MIN) * SNAP_MIN;
      const under = document.elementsFromPoint(pointer.clientX, pointer.clientY).find((el) => el.classList?.contains("ag-col"));
      if (under) target = under;
      const shiftX = target.getBoundingClientRect().left - fromColumn.getBoundingClientRect().left;
      node.style.transform = "translate(" + shiftX + "px, " + Math.round(delta * PX_PER_MIN) + "px)";
    }

    async function up() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      if (!dragging) return;
      node.dataset.dragged = "1";
      node.classList.remove("is-dragging");
      const day = target.dataset.day;
      const start = minutesOf(event.starts_at) + delta;
      const duration = minutesOf(event.ends_at) - minutesOf(event.starts_at);
      if ((delta === 0 && day === String(event.starts_at).slice(0, 10)) || start < 0 || start + duration > 24 * 60) {
        node.style.transform = "";
        return;
      }
      try {
        await callApi("/api/events/" + event.id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ starts_at: stampOf(day, start), ends_at: stampOf(day, start + duration) }),
        });
        toast("« " + event.title + " » déplacé au " + fromIso(day).toLocaleDateString("fr-FR",
          { weekday: "long", day: "numeric" }) + " à " + clock(start) + ".");
        await load();
      } catch (error) {
        node.style.transform = "";
        toast(error.message, true);
      }
    }

    // Ecoute sur la fenetre : le curseur quitte le bloc des le premier deplacement.
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  });
}

function dayHead(day) {
  const node = document.createElement("div");
  node.className = "ag-dayhead";
  if (day === todayIso()) node.classList.add("is-today");

  const name = document.createElement("span");
  name.className = "ag-dayname";
  name.textContent = DAY_LABELS[(fromIso(day).getDay() + 6) % 7];
  node.appendChild(name);

  const number = document.createElement("span");
  number.className = "ag-daynum";
  number.textContent = fromIso(day).toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
  node.appendChild(number);

  const reason = offDays.get(day);
  if (reason !== undefined) {
    node.classList.add("is-off");
    node.appendChild(offBadge(day, reason));
  } else if (pendingDays.has(day)) {
    node.appendChild(pendingBadge(day));
  }

  return node;
}

/* Etiquette d'un conge en attente : le jour reste travaille. */
function pendingBadge(day) {
  const badge = document.createElement("span");
  badge.className = "ag-off-reason is-pending";
  const label = (pendingDays.get(day) || "Congé") + " · en attente";
  badge.textContent = label;
  badge.title = label + " : non validé, le plan n'en tient pas compte.";
  return badge;
}

/* Etiquette d'un jour non travaille : motif renvoye par le calendrier. */
function offBadge(day, reason) {
  const badge = document.createElement("span");
  badge.className = "ag-off-reason";
  if (leaveDays.has(day)) badge.classList.add("is-leave");
  badge.textContent = reason || "Jour de repos";
  badge.title = reason || "Jour de repos";
  return badge;
}

function markOffColumn(node, day) {
  if (!offDays.has(day)) {
    if (pendingDays.has(day)) node.classList.add("is-pending");
    return;
  }
  node.classList.add("is-off");
  if (leaveDays.has(day)) node.classList.add("is-leave");
}

function buildGrid(days, byDay, bounds) {
  const height = Math.round((bounds.last - bounds.first) * PX_PER_MIN);

  const scroll = document.createElement("div");
  scroll.className = "agenda-scroll";

  const grid = document.createElement("div");
  grid.className = "agenda-grid";
  scroll.appendChild(grid);

  const corner = document.createElement("div");
  corner.className = "ag-corner";
  grid.appendChild(corner);

  days.forEach((day) => grid.appendChild(dayHead(day)));

  const gutter = document.createElement("div");
  gutter.className = "ag-gutter";
  gutter.style.height = height + "px";
  for (let minute = bounds.first; minute <= bounds.last; minute += 60) {
    const label = document.createElement("span");
    label.className = "ag-hour";
    label.style.top = Math.round((minute - bounds.first) * PX_PER_MIN) + "px";
    label.textContent = clock(minute);
    gutter.appendChild(label);
  }
  grid.appendChild(gutter);

  days.forEach((day) => {
    const column = document.createElement("div");
    column.className = "ag-col";
    column.dataset.day = day;
    if (day === todayIso()) column.classList.add("is-today");
    markOffColumn(column, day);
    column.style.height = height + "px";
    // Les traits d'heure sont peints en fond : un element par heure serait
    // du DOM inutile sur une plage large.
    column.style.backgroundSize = "100% " + Math.round(60 * PX_PER_MIN) + "px";
    layoutDay(byDay.get(day) || []).forEach((segment) => {
      column.appendChild(eventButton(segment, bounds.first));
    });
    grid.appendChild(column);
  });

  return scroll;
}

function buildList(days, byDay) {
  const list = document.createElement("div");
  list.className = "agenda-list";
  // Rang d'apparition en cascade des lignes de la semaine (plafonne a 12).
  let rank = 0;

  days.forEach((day) => {
    const block = document.createElement("section");
    block.className = "ag-day";
    if (day === todayIso()) block.classList.add("is-today");

    const head = document.createElement("h3");
    head.className = "ag-day-title";
    head.textContent = fromIso(day).toLocaleDateString("fr-FR", {
      weekday: "long",
      day: "numeric",
      month: "long",
    });
    if (offDays.has(day)) {
      markOffColumn(block, day);
      head.appendChild(offBadge(day, offDays.get(day)));
    } else if (pendingDays.has(day)) {
      markOffColumn(block, day);
      head.appendChild(pendingBadge(day));
    }
    block.appendChild(head);

    const segments = layoutDay(byDay.get(day) || []);
    if (!segments.length) {
      const nothing = document.createElement("p");
      nothing.className = "ag-day-empty";
      nothing.textContent = "Rien de prévu.";
      block.appendChild(nothing);
    } else {
      segments.forEach((segment) => {
        const event = segment.event;
        const row = document.createElement("button");
        row.type = "button";
        row.className = "ag-row" + (animateRows ? " anim-in" : "") + " ev-" + kindOf(event);
        if (event.read_only) row.classList.add("is-readonly");
        row.style.setProperty("--i", String(Math.min(rank, 12)));
        rank += 1;
        applyColor(row, event);

        const time = document.createElement("span");
        time.className = "ag-row-time";
        time.textContent = clock(segment.from) + " – " + clock(segment.to);
        row.appendChild(time);

        const box = document.createElement("span");
        box.className = "ag-row-body";

        const title = document.createElement("span");
        title.className = "ag-row-title";
        title.textContent = event.title ? String(event.title) : "(sans titre)";
        box.appendChild(title);

        const meta = document.createElement("span");
        meta.className = "ag-row-meta";
        meta.textContent = metaLine(event);
        box.appendChild(meta);

        if (event.read_only) box.appendChild(sourceBadge(event, "ag-row-source"));

        row.appendChild(box);
        row.addEventListener("click", () => openDetail(event, segment));
        block.appendChild(row);
      });
    }

    list.appendChild(block);
  });

  return list;
}

function buildEmpty() {
  const box = document.createElement("div");
  box.className = "agenda-empty";

  const title = document.createElement("p");
  title.className = "agenda-empty-title";
  title.textContent = "Semaine vide.";
  box.appendChild(title);

  const hint = document.createElement("p");
  hint.className = "agenda-empty-hint";
  hint.textContent = "Aucun rendez-vous, aucun créneau bloqué. Pose le premier.";
  box.appendChild(hint);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn-primary";
  button.textContent = "Nouvel événement";
  button.addEventListener("click", () => openCreate());
  box.appendChild(button);

  return box;
}

function render() {
  const days = weekDays();
  rangeEl.textContent = weekLabel(days);

  const byDay = new Map();
  days.forEach((day) => byDay.set(day, []));

  const all = [];
  events.forEach((event) => {
    segmentsOf(event, days).forEach((segment) => {
      byDay.get(segment.day).push(segment);
      all.push(segment);
    });
  });

  bodyEl.replaceChildren();
  // Un conge ou un ferie dans la semaine merite la grille meme sans evenement ;
  // les week-ends seuls, eux, ne suffisent pas a masquer l'etat vide.
  if (!all.length && !leaveDays.size && !pendingDays.size) {
    bodyEl.appendChild(buildEmpty());
    return;
  }

  if (narrowQuery.matches) {
    bodyEl.appendChild(buildList(days, byDay));
  } else {
    bodyEl.appendChild(buildGrid(days, byDay, boundsOf(all)));
  }
}

/* ---------- Chargement ---------- */

function showError(message) {
  if (!message) {
    errorEl.hidden = true;
    errorEl.textContent = "";
    return;
  }
  errorEl.textContent = message;
  errorEl.hidden = false;
}

async function loadProjects() {
  try {
    const data = await callApi("/api/projects");
    (data.projects || []).forEach((project) => {
      projectColors.set(Number(project.id), project.color);
      const option = document.createElement("option");
      option.value = String(project.id);
      option.textContent = project.name ? String(project.name) : "Projet " + project.id;
      fields.project.appendChild(option);
    });
  } catch (error) {
    // Sans la liste des projets l'agenda reste utilisable : couleurs par type.
    toast("Projets indisponibles : " + error.message, true);
  }
}

/* Retient les jours non travailles de la semaine a partir de /api/days-off. */
function applyWeekDaysOff(data, days) {
  offDays = new Map();
  leaveDays = new Set();
  pendingDays = new Map();
  (data.calendar || []).forEach((item) => {
    if (item && !item.working && ISO_DAY.test(item.date || "")) {
      offDays.set(item.date, item.reason ? String(item.reason) : "");
    }
  });
  (data.days_off || []).forEach((entry) => {
    const first = String(entry.start_day || "");
    const last = String(entry.end_day || entry.start_day || "");
    days.forEach((day) => {
      if (day < first || day > last) return;
      if (entry.tentative) {
        if (!offDays.has(day)) pendingDays.set(day, entry.title ? String(entry.title) : "Congé");
      } else if (offDays.has(day)) {
        leaveDays.add(day);
      }
    });
  });
}

async function load() {
  const days = weekDays();
  const range = "?start=" + days[0] + "&end=" + days[6];
  // Les deux appels partent ensemble ; l'echec de l'un ne masque pas l'autre.
  const results = await Promise.allSettled([
    callApi("/api/events" + range),
    callApi("/api/days-off" + range),
  ]);

  const problems = [];
  if (results[0].status === "fulfilled") {
    events = results[0].value.events || [];
  } else {
    events = [];
    problems.push("Semaine indisponible : " + results[0].reason.message);
  }
  if (results[1].status === "fulfilled") {
    applyWeekDaysOff(results[1].value, days);
  } else {
    offDays = new Map();
    leaveDays = new Set();
    pendingDays = new Map();
    problems.push("Jours de repos indisponibles : " + results[1].reason.message);
  }
  showError(problems.length ? problems.join(" · ") : null);
  render();

  const year = fromIso(weekStart).getFullYear();
  if (year !== dayOffYear) loadDayOffYear(year);
}

/* ---------- Jours de repos de l'annee ---------- */

function showDayOffError(message) {
  if (!message) {
    dayOffErrorEl.hidden = true;
    dayOffErrorEl.textContent = "";
    return;
  }
  dayOffErrorEl.textContent = message;
  dayOffErrorEl.hidden = false;
}

function shortDate(iso, withYear) {
  const options = withYear
    ? { day: "numeric", month: "short", year: "numeric" }
    : { day: "numeric", month: "short" };
  return fromIso(iso).toLocaleDateString("fr-FR", options);
}

function periodLabel(entry) {
  const first = String(entry.start_day || "");
  const last = String(entry.end_day || first);
  if (!ISO_DAY.test(first)) return "—";
  if (last === first || !ISO_DAY.test(last)) {
    return fromIso(first).toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" });
  }
  return shortDate(first, first.slice(0, 4) !== last.slice(0, 4)) + " – " + shortDate(last, false);
}

function dayOffRow(entry) {
  const kind = DAY_OFF_LABELS[entry.kind] ? entry.kind : "leave";

  const row = document.createElement("div");
  row.className = "dayoff-row dayoff-" + kind;
  if (entry.tentative) row.classList.add("is-pending");

  const period = document.createElement("span");
  period.className = "dayoff-period";
  period.textContent = periodLabel(entry);
  row.appendChild(period);

  const body = document.createElement("span");
  body.className = "dayoff-body";

  const title = document.createElement("span");
  title.className = "dayoff-name";
  title.textContent = entry.title ? String(entry.title) : "(sans titre)";
  body.appendChild(title);

  const count = Number(entry.days) || 0;
  const meta = document.createElement("span");
  meta.className = "dayoff-meta";
  meta.textContent = count + (count > 1 ? " jours" : " jour") + (entry.country ? " · " + String(entry.country) : "");
  body.appendChild(meta);
  row.appendChild(body);

  const tag = document.createElement("span");
  tag.className = "tag dayoff-tag";
  tag.textContent = entry.tentative ? DAY_OFF_LABELS[kind] + " en attente" : DAY_OFF_LABELS[kind];
  if (entry.tentative) tag.title = "Non validé : le plan n'en tient pas compte.";
  row.appendChild(tag);

  // Une absence importee d'un agenda externe se supprime a la source, pas ici.
  if (entry.read_only) {
    const origin = document.createElement("span");
    origin.className = "tag dayoff-source";
    origin.textContent = "Importé";
    origin.title = "Importé d'un agenda externe : modifie-le à la source (Outlook ou Odoo).";
    row.appendChild(origin);
    return row;
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "icon-btn danger";
  remove.title = "Supprimer";
  remove.textContent = "✕";
  remove.addEventListener("click", () => removeDayOff(entry));
  row.appendChild(remove);

  return row;
}

function renderDayOffList(entries) {
  dayOffListEl.replaceChildren();
  if (!entries.length) {
    const nothing = document.createElement("p");
    nothing.className = "ag-day-empty";
    nothing.textContent = "Aucun congé ni férié en " + dayOffYear + ".";
    dayOffListEl.appendChild(nothing);
    return;
  }
  entries
    .slice()
    .sort((a, b) => String(a.start_day).localeCompare(String(b.start_day)))
    .forEach((entry) => dayOffListEl.appendChild(dayOffRow(entry)));
}

async function loadDayOffYear(year) {
  dayOffYear = year;
  dayOffHeadingEl.textContent = "Jours de repos " + year;
  holidaysEl.textContent = "Ajouter les fériés de " + year;
  try {
    const data = await callApi("/api/days-off?start=" + year + "-01-01&end=" + year + "-12-31");
    // Une navigation rapide peut faire revenir une reponse perimee.
    if (year !== dayOffYear) return;
    renderDayOffList(data.days_off || []);
    showDayOffError(null);
  } catch (error) {
    if (year !== dayOffYear) return;
    dayOffListEl.replaceChildren();
    showDayOffError("Jours de repos indisponibles : " + error.message);
  }
}

/* Recharge la semaine et la liste de l'annee apres une modification. */
async function refreshDaysOff() {
  await load();
  await loadDayOffYear(dayOffYear);
}

async function removeDayOff(entry) {
  const label = entry.title ? String(entry.title) : "ce jour de repos";
  if (!confirm("Supprimer « " + label + " » ?")) return;
  try {
    await callApi("/api/days-off/" + entry.id, { method: "DELETE" });
    await refreshDaysOff();
    toast("Jour de repos supprimé.");
  } catch (error) {
    showDayOffError("Suppression impossible : " + error.message);
  }
}

function readDayOffForm() {
  const title = dayOffFields.title.value.trim();
  const start = dayOffFields.start.value;
  const end = dayOffFields.end.value;

  if (!title) return { error: "Le titre est obligatoire." };
  if (!ISO_DAY.test(start)) return { error: "Choisis le premier jour." };
  if (end && end < start) return { error: "Le dernier jour doit suivre le premier." };

  const payload = { title: title, start_day: start, kind: dayOffFields.kind.value };
  if (end) payload.end_day = end;
  return { payload: payload };
}

dayOffFormEl.addEventListener("submit", async (submitEvent) => {
  submitEvent.preventDefault();

  const result = readDayOffForm();
  if (result.error) {
    showDayOffError(result.error);
    return;
  }

  dayOffSaveEl.disabled = true;
  try {
    await callApi("/api/days-off", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result.payload),
    });
    dayOffFields.title.value = "";
    dayOffFields.start.value = "";
    dayOffFields.end.value = "";
    showDayOffError(null);
    await refreshDaysOff();
    toast("Jour de repos ajouté.");
  } catch (error) {
    // La saisie reste en place pour etre corrigee.
    showDayOffError("Enregistrement impossible : " + error.message);
  } finally {
    dayOffSaveEl.disabled = false;
  }
});

holidaysEl.addEventListener("click", async () => {
  const year = dayOffYear;
  holidaysEl.disabled = true;
  try {
    const data = await callApi("/api/days-off/holidays", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ year: year }),
    });
    showDayOffError(null);
    await refreshDaysOff();
    toast((Number(data.added) || 0) + " férié(s) ajouté(s)");
  } catch (error) {
    showDayOffError("Import des fériés impossible : " + error.message);
  } finally {
    holidaysEl.disabled = false;
  }
});

/* ---------- Formulaire ---------- */

function showFormError(message) {
  if (!message) {
    formErrorEl.hidden = true;
    formErrorEl.textContent = "";
    return;
  }
  formErrorEl.textContent = message;
  formErrorEl.hidden = false;
}

function syncRepeatField() {
  untilBoxEl.hidden = fields.repeat.value === "none";
}

function defaultDay() {
  const today = todayIso();
  const days = weekDays();
  return days.indexOf(today) === -1 ? days[0] : today;
}

function openCreate(day) {
  editing = null;
  modalTitleEl.textContent = "Nouvel événement";
  seriesHintEl.hidden = true;
  deleteBoxEl.hidden = true;
  showFormError(null);

  fields.title.value = "";
  fields.date.value = day || defaultDay();
  fields.kind.value = "meeting";
  fields.start.value = "09:00";
  fields.end.value = "10:00";
  fields.location.value = "";
  fields.project.value = "";
  fields.repeat.value = "none";
  fields.until.value = "";
  fields.notes.value = "";
  syncRepeatField();

  modalEl.showModal();
  fields.title.focus();
}

/* ---------- Fiche de detail ---------- */

const URL_PATTERN = /(https?:\/\/[^\s<>"]+)/g;

/* Rend un texte libre avec ses liens cliquables (lien Teams d'une invitation),
 * sans jamais interpreter de HTML. */
function linkedText(container, text) {
  String(text).split(URL_PATTERN).forEach((part, index) => {
    if (index % 2 === 1) {
      const link = document.createElement("a");
      link.href = part;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.className = "link";
      link.textContent = part;
      container.appendChild(link);
    } else if (part) {
      container.appendChild(document.createTextNode(part));
    }
  });
}

function addFact(label, value) {
  if (!value) return;
  const term = document.createElement("dt");
  term.textContent = label;
  const data = document.createElement("dd");
  data.textContent = String(value);
  detailFactsEl.appendChild(term);
  detailFactsEl.appendChild(data);
}

function openDetail(event, segment) {
  detailed = { event: event, day: segment.day };

  detailTitleEl.textContent = event.title ? String(event.title) : "(sans titre)";
  detailWhenEl.textContent =
    fromIso(segment.day).toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" }) +
    " · " + clock(segment.from) + " – " + clock(segment.to);

  detailFactsEl.replaceChildren();
  addFact("Type", KIND_LABELS[kindOf(event)]);
  addFact("Lieu", event.location);
  addFact("Projet", event.project_name);
  if (event.is_occurrence || (event.repeat && event.repeat !== "none")) {
    addFact("Répétition", event.repeat === "daily" ? "Tous les jours" : "Toutes les semaines");
  }
  if (event.read_only) addFact("Source", sourceLabel(event));

  detailNotesEl.replaceChildren();
  detailNotesEl.hidden = !event.notes;
  if (event.notes) linkedText(detailNotesEl, event.notes);

  detailReadOnlyEl.hidden = !event.read_only;
  detailReadOnlyEl.textContent = event.read_only
    ? "Importé de " + sourceLabel(event) + " : lecture seule, modifie-le dans " + sourceLabel(event) + "."
    : "";
  detailEditEl.hidden = Boolean(event.read_only);
  // Un compte rendu d'absence n'a pas de sens ; une reunion importee d'Outlook en a un.
  detailMinutesEl.hidden = kindOf(event) === "absence";
  detailMinutesEl.disabled = false;

  detailEl.showModal();
}

function closeDetail() {
  detailed = null;
  if (detailEl.open) detailEl.close();
}

detailEditEl.addEventListener("click", () => {
  if (!detailed) return;
  const { event, day } = detailed;
  closeDetail();
  openEdit(event, day);
});
/* Compte rendu de la seance affichee : le serveur renvoie celui qui existe
 * deja ou en cree un (titre, date et projet repris de l'evenement). Le
 * libelle reste « Compte rendu » : savoir s'il existe couterait un appel
 * de plus a chaque ouverture de la fiche. */
const detailMinutesEl = document.createElement("button");
detailMinutesEl.type = "button";
detailMinutesEl.className = "btn-ghost";
detailMinutesEl.id = "evd-minutes";
detailMinutesEl.textContent = "Compte rendu";
detailEditEl.before(detailMinutesEl);

detailMinutesEl.addEventListener("click", async () => {
  if (!detailed) return;
  const { event } = detailed;
  detailMinutesEl.disabled = true;
  try {
    // starts_at est deja reporte sur le jour de l'occurrence pour une serie.
    const minutes = await callApi("/api/minutes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: event.id, held_on: String(event.starts_at).slice(0, 10) }),
    });
    window.location.href = "/comptes-rendus/" + minutes.id;
  } catch (error) {
    detailMinutesEl.disabled = false;
    toast(error.message, true);
  }
});

document.getElementById("evd-close").addEventListener("click", closeDetail);
document.getElementById("evd-dismiss").addEventListener("click", closeDetail);

function openEdit(event, day) {
  // Le serveur refuse toute modification d'un evenement importe : pas de formulaire.
  if (event.read_only) {
    showReadOnly(event);
    return;
  }

  const start = parseMoment(event.starts_at);
  const end = parseMoment(event.ends_at);
  const isSeries = Boolean(event.is_occurrence) || (event.repeat && event.repeat !== "none");

  editing = { id: event.id, day: day, is_occurrence: Boolean(event.is_occurrence) };

  modalTitleEl.textContent = "Modifier l'événement";
  seriesHintEl.hidden = !isSeries;
  showFormError(null);

  deleteBoxEl.hidden = false;
  deleteEl.hidden = Boolean(isSeries);
  deleteOneEl.hidden = !isSeries;
  deleteAllEl.hidden = !isSeries;

  fields.title.value = event.title ? String(event.title) : "";
  fields.date.value = start ? start.day : day;
  fields.kind.value = kindOf(event);
  fields.start.value = start ? clock(start.minutes) : "";
  fields.end.value = end ? clock(end.minutes) : "";
  fields.location.value = event.location ? String(event.location) : "";
  fields.project.value = event.project_id == null ? "" : String(event.project_id);
  fields.repeat.value = event.repeat && event.repeat !== "none" ? event.repeat : "none";
  fields.until.value = event.repeat_until ? String(event.repeat_until).slice(0, 10) : "";
  fields.notes.value = event.notes ? String(event.notes) : "";
  syncRepeatField();

  modalEl.showModal();
  fields.title.focus();
}

function readForm() {
  const title = fields.title.value.trim();
  const day = fields.date.value;
  const start = fields.start.value;
  const end = fields.end.value;

  if (!title) return { error: "Le titre est obligatoire." };
  if (!ISO_DAY.test(day)) return { error: "Choisis une date." };
  if (!start || !end) return { error: "Renseigne l'heure de début et l'heure de fin." };
  if (end <= start) return { error: "L'heure de fin doit être après l'heure de début." };

  const repeat = fields.repeat.value;
  const until = fields.until.value;
  if (repeat !== "none" && until && until < day) {
    return { error: "La fin de répétition doit être après la date de l'événement." };
  }

  return {
    payload: {
      title: title,
      starts_at: day + " " + start,
      ends_at: day + " " + end,
      kind: fields.kind.value,
      location: fields.location.value.trim() || null,
      notes: fields.notes.value.trim() || null,
      project_id: fields.project.value ? Number(fields.project.value) : null,
      repeat: repeat,
      repeat_until: repeat === "none" ? null : until || null,
    },
  };
}

formEl.addEventListener("submit", async (submitEvent) => {
  submitEvent.preventDefault();

  const result = readForm();
  if (result.error) {
    showFormError(result.error);
    return;
  }

  const url = editing ? "/api/events/" + editing.id : "/api/events";
  saveEl.disabled = true;
  try {
    await callApi(url, {
      method: editing ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result.payload),
    });
    modalEl.close();
    await load();
    toast(editing ? "Événement mis à jour." : "Événement ajouté.");
  } catch (error) {
    // La fenetre reste ouverte : la saisie n'est pas perdue.
    showFormError("Enregistrement impossible : " + error.message);
  } finally {
    saveEl.disabled = false;
  }
});

async function removeSeries() {
  if (!editing) return;
  if (!confirm("Supprimer cet événement et toutes ses répétitions ?")) return;
  try {
    await callApi("/api/events/" + editing.id, { method: "DELETE" });
    modalEl.close();
    await load();
    toast("Événement supprimé.");
  } catch (error) {
    showFormError("Suppression impossible : " + error.message);
  }
}

async function cancelOccurrence() {
  if (!editing) return;
  const day = editing.day || fields.date.value;
  if (!ISO_DAY.test(day)) {
    showFormError("Jour de l'occurrence introuvable.");
    return;
  }
  try {
    await callApi("/api/events/" + editing.id + "/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ day: day }),
    });
    modalEl.close();
    await load();
    toast("Occurrence annulée.");
  } catch (error) {
    showFormError("Annulation impossible : " + error.message);
  }
}

deleteEl.addEventListener("click", removeSeries);
deleteAllEl.addEventListener("click", removeSeries);
deleteOneEl.addEventListener("click", cancelOccurrence);

fields.repeat.addEventListener("change", syncRepeatField);
document.getElementById("ev-cancel").addEventListener("click", () => modalEl.close());

/* ---------- Navigation ---------- */

document.getElementById("ag-prev").addEventListener("click", () => {
  weekStart = addDays(weekStart, -7);
  load();
});

document.getElementById("ag-next").addEventListener("click", () => {
  weekStart = addDays(weekStart, 7);
  load();
});

document.getElementById("ag-today").addEventListener("click", () => {
  weekStart = mondayOf(todayIso());
  load();
});

document.getElementById("ag-new").addEventListener("click", () => openCreate());

// La bascule grille / liste ne depend que de la largeur : pas de rechargement.
if (narrowQuery.addEventListener) {
  narrowQuery.addEventListener("change", render);
} else {
  narrowQuery.addListener(render);
}

/* ---------- Initialisation ---------- */

headEl.textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

weekStart = mondayOf(todayIso());

// Lien profond /agenda?date=AAAA-MM-JJ (palette Ctrl+K) : ouvre la semaine de ce jour.
(function requestedWeek() {
  const params = new URLSearchParams(window.location.search);
  const day = params.get("date");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day || "")) return;
  history.replaceState(null, "", window.location.pathname);
  weekStart = mondayOf(day);
})();

loadProjects().then(load);

/* Rechargement a chaud : meme semaine, defilement de la grille garde, sans
 * rejouer l'entree des lignes ; la liste des jours de repos de l'annee suit. */
async function liveReload() {
  const before = bodyEl.querySelector(".agenda-scroll");
  const position = before ? { top: before.scrollTop, left: before.scrollLeft } : null;
  animateRows = false;
  try {
    await load();
    if (dayOffYear) await loadDayOffYear(dayOffYear);
  } finally {
    animateRows = true;
  }
  const after = bodyEl.querySelector(".agenda-scroll");
  if (position && after) {
    after.scrollTop = position.top;
    after.scrollLeft = position.left;
  }
}

J.live.onChange(liveReload);
