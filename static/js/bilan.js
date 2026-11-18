/* NOMAIA — bilan de periode : chiffres bruts puis analyse de l'IA. */

const { toast, callApi, escapeHtml, formatDuration, formatDeadline } = window.J;

const statsEl = document.getElementById("bl-stats");
const chartsEl = document.getElementById("bl-charts");
const hoursEl = document.getElementById("bl-hours");
const notesEl = document.getElementById("bl-notes");
const workLogEl = document.getElementById("bl-worklog");
const projectsEl = document.getElementById("bl-projects");
const reviewEl = document.getElementById("bl-review");
const periodEl = document.getElementById("bl-period");
const customEl = document.getElementById("bl-custom");
const startEl = document.getElementById("bl-start");
const endEl = document.getElementById("bl-end");
const historyEl = document.getElementById("bl-history");

// Mouvements d'un projet sur la periode, dans l'ordre d'affichage.
const ACTIVITY_LABELS = [
  ["tasks_done", "tâche(s) terminée(s)"],
  ["phases_done", "étape(s) franchie(s)"],
  ["anomalies_opened", "anomalie(s) remontée(s)"],
  ["anomalies_fixed", "anomalie(s) corrigée(s)"],
  ["deliverables_received", "livrable(s) reçu(s)"],
  ["register_entries", "entrée(s) au registre"],
];

const Pilot = window.Pilot;

const DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];

function statTile(value, label, modifier) {
  return (
    '<div class="stat' + (modifier ? " " + modifier : "") + '">' +
      '<span class="stat-value">' + escapeHtml(value) + "</span>" +
      '<span class="stat-label">' + label + "</span>" +
    "</div>"
  );
}

/* Une barre horizontale ; `tag` permet d'en faire le resume d'un <details>. */
function barRow(row, labelKey, valueKey, formatter, max, tag) {
  const element = tag || "div";
  return (
    "<" + element + ' class="bar-row">' +
      '<span class="bar-label" title="' + escapeHtml(row[labelKey]) + '">' + escapeHtml(row[labelKey]) + "</span>" +
      '<div class="bar-track"><div class="bar-fill" ' +
        Pilot.fillStyle((row[valueKey] / max) * 100, row.color ? "background:" + escapeHtml(row.color) : "") +
        "></div></div>" +
      '<span class="bar-value">' + formatter(row[valueKey]) + "</span>" +
    "</" + element + ">"
  );
}

function bars(rows, labelKey, valueKey, formatter) {
  const max = Math.max(...rows.map((row) => row[valueKey]), 1);
  return rows.map((row) => barRow(row, labelKey, valueKey, formatter, max)).join("");
}

// Etat d'ouverture du detail « Sans projet », conserve d'un rechargement a l'autre.
let unassignedOpen = false;
// Projets actifs proposes au rattachement, charges a la premiere ouverture.
let projectChoices = null;

/* Temps par projet. La barre « Sans projet » se deplie sur les taches a
   rattacher, chacune avec son selecteur de projet. */
function projectBars(rows, unassigned) {
  const max = Math.max(...rows.map((row) => row.minutes), 1);
  return rows
    .map((row) => {
      if (row.project_id != null || !unassigned.length) {
        return barRow(row, "project", "minutes", formatDuration, max);
      }
      return (
        '<details class="bl-unassigned"' + (unassignedOpen ? " open" : "") + ">" +
          barRow(row, "project", "minutes", formatDuration, max, "summary") +
          '<ul class="bl-unassigned-list">' +
          unassigned
            .map(
              (task) =>
                '<li class="bl-unassigned-item" data-id="' + task.id + '">' +
                  '<span class="bl-unassigned-title">' + escapeHtml(task.title) + "</span>" +
                  '<span class="bl-unassigned-min">' + formatDuration(task.minutes) + "</span>" +
                  '<select class="bl-assign-select" aria-label="Projet de « ' + escapeHtml(task.title) + ' »">' +
                    projectOptions() +
                  "</select>" +
                  '<button type="button" class="btn-ghost bl-assign-btn">Rattacher</button>' +
                "</li>"
            )
            .join("") +
          "</ul>" +
        "</details>"
      );
    })
    .join("");
}

