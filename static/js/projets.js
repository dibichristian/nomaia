/* NOMAIA — projets : tableau de bord general, une carte par projet.
 *
 * Les noms de projets viennent de l'API : tout passe par escapeHtml.
 */

const { toast, callApi, escapeHtml, formatDuration, formatDeadline } = window.J;

const listEl = document.getElementById("project-list");
const kpisEl = document.getElementById("pj-kpis");
const filtersEl = document.getElementById("pj-filters");

const Pilot = window.Pilot;
const donutEl = document.getElementById("pl-donut");
const stacksEl = document.getElementById("pl-stacks");

const STATUS_LABELS = { active: "Actif", paused: "En pause", done: "Terminé" };
const VIEW_KEY = "nomaia.projets.view";

let board = { projects: [], kpis: {} };
let filter = "current";
// Vue par defaut : le tableau, sauf sur telephone ou il defilerait de cote.
let view = window.matchMedia("(max-width: 640px)").matches ? "cards" : "table";
try {
  const choisie = localStorage.getItem(VIEW_KEY);
  if (choisie === "cards" || choisie === "table") view = choisie;
} catch (error) {
  // Stockage indisponible (navigation privee) : vue par defaut.
}

function statTile(value, label, modifier) {
  return (
    '<div class="stat' + (modifier ? " " + modifier : "") + '">' +
      '<span class="stat-value">' + escapeHtml(value) + "</span>" +
      '<span class="stat-label">' + label + "</span>" +
    "</div>"
  );
}

function renderKpis(kpis) {
  kpisEl.innerHTML =
    statTile(kpis.active_projects, "Projets actifs") +
    statTile(kpis.at_risk, "À surveiller", kpis.at_risk ? "stat-late" : "") +
    statTile(kpis.open_tasks, "Tâches ouvertes") +
    statTile(kpis.due_soon, "Échéances sous 7 j") +
    statTile(kpis.late_tasks, "En retard", kpis.late_tasks ? "stat-late" : "") +
    statTile(kpis.blocked, "Bloquées", kpis.blocked ? "stat-late" : "") +
    statTile(kpis.waiting_client, "Attente client") +
    statTile(
      kpis.anomalies_open,
      kpis.anomalies_critical ? "Anomalies ouvertes · " + kpis.anomalies_critical + " critique(s)" : "Anomalies ouvertes",
      kpis.anomalies_critical ? "stat-late" : ""
    ) +
    statTile(kpis.deliverables_awaited, "Livrables attendus") +
    statTile(kpis.done_week, "Terminées cette semaine", "stat-done") +
    statTile(formatDuration(kpis.tracked_week) || "0 min", "Suivies cette semaine");
  Pilot.stagger(kpisEl.children);
  Pilot.countUp(kpisEl);
}

function renderCharts(projects) {
  donutEl.innerHTML = Pilot.donut(board.kpis.task_states || {});
  const lignes = projects.filter(
    (project) => Object.values(project.task_states).some(Boolean)
  );
  stacksEl.innerHTML = lignes.length
    ? lignes
        .map((project) => {
          const somme = Object.values(project.task_states).reduce((a, b) => a + b, 0);
          return (
            '<a class="pl-stack-row" href="/projets/' + project.id + '">' +
              '<span class="pl-stack-name">' + escapeHtml(project.name) + "</span>" +
              Pilot.stackBar(project.task_states) +
              '<span class="pl-stack-total">' + somme + "</span>" +
            "</a>"
          );
        })
        .join("") + Pilot.legend()
    : '<p class="pj-faint">Aucune tâche rattachée aux projets de ce filtre.</p>';
  Pilot.stagger(stacksEl.querySelectorAll(".pl-stack-row"));
}

function milestoneDate(jalon) {
  if (!jalon.date) return '<span class="pl-cell-sub">date à fixer</span>';
  const delai = Pilot.daysLabel(jalon.days_left);
  return (
    '<span class="pl-cell-sub' + (jalon.days_left < 0 ? " is-late" : "") + '">' +
      "jalon " + formatDeadline(jalon.date) + (delai ? " · " + delai : "") + "</span>"
  );
}

/* Etape en cours et prochain jalon. Le plus souvent c'est la meme etape :
 * le jalon n'est detaille a part que lorsqu'il differe. */
function phaseCell(project) {
  const courante = project.current_phase;
  const jalon = project.next_milestone;
  if (!courante) return '<span class="pj-faint">' + (project.phases.length ? "Terminé" : "—") + "</span>";
  let html =
    '<span class="pl-cell-main">' + escapeHtml(courante.name) + "</span>" +
    '<span class="pl-cell-sub">' + Pilot.PHASE_LABELS[courante.status] + " · " + courante.progress_pct + " %</span>";
  if (jalon && jalon.id !== courante.id) {
    html += '<span class="pl-cell-sub">prochain jalon : ' + escapeHtml(jalon.name) + "</span>";
  }
  return html + (jalon ? milestoneDate(jalon) : "");
}