function projectOptions() {
  return (
    '<option value="">' + (projectChoices ? "Projet…" : "Chargement…") + "</option>" +
    (projectChoices || [])
      .map((project) => '<option value="' + project.id + '">' + escapeHtml(project.name) + "</option>")
      .join("")
  );
}

/* Charge une fois la liste des projets actifs, puis remplit les selecteurs. */
async function fillProjectChoices() {
  if (!projectChoices) {
    try {
      projectChoices = (await callApi("/api/projects?status=active")).projects || [];
    } catch (error) {
      toast("Projets indisponibles : " + error.message, true);
      return;
    }
  }
  chartsEl.querySelectorAll(".bl-assign-select").forEach((select) => {
    if (!select.value) select.innerHTML = projectOptions();
  });
}

/* Carte d'un graphique ; message neutre quand la periode n'a rien a montrer. */
function chartCard(title, body, emptyText) {
  return (
    '<div class="card bl-chart"><h3 class="window-title">' + title + "</h3>" +
      (body || '<p class="bl-chart-empty">' + emptyText + "</p>") +
    "</div>"
  );
}

const LOG_DAY_FORMAT = { weekday: "long", day: "numeric", month: "long" };

/* Journal des sessions Claude, replie par defaut : un groupe par jour, le
   plus recent d'abord, une ligne par entree (heure, projet, tache, duree). */
function renderWorkLog(log, agent) {
  if (!log.length) {
    workLogEl.innerHTML = "";
    return;
  }

  const total = log.reduce((sum, entry) => sum + (entry.minutes || 0), 0);
  const known = (agent && agent.entries) || log.length;
  const count = log.length + " entrée" + (log.length > 1 ? "s" : "") +
    (known > log.length ? " (sur " + known + ")" : "");

  const days = [];
  log
    .slice()
    .reverse()
    .forEach((entry) => {
      const day = entry.created_at.slice(0, 10);
      if (!days.length || days[days.length - 1].day !== day) days.push({ day, entries: [] });
      days[days.length - 1].entries.push(entry);
    });

  workLogEl.innerHTML =
    '<details class="card bl-log">' +
      '<summary class="bl-log-summary">' +
        '<span class="bl-log-heading">Journal des sessions Claude</span>' +
        '<span class="bl-log-meta">' + count + (total ? " · " + formatDuration(total) : "") + "</span>" +
      "</summary>" +
      days
        .map(
          (group) =>
            '<section class="bl-log-day"><h4 class="bl-log-date">' +
              escapeHtml(new Date(group.day + "T00:00:00").toLocaleDateString("fr-FR", LOG_DAY_FORMAT)) +
            "</h4><ul class=\"bl-log-list\">" +
            group.entries.map(logEntry).join("") +
            "</ul></section>"
        )
        .join("") +
    "</details>";
}

function logEntry(entry) {
  const project = entry.project_name || "Sans projet";
  return (
    '<li class="bl-log-entry">' +
      '<div class="bl-log-line">' +
        '<span class="bl-log-time">' + escapeHtml(entry.created_at.slice(11, 16)) + "</span>" +
        '<span class="bl-log-dot" title="' + escapeHtml(project) + '"' +
          (entry.project_color ? ' style="--pj-color:' + escapeHtml(entry.project_color) + '"' : "") + "></span>" +
        '<span class="bl-log-task">' + escapeHtml(entry.task_title || project) + "</span>" +
        '<span class="bl-log-min">' + (formatDuration(entry.minutes) || "") + "</span>" +
      "</div>" +
      '<p class="bl-log-text" title="' + escapeHtml(entry.content) + '">' + escapeHtml(entry.content) + "</p>" +
    "</li>"
  );
}

/* Facteur de correction applique par NOMAIA a ses futures estimations : il se
   deduit du temps reellement chronometre, pas de la periode affichee. */
function calibrationNote(calibration) {
  if (!calibration || !calibration.global) return "";

  const parts = calibration.projects
    .slice(0, 4)
    .map((item) => escapeHtml(item.project) + " ×" + item.ratio);

  return (
    '<p class="estimation-note">Calibrage appliqué au triage (' +
    calibration.window_days + " derniers jours, " + calibration.global.tasks +
    " tâche(s)) : ×" + calibration.global.ratio +
    (parts.length ? " au global — " + parts.join(", ") : "") +
    ".</p>"
  );
}

function renderStats(data) {
  const summary = data.summary;
  const estimation = data.estimation;

  document.getElementById("bl-range").textContent =
    formatDeadline(data.period_start) + " → " + formatDeadline(data.period_end);

  statsEl.innerHTML =
    statTile(summary.done, "Terminées", "stat-done") +
    statTile(summary.created, "Créées") +
    statTile(summary.captures, "Entrées déposées") +
    statTile(formatDuration(summary.tracked_min) || "0 min", "Temps suivi") +
    statTile(formatDuration((data.agent || {}).minutes) || "0 min", "Dont sessions Claude") +
    statTile(summary.late_tasks, "En retard", "stat-late");
  Pilot.stagger(statsEl.children);
  Pilot.countUp(statsEl);

  if (!(data.unassigned_tasks || []).length) unassignedOpen = false;

  const dayRows = data.done_by_day.map((row) => ({
    label: DAYS[new Date(row.day + "T00:00:00").getDay() === 0 ? 6
      : new Date(row.day + "T00:00:00").getDay() - 1],
    count: row.count,
  }));

  chartsEl.innerHTML =
    '<div class="bl-charts">' +
      chartCard(
        "Temps par projet",
        data.tracked_by_project.length
          ? projectBars(data.tracked_by_project, data.unassigned_tasks || [])
          : "",
        "Aucun temps suivi sur la période."
      ) +
      chartCard(
        "Tâches terminées par jour",
        dayRows.length ? bars(dayRows, "label", "count", (value) => value) : "",
        "Aucune tâche terminée sur la période."
      ) +
    "</div>";
  if (unassignedOpen) fillProjectChoices();

  let html = "";

  if (estimation.ratio !== null) {
    const ecart = Math.round((estimation.ratio - 1) * 100);
    const sens = ecart >= 0 ? "de plus que prévu" : "de moins que prévu";
    html +=
      '<p class="estimation-note">Sur ' + estimation.tasks + " tâche(s) chronométrée(s) : " +
      formatDuration(estimation.estimated_min) + " estimées contre " +
      formatDuration(estimation.tracked_min) + " réelles — " +
      Math.abs(ecart) + " % " + sens + ".</p>";
  } else if (summary.done) {
    html += '<p class="estimation-note">Aucun temps chronométré sur la période : ' +
      "lance le minuteur depuis le plan du jour pour mesurer l'écart avec tes estimations.</p>";
  }

  html += calibrationNote(data.calibration);

  notesEl.innerHTML = html;
  renderProjects(data.projects);
  renderWorkLog(data.work_log || [], data.agent);
}

/* ---------- Heures planifiees et suivies, jour par jour ---------- */

const HOURS_DAY_FORMAT = { weekday: "short", day: "numeric", month: "short" };
const asMinutes = (minutes) => formatDuration(minutes) || "0 min";