/* Alertes en pastille : le detail est dans l'infobulle et sur la fiche. */
function signalsCell(signals) {
  if (!signals.length) return '<span class="pl-ok" title="Aucune alerte">✓</span>';
  const grave = signals.some((signal) => signal.level === "danger");
  return (
    '<span class="pj-signal is-' + (grave ? "danger" : "warn") + '" title="' +
      escapeHtml(signals.map((signal) => signal.label).join("\n")) + '">' +
      signals.length + " alerte" + (signals.length > 1 ? "s" : "") + "</span>" +
    '<span class="pl-cell-sub pl-signal-first">' + escapeHtml(signals[0].label) + "</span>"
  );
}

function followCell(project) {
  if (!project.last_activity) return '<span class="pj-faint">jamais</span>';
  const vieux = project.idle_days >= 7;
  return (
    '<span class="pl-cell-main">' + formatDeadline(project.last_activity) + "</span>" +
    '<span class="pl-cell-sub' + (vieux ? " is-late" : "") + '">il y a ' + project.idle_days + " j</span>"
  );
}

function tableRow(project) {
  const courante = project.current_phase;
  const somme = Object.values(project.task_states).reduce((a, b) => a + b, 0);
  return (
    '<tr style="--pj-color:' + escapeHtml(project.color) + '">' +
      '<td class="pl-col-name"><a class="pl-name-link" href="/projets/' + project.id + '">' +
        escapeHtml(project.name) + "</a>" + window.J.spaceBadge(project.space) +
        '<span class="pl-cell-sub">' + (STATUS_LABELS[project.status] || project.status) +
          (project.go_live ? " · bascule " + formatDeadline(project.go_live) : "") + "</span></td>" +
      '<td class="pl-col-weather"' + (project.weather_note ? ' title="' + escapeHtml(project.weather_note) + '"' : "") + ">" +
        (Pilot.weather(project.weather) || '<span class="pj-faint">—</span>') + "</td>" +
      '<td class="pl-col-progress"><div class="pj-progress">' +
        '<div class="load-bar"><div class="load-fill" ' + Pilot.fillStyle(project.progress_pct) + "></div></div>" +
        '<span class="pj-progress-value">' + project.progress_pct + " %</span></div></td>" +
      '<td class="pl-col-strip">' + Pilot.phaseStrip(project.phases, courante && courante.id) + "</td>" +
      '<td class="pl-col-phase">' + phaseCell(project) + "</td>" +
      '<td class="pl-col-tasks">' + Pilot.stackBar(project.task_states) +
        '<span class="pl-cell-sub">' + somme + " tâche(s) · " + project.task_states.done + " ✓</span></td>" +
      "<td>" + followCell(project) + "</td>" +
      '<td class="pl-col-signals">' + signalsCell(project.signals) + "</td>" +
    "</tr>"
  );
}

function renderTable(projects) {
  return (
    '<div class="pl-table-wrap"><table class="pl-table">' +
      "<thead><tr>" +
        "<th>Projet</th><th>Météo</th><th>Avancement</th><th>Étapes</th><th>Étape en cours · jalon</th>" +
        "<th>Tâches</th><th>Dernier suivi</th><th>Alertes</th>" +
      "</tr></thead>" +
      "<tbody>" + projects.map(tableRow).join("") + "</tbody>" +
    "</table></div>"
  );
}

function deadlineLabel(project) {
  if (!project.deadline) return "sans échéance";
  const jours = project.days_left;
  const quand = formatDeadline(project.deadline);
  if (project.status === "done") return "échéance " + quand;
  if (jours < 0) return quand + " · dépassée";
  if (jours === 0) return "aujourd'hui";
  return quand + " · J-" + jours;
}