function hoursRow(row, scale) {
  const day = new Date(row.date + "T00:00:00").toLocaleDateString("fr-FR", HOURS_DAY_FORMAT);
  const planned = row.planned_min;
  const title = "Planifié " + (planned === null ? "non noté" : asMinutes(planned)) +
    " · Suivi " + asMinutes(row.tracked_min) +
    (row.capacity_min ? " · Capacité " + formatDuration(row.capacity_min) : "");
  return (
    '<div class="bh-row' + (row.working ? "" : " is-off") + '" title="' + escapeHtml(title) + '">' +
      '<span class="bh-day">' + escapeHtml(day) + "</span>" +
      '<span class="bh-bars">' +
        '<span class="bh-bar is-planned"><span style="--pct:' + Math.min((planned || 0) / scale, 1) + '"></span></span>' +
        '<span class="bh-bar is-real"><span style="--pct:' + Math.min(row.tracked_min / scale, 1) + '"></span></span>' +
      "</span>" +
      '<span class="bh-values"><strong>' + (planned === null ? "—" : asMinutes(planned)) + "</strong> / " +
        asMinutes(row.tracked_min) + "</span>" +
    "</div>"
  );
}

/* Carte « Heures planifiees et suivies » : une ligne par jour de la periode. */
function renderHours(data) {
  const rows = data.days;
  const totals = data.totals;
  let body = "";

  if (rows.length) {
    // Echelle commune : la plus grande valeur de la periode (une heure au minimum).
    const scale = Math.max(60, ...rows.map((row) => Math.max(row.capacity_min || 0, row.planned_min || 0, row.tracked_min)));
    const days = totals.planned_days;
    const ratio = totals.planned_min ? Math.round(totals.tracked_on_planned_min / totals.planned_min * 100) : null;
    body =
      rows.map((row) => hoursRow(row, scale)).join("") +
      '<p class="bh-totals">Total : ' + asMinutes(totals.planned_min) + " planifiées sur " + days + " jour" +
        (days > 1 ? "s notés" : " noté") + " · " + asMinutes(totals.tracked_min) + " suivies" +
        (ratio !== null ? " · suivi = " + ratio + " % du planifié" : "") +
        (days < totals.days ? " · le planifié est noté à chaque ouverture du plan du jour" : "") +
        (data.truncated ? " · limité aux " + rows.length + " derniers jours" : "") + "</p>";
  }

  hoursEl.innerHTML =
    '<div class="card bl-chart bl-hours">' +
      '<div class="bh-head"><h3 class="window-title">Heures planifiées et suivies</h3>' +
        '<span class="bh-legend" aria-hidden="true">' +
          '<span class="bh-key"><i class="bh-dot is-planned"></i>Planifié</span>' +
          '<span class="bh-key"><i class="bh-dot is-real"></i>Suivi</span>' +
        "</span></div>" +
      (body || '<p class="bl-chart-empty">Aucune journée écoulée sur la période.</p>') +
    "</div>";
}

/* Charge les heures de la periode ; la carte se vide en cas d'echec. */
async function loadHours(query) {
  try {
    renderHours(await callApi("/api/stats/hours?" + query));
  } catch (error) {
    hoursEl.innerHTML = "";
  }
}

/* Parametres de la periode choisie : semaine, mois ou dates libres. Null
   tant que les dates libres sont incompletes. */
function periodQuery() {
  const value = periodEl.value;
  if (value === "custom") {
    if (!startEl.value || !endEl.value) return null;
    return "start=" + startEl.value + "&end=" + endEl.value;
  }
  return (value[0] === "m" ? "months_back=" : "weeks_back=") + value.slice(1);
}

/* Projets : etat actuel (avancement, etape, alertes) et mouvements de la periode. */
function renderProjects(projects) {
  if (!projects || !projects.length) {
    projectsEl.innerHTML = "";
    return;
  }

  projectsEl.innerHTML =
    '<h3 class="window-title">Projets</h3><div class="bl-projects">' +
    projects
      .map((project) => {
        const meteo = Pilot.WEATHER[project.weather];
        const etat = [];
        if (project.current_phase) etat.push("Étape : " + escapeHtml(project.current_phase));
        if (project.next_milestone && project.next_milestone.date) {
          etat.push(
            "Jalon « " + escapeHtml(project.next_milestone.name) + " » le " +
            formatDeadline(project.next_milestone.date)
          );
        }
        if (project.anomalies_open) {
          etat.push(
            project.anomalies_open + " anomalie(s) ouverte(s)" +
            (project.anomalies_critical ? ", dont " + project.anomalies_critical + " critique(s)" : "")
          );
        }
        if (project.deliverables_awaited) {
          etat.push(project.deliverables_awaited + " livrable(s) client attendu(s)");
        }
        const mouvements = ACTIVITY_LABELS
          .filter(([key]) => project.activity[key])
          .map(([key, label]) => project.activity[key] + " " + label);

        return (
          '<a class="bl-project" href="/projets/' + project.id + '" style="--pj-color:' +
            escapeHtml(project.color) + '">' +
            '<div class="pj-card-head"><span class="pj-name">' +
              (meteo ? '<span title="' + meteo.label + '">' + meteo.icon + "</span> " : "") +
              escapeHtml(project.name) + "</span>" +
              '<span class="pj-progress-value">' + project.progress_pct + " %</span></div>" +
            '<div class="load-bar"><div class="load-fill" ' + Pilot.fillStyle(project.progress_pct) + "></div></div>" +
            (etat.length ? '<p class="pj-line">' + etat.join(" · ") + "</p>" : "") +
            '<p class="pj-line pj-week">' +
              (mouvements.length ? "Sur la période : " + mouvements.join(", ") : "Aucun mouvement sur la période") +
            "</p>" +
            project.signals.map((label) => '<p class="pj-line bl-signal">⚠ ' + escapeHtml(label) + "</p>").join("") +
          "</a>"
        );
      })
      .join("") +
    "</div>";
  Pilot.stagger(projectsEl.querySelectorAll(".bl-project"));
}

function renderReview(review) {
  if (!review) {
    reviewEl.innerHTML = '<p class="empty">Aucun bilan généré pour cette période.</p>';
    return;
  }

  const payload = review.payload;
  let html = '<div class="review-head">Bilan du ' + formatDeadline(review.period_start) +
    " au " + formatDeadline(review.period_end) + "</div>";

  html += '<p class="review-summary">' + escapeHtml(payload.resume) + "</p>";

  if (payload.points_forts.length) {
    html += '<h3 class="window-title">Points forts</h3><ul class="review-list good">' +
      payload.points_forts.map((item) => "<li>" + escapeHtml(item) + "</li>").join("") + "</ul>";
  }

  if (payload.points_faibles.length) {
    html += '<h3 class="window-title">Points faibles</h3><ul class="review-list bad">' +
      payload.points_faibles.map((item) => "<li>" + escapeHtml(item) + "</li>").join("") + "</ul>";
  }

  if (payload.recommandations.length) {
    html += '<h3 class="window-title">Recommandations</h3>' +
      payload.recommandations
        .map(
          (item) =>
            '<div class="reco"><strong>' + escapeHtml(item.action) + "</strong>" +
            (item.pourquoi ? "<span>" + escapeHtml(item.pourquoi) + "</span>" : "") +
            "</div>"
        )
        .join("");
  }

  if (payload.focus_semaine) {
    html += '<div class="focus">Focus : ' + escapeHtml(payload.focus_semaine) + "</div>";
  }

  reviewEl.innerHTML = html;
  Pilot.stagger(reviewEl.querySelectorAll(".reco"));
}

function renderHistory(reviews) {
  if (!reviews.length) {
    historyEl.innerHTML = '<li class="empty">Aucun bilan enregistré.</li>';
    return;
  }
  historyEl.innerHTML = reviews
    .map(
      (item) =>
        '<li><button type="button" class="review-history-item" data-id="' + item.id +
          '" data-start="' + escapeHtml(item.period_start) + '" data-end="' + escapeHtml(item.period_end) + '">' +
          '<span class="work-log-when">' + formatDeadline(item.period_start) + " → " +
            formatDeadline(item.period_end) + "</span>" +
          "<span>" + escapeHtml(item.resume) + "</span>" +
        "</button></li>"
    )
    .join("");
  Pilot.stagger(historyEl.children);
}