function projectCard(project) {
  const signals = project.signals
    .map((signal) => '<span class="pj-signal is-' + signal.level + '">' + escapeHtml(signal.label) + "</span>")
    .join("");
  const semaine = [];
  if (project.done_week) semaine.push(project.done_week + " terminée(s)");
  if (project.tracked_week) semaine.push(formatDuration(project.tracked_week) + " suivies");

  return (
    '<a class="pj-card' + (project.signals.length ? " has-signal" : "") + '" href="/projets/' +
      project.id + '" style="--pj-color:' + escapeHtml(project.color) + '">' +
      '<div class="pj-card-head">' +
        '<span class="pj-name">' + Pilot.weather(project.weather) + " " + escapeHtml(project.name) +
          window.J.spaceBadge(project.space) + "</span>" +
        '<span class="pj-status s-' + escapeHtml(project.status) + '">' +
          (STATUS_LABELS[project.status] || project.status) + "</span>" +
      "</div>" +
      (project.phases.length
        ? Pilot.phaseStrip(project.phases, project.current_phase && project.current_phase.id) +
          (project.current_phase
            ? '<p class="pj-line">Étape : ' + escapeHtml(project.current_phase.name) + "</p>"
            : "")
        : "") +
      '<div class="pj-progress">' +
        '<div class="load-bar"><div class="load-fill" ' + Pilot.fillStyle(project.progress_pct) + "></div></div>" +
        '<span class="pj-progress-value">' + project.progress_pct + " %</span>" +
      "</div>" +
      '<p class="pj-line">' + project.open_count + " ouverte(s) · " + project.done_count + " terminée(s)" +
        " · reste " + (formatDuration(project.remaining_min) || "0 min") + "</p>" +
      '<p class="pj-line">' + escapeHtml(deadlineLabel(project)) +
        (project.tracked_min ? " · " + formatDuration(project.tracked_min) + " passées" : "") + "</p>" +
      (semaine.length ? '<p class="pj-line pj-week">Cette semaine : ' + semaine.join(", ") + "</p>" : "") +
      (signals ? '<div class="pj-signals">' + signals + "</div>" : "") +
      (project.last_activity
        ? '<p class="pj-line pj-faint">Dernière activité ' + formatDeadline(project.last_activity) + "</p>"
        : "") +
    "</a>"
  );
}

function visibleProjects() {
  return board.projects.filter((project) => {
    if (filter === "current") return project.status !== "done";
    if (filter === "risk") return project.status !== "done" && project.signals.length;
    if (filter === "done") return project.status === "done";
    return true;
  });
}

function renderList() {
  const projects = visibleProjects();
  if (!board.projects.length) {
    listEl.innerHTML =
      '<p class="empty">Aucun projet. NOMAIA rattachera les tâches dès qu\'il en trouvera un.</p>';
    return;
  }
  renderCharts(projects);
  listEl.className = view === "cards" ? "pj-grid" : "";
  if (!projects.length) {
    listEl.innerHTML = '<p class="empty">Aucun projet dans ce filtre.</p>';
  } else {
    listEl.innerHTML = view === "cards" ? projects.map(projectCard).join("") : renderTable(projects);
    Pilot.stagger(listEl.querySelectorAll(view === "cards" ? ".pj-card" : ".pl-table tbody tr"));
  }
}

async function reload() {
  try {
    board = await callApi("/api/projects/board?space=current");
    renderKpis(board.kpis);
    renderList();
  } catch (error) {
    listEl.innerHTML = '<p class="empty">' + escapeHtml(error.message) + "</p>";
  }
}

filtersEl.addEventListener("click", (event) => {
  const viewChip = event.target.closest("[data-view]");
  if (viewChip) {
    view = viewChip.dataset.view;
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch (error) {
      // Preference non memorisee : sans consequence.
    }
    syncViewChips();
    renderList();
    return;
  }
  const chip = event.target.closest("[data-filter]");
  if (!chip) return;
  filter = chip.dataset.filter;
  filtersEl.querySelectorAll("[data-filter]").forEach((node) => {
    node.classList.toggle("is-on", node === chip);
    node.setAttribute("aria-pressed", String(node === chip));
  });
  renderList();
});

function syncViewChips() {
  filtersEl.querySelectorAll("[data-view]").forEach((node) => {
    node.classList.toggle("is-on", node.dataset.view === view);
    node.setAttribute("aria-pressed", String(node.dataset.view === view));
  });
}

syncViewChips();

document.getElementById("project-add").addEventListener("submit", async (event) => {
  event.preventDefault();
  const nameInput = document.getElementById("pr-name");

  try {
    const created = await callApi("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: nameInput.value,
        color: document.getElementById("pr-color").value,
        deadline: document.getElementById("pr-deadline").value,
        attach_matching: document.getElementById("pr-attach").checked,
        // Espace filtre dans la barre du haut : le projet reste visible apres creation.
        space: window.J.space === "all" ? undefined : window.J.space,
      }),
    });
    nameInput.value = "";
    await reload();
    toast(
      created.attached
        ? "Projet créé, " + created.attached + " tâche(s) rattachée(s)."
        : "Projet créé."
    );
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("header-date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

reload();

// Rechargement a chaud : filtre et vue gardes ; la page redessinee ne rejoue
// ni la cascade ni le remplissage des barres (.is-settled, comme la fiche projet).
J.live.onChange(() => {
  document.querySelector("main.container").classList.add("is-settled");
  return reload();
});