async function loadHistory() {
  try {
    renderHistory((await callApi("/api/reviews")).reviews);
  } catch (error) {
    historyEl.innerHTML = '<li class="empty">' + escapeHtml(error.message) + "</li>";
  }
}

/* Charge les chiffres de la periode, puis un bilan precis (historique) ou le
   dernier genere pour cette periode. */
async function reload(reviewId) {
  const query = periodQuery();
  if (!query) return;
  try {
    renderStats(await callApi("/api/stats?" + query));
    loadHours(query);
    const data = await callApi(reviewId ? "/api/reviews/" + reviewId : "/api/review?" + query);
    renderReview(data.review);
  } catch (error) {
    statsEl.innerHTML = '<p class="empty">' + escapeHtml(error.message) + "</p>";
  }
}

periodEl.addEventListener("change", () => {
  customEl.hidden = periodEl.value !== "custom";
  reload();
});
startEl.addEventListener("change", () => reload());
endEl.addEventListener("change", () => reload());

historyEl.addEventListener("click", (event) => {
  const item = event.target.closest(".review-history-item");
  if (!item) return;
  periodEl.value = "custom";
  customEl.hidden = false;
  startEl.value = item.dataset.start;
  endEl.value = item.dataset.end;
  reload(item.dataset.id);
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// « toggle » ne remonte pas : ecoute en phase de capture.
chartsEl.addEventListener(
  "toggle",
  (event) => {
    if (!event.target.classList.contains("bl-unassigned")) return;
    unassignedOpen = event.target.open;
    if (unassignedOpen) fillProjectChoices();
  },
  true
);

/* Rattache une tache sans projet, puis recharge les chiffres de la periode. */
chartsEl.addEventListener("click", async (event) => {
  const button = event.target.closest(".bl-assign-btn");
  if (!button) return;
  const item = button.closest(".bl-unassigned-item");
  const select = item.querySelector(".bl-assign-select");
  if (!select.value) {
    toast("Choisis un projet.", true);
    select.focus();
    return;
  }
  const query = periodQuery();
  button.disabled = true;
  try {
    await callApi("/api/tasks/" + item.dataset.id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: Number(select.value) }),
    });
    toast("Tâche rattachée à " + select.options[select.selectedIndex].text + ".");
    if (query) renderStats(await callApi("/api/stats?" + query));
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

// Texte d'une entree du journal : tronque a deux lignes, un clic le deplie.
workLogEl.addEventListener("click", (event) => {
  const text = event.target.closest(".bl-log-text");
  if (text) text.classList.toggle("is-open");
});

document.getElementById("bl-generate").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const query = periodQuery();
  if (!query) {
    toast("Choisis les deux dates de la période.", true);
    return;
  }
  button.disabled = true;
  button.textContent = "Analyse en cours…";

  try {
    const data = await callApi("/api/review?" + query, { method: "POST" });
    renderReview(data.review);
    loadHistory();
    toast("Bilan généré.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Générer le bilan";
  }
});

document.getElementById("header-date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

reload();
loadHistory();

/* Rechargement a chaud : chiffres de la periode affichee et historique, sans
 * rejouer l'entree (.is-settled) ; l'analyse affichee et le rapport (message
 * modifiable) ne sont pas touches. Un rattachement en cours de choix differe. */
J.live.onChange(async () => {
  const query = periodQuery();
  if (!query) return true;
  const pending = Array.from(chartsEl.querySelectorAll(".bl-assign-select")).some((select) => select.value);
  if (pending) return false;
  document.querySelector("main.container").classList.add("is-settled");
  try {
    renderStats(await callApi("/api/stats?" + query));
    await loadHours(query);
  } catch (error) {
    // Serveur injoignable : les chiffres affiches restent en place.
  }
  await loadHistory();
  return true;
});

/* ---------- Rapport d'avancement pour un chef de projet ---------- */

const managerEl = document.getElementById("rp-manager");
const reportUrlEl = document.getElementById("rp-url");
const reportStatsEl = document.getElementById("rp-stats");
const messageEl = document.getElementById("rp-message");
const whatsappEl = document.getElementById("rp-whatsapp");
let managers = [];

function currentManager() {
  return managers.find((item) => String(item.id) === managerEl.value) || null;
}

/* Lien WhatsApp : la discussion du chef de projet si son numero est au format
   international (+225…, 00225…), sinon le choix du contact dans WhatsApp. */
function whatsappLink(phone, text) {
  const brut = (phone || "").replace(/[\s.\-()]/g, "");
  let numero = "";
  if (brut.startsWith("+")) numero = brut.slice(1);
  else if (brut.startsWith("00")) numero = brut.slice(2);
  numero = /^\d{8,15}$/.test(numero) ? numero : "";
  return "https://wa.me/" + numero + "?text=" + encodeURIComponent(text);
}

function refreshWhatsappLink() {
  const manager = currentManager();
  whatsappEl.href = whatsappLink(manager ? manager.phone : "", messageEl.value);
}

function renderReportStats(summary) {
  reportStatsEl.innerHTML =
    statTile(summary.projects, "Projets") +
    statTile(summary.avg_progress + " %", "Avancement moyen") +
    statTile(summary.tasks_done, "Tâches terminées", "stat-done") +
    statTile(summary.anomalies_open, "Anomalies ouvertes", summary.anomalies_critical ? "stat-late" : "") +
    statTile(summary.deliverables_awaited, "Livrables attendus") +
    statTile(summary.at_risk, "À surveiller", summary.at_risk ? "stat-late" : "");
  Pilot.stagger(reportStatsEl.children);
  Pilot.countUp(reportStatsEl);
}

async function loadReport() {
  const manager = currentManager();
  const query = periodQuery();
  if (!manager || !query) return;
  reportUrlEl.value = manager.report_url || "";
  try {
    const data = await callApi("/api/progress-report?manager_id=" + manager.id + "&" + query);
    renderReportStats(data.report.summary);
    messageEl.value = data.message;
  } catch (error) {
    reportStatsEl.innerHTML = "";
    messageEl.value = "";
    toast(error.message, true);
  }
  refreshWhatsappLink();
}

async function loadManagers() {
  try {
    managers = (await callApi("/api/progress-report/managers")).managers;
  } catch (error) {
    toast(error.message, true);
    return;
  }
  if (!managers.length) {
    managerEl.innerHTML = '<option value="">Aucun chef de projet</option>';
    messageEl.value =
      "Aucun chef de projet n'est désigné. Ouvre la fiche d'un projet, « Modifier », et choisis son chef de projet parmi tes contacts.";
    return;
  }
  managerEl.innerHTML = managers
    .map(
      (item) =>
        '<option value="' + item.id + '">' + escapeHtml(item.name) + " — " +
        escapeHtml(item.projects.join(", ")) + "</option>"
    )
    .join("");
  loadReport();
}

managerEl.addEventListener("change", loadReport);
periodEl.addEventListener("change", loadReport);
startEl.addEventListener("change", loadReport);
endEl.addEventListener("change", loadReport);
messageEl.addEventListener("input", refreshWhatsappLink);

document.getElementById("rp-url-save").addEventListener("click", async () => {
  const manager = currentManager();
  if (!manager) return;
  try {
    const data = await callApi("/api/people/" + manager.id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ report_url: reportUrlEl.value }),
    });
    manager.report_url = data.person ? data.person.report_url : reportUrlEl.value;
    toast("Lien enregistré.");
    loadReport();
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("rp-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(messageEl.value);
  } catch (error) {
    // Presse-papiers refuse (contexte non securise) : selection a copier a la main.
    messageEl.select();
    document.execCommand("copy");
  }
  toast("Message copié.");
});

loadManagers();
