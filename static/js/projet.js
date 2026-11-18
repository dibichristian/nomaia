/* NOMAIA — fiche d'un projet : avancement, taches, journal, contacts, agenda.
 *
 * Les textes (titres, commentaires, noms) viennent de l'API, parfois d'un
 * modele de langage : tout passe par escapeHtml.
 */

const {
  toast, callApi, escapeHtml, formatDuration, formatDeadline,
  nestTasks, treeTasks, toggleFold, foldButton, renderTree,
} = window.J;

const rootEl = document.getElementById("pj-root");
const projectId = Number(rootEl.dataset.projectId);

const STATUS_LABELS = { active: "Actif", paused: "En pause", done: "Terminé" };
const PRIORITY_LABELS = { 1: "Critique", 2: "Haute", 3: "Normale", 4: "Basse" };
const SOURCE_LABELS = { claude: "session Claude", chat: "chat NOMAIA" };
const KIND_LABELS = {
  note: "", progress: "avancement", work: "travail",
  decision: "décision", scope: "périmètre", dependency: "dépendance",
};

// Elements affiches dans les panneaux Anomalies et Livrables avant « et N autres ».
const QUALITY_SHOWN = 6;
// Duree de l'entree en scene (cascade et barres) avant de la couper : 1,2 s.
const SETTLE_DELAY = 1200;

const Pilot = window.Pilot;

let fiche = null;
// Etapes depliees dans la liste : conservees d'un rechargement a l'autre.
let openPhases = null;

function statTile(value, label, modifier) {
  return (
    '<div class="stat' + (modifier ? " " + modifier : "") + '">' +
      '<span class="stat-value">' + escapeHtml(value) + "</span>" +
      '<span class="stat-label">' + label + "</span>" +
    "</div>"
  );
}

/* ---------- En-tete et indicateurs ---------- */

function renderHead(project) {
  document.getElementById("pj-title").textContent = project.name;
  document.title = "NOMAIA — " + project.name;
  document.getElementById("pj-dot").style.background = project.color;
  const status = document.getElementById("pj-status");
  status.textContent = STATUS_LABELS[project.status] || project.status;
  status.className = "pj-status s-" + project.status;

  let echeance = "Sans échéance";
  if (project.deadline) {
    echeance = "Échéance " + formatDeadline(project.deadline);
    if (project.status !== "done" && project.days_left !== null) {
      echeance += project.days_left < 0 ? " · dépassée" : " · J-" + project.days_left;
    }
  }
  if (project.go_live) echeance += " · mise en production visée " + formatDeadline(project.go_live);
  // Contexte Odoo (lot 8) : client et version, en tete de la ligne.
  const contexte = [project.client, project.odoo_version ? "Odoo " + project.odoo_version : ""].filter(Boolean);
  document.getElementById("pj-deadline").textContent = contexte.concat(echeance ? [echeance] : []).join(" · ");

  document.getElementById("pj-weather").innerHTML = Pilot.weather(project.weather, true);
  const note = document.getElementById("pj-weather-note");
  note.hidden = !project.weather_note;
  note.textContent = project.weather_note || "";

  const passe = formatDuration(project.tracked_min || 0) || "0 min";
  const ecart = project.estimated_min ? passe + " / " + formatDuration(project.estimated_min) : passe;

  const etapes = project.phases;
  const courante = project.current_phase;
  const jalon = project.next_milestone;
  const rang = courante ? etapes.findIndex((phase) => phase.id === courante.id) + 1 : 0;
  const jalonDate = jalon && jalon.days_left !== null;
  const jalonEnRetard = jalonDate && jalon.days_left < 0;
  let jalonValeur = "—";
  if (jalonDate) {
    if (jalonEnRetard) jalonValeur = "+" + -jalon.days_left + " j";
    else jalonValeur = jalon.days_left === 0 ? "Auj." : "J-" + jalon.days_left;
  }

  document.getElementById("pj-kpis").innerHTML =
    statTile(project.progress_pct + " %", "Avancement", "stat-done") +
    (etapes.length
      ? statTile(courante ? rang + " / " + etapes.length : "✓", "Étape en cours") +
        statTile(jalonValeur, jalonEnRetard ? "Jalon en retard" : "Prochain jalon", jalonEnRetard ? "stat-late" : "")
      : "") +
    statTile(formatDuration(project.remaining_min) || "0 min", "Reste estimé") +
    statTile(ecart, "Passé / estimé") +
    statTile(project.late_count, "En retard", project.late_count ? "stat-late" : "") +
    statTile(project.blocked_count, "Bloquées", project.blocked_count ? "stat-late" : "") +
    statTile(project.waiting_client, "Attente client");
  const kpisEl = document.getElementById("pj-kpis");
  Pilot.stagger(kpisEl.children);
  Pilot.countUp(kpisEl);

  document.getElementById("pj-signals").innerHTML = project.signals
    .map((signal) => '<span class="pj-signal is-' + signal.level + '">' + escapeHtml(signal.label) + "</span>")
    .join("");

  const attach = document.getElementById("pj-attach");
  attach.hidden = !fiche.orphan_count;
  attach.textContent = "Rattacher " + fiche.orphan_count + " tâche(s) qui le citent";
}

/* ---------- Taches ---------- */

function taskRow(task, depth) {
  const meta = ["P" + task.priority + " · " + PRIORITY_LABELS[task.priority]];
  if (task.deadline) meta.push("échéance " + formatDeadline(task.deadline));
  meta.push(formatDuration(task.estimate_min));
  if (task.tracked_min) meta.push(formatDuration(task.tracked_min) + " passées");
  if (task.module) meta.push(escapeHtml(task.module));
  // Imbriquee sous son parent, la mention serait redondante.
  if (task.parent_title && !depth) meta.push("étape de « " + escapeHtml(task.parent_title) + " »");
  if (task.person_name) meta.push("pour " + escapeHtml(task.person_name));
  if (task.status === "done" && task.done_at) meta.push("finie " + formatDeadline(task.done_at.slice(0, 10)));

  const badge =
    Pilot.kindBadge(task) +
    (task.state !== "todo" ? Pilot.stateBadge(task.state) : "") +
    Pilot.sideBadge(task.side) +
    (task.source === "claude" ? '<span class="pj-badge" title="Ouverte par une session Claude">Claude</span>' : "");
  const progress = task.status !== "done" && task.progress_pct
    ? '<div class="pj-task-progress" title="Avancement déclaré"><div class="load-bar"><div class="load-fill" ' +
      Pilot.fillStyle(task.progress_pct) + '></div></div><span>' + task.progress_pct + " %</span></div>"
    : "";

  const children = task.children || [];
  const holder = task.status === "done" && children.some((child) => child.status !== "done")
    ? " is-holder"
    : "";

  return (
    '<div class="slot compact pj-task' + (task.status === "done" ? " is-done" : "") +
      (depth ? " is-subtask" : "") + holder +
      '" data-priority="' + task.priority + '" data-id="' + task.id + '" style="--depth:' + (depth || 0) + '">' +
      '<input type="checkbox" class="task-check" data-action="toggle"' +
        (task.status === "done" ? " checked" : "") + ' title="Terminée">' +
      '<div class="slot-body">' +
        '<button type="button" class="slot-title as-link" data-action="detail" title="Détailler">' +
          escapeHtml(task.title) + "</button>" + badge +
        '<span class="slot-meta">' + meta.join(" · ") + "</span>" +
        (children.length ? '<div class="tree-fold-row">' + foldButton(task) + "</div>" : "") +
        progress +
      "</div>" +
    "</div>"
  );
}

/* Arbres de taches rendus avec leurs sous-taches. */
function taskTree(roots) {
  return renderTree(roots, taskRow);
}

function taskSection(title, roots, extra) {
  if (!roots.length && !extra) return "";
  return (
    '<h3 class="window-title">' + title + ' <span class="group-count">' + roots.length + "</span></h3>" +
    taskTree(roots) + (extra || "")
  );
}

/* Range chaque arbre dans une section : en retard si une de ses taches
 * ouvertes l'est, a faire s'il en reste une ouverte, termine sinon. */
function sectionTrees(tasks) {
  const lateIds = new Set(tasks.late.map((task) => task.id));
  const sections = { late: [], open: [], done: [] };
  nestTasks(tasks.late.concat(tasks.open, tasks.done)).forEach((root) => {
    const ouvertes = treeTasks(root).filter((task) => task.status !== "done");
    const cle = ouvertes.some((task) => lateIds.has(task.id)) ? "late" : ouvertes.length ? "open" : "done";
    sections[cle].push(root);
  });
  return sections;
}

function renderTasks(tasks) {
  if (fiche.project.phases.length) {
    renderPhaseTasks(fiche.project.phases, tasks.unphased);
    return;
  }
  const arbres = sectionTrees(tasks);
  let html = taskSection("En retard", arbres.late);
  html += taskSection(
    "À faire",
    arbres.open,
    arbres.open.length ? "" : '<p class="empty">Aucune tâche ouverte.</p>'
  );
  const plus = tasks.done_total > tasks.done.length
    ? '<p class="plan-hint">' + (tasks.done_total - tasks.done.length) + " plus ancienne(s) non affichée(s).</p>"
    : "";
  html += taskSection("Terminées", arbres.done, plus);
  document.getElementById("pj-tasks").innerHTML = html;
}

/* ---------- Etapes du projet ---------- */

function milestoneText(label, jalon, withDelay) {
  if (!jalon) return "";
  let texte = '<span class="pl-ms-label">' + label + "</span> <strong>" + escapeHtml(jalon.name) + "</strong>";
  if (jalon.date) {
    texte += " · " + formatDeadline(jalon.date);
    const delai = withDelay ? Pilot.daysLabel(jalon.days_left) : "";
    if (delai) texte += ' <span class="' + (jalon.days_left < 0 ? "is-late" : "") + '">(' + delai + ")</span>";
  }
  return '<span class="pl-ms">' + texte + "</span>";
}

function renderPhaseSelect(project) {
  const select = document.getElementById("pj-task-phase");
  const choix = select.value;
  select.hidden = !project.phases.length;
  select.innerHTML = '<option value="">Sans étape</option>' + project.phases
    .map((phase) => '<option value="' + phase.id + '">' + phase.sequence + ". " + escapeHtml(phase.name) + "</option>")
    .join("");
  // Par defaut, une nouvelle tache va dans l'etape en cours.
  select.value = choix || (project.current_phase ? String(project.current_phase.id) : "");
}

function renderPhasesSummary(project) {
  const etapes = project.phases;
  const box = document.getElementById("pl-phases-summary");
  document.getElementById("pl-template").hidden = Boolean(etapes.length);
  renderPhaseSelect(project);

  if (!etapes.length) {
    box.innerHTML =
      '<p class="pj-faint">Aucune étape. Applique le gabarit (11 étapes, du recueil du besoin à la ' +
      "maintenance) ou ajoute tes propres étapes : l'avancement du projet sera " +
      "alors calculé étape par étape, pondéré par leur poids.</p>";
    return;
  }

  const courante = project.current_phase;
  box.innerHTML =
    Pilot.phaseStrip(etapes, courante && courante.id, true) +
    '<div class="pl-milestones">' +
      milestoneText("Dernier jalon atteint", project.last_milestone, false) +
      (courante
        ? '<span class="pl-ms"><span class="pl-ms-label">Étape en cours</span> <strong>' +
            escapeHtml(courante.name) + "</strong> · " + Pilot.PHASE_LABELS[courante.status] + " · " +
            courante.progress_pct + " %</span>"
        : '<span class="pl-ms"><strong>Toutes les étapes sont terminées.</strong></span>') +
      milestoneText("Prochain jalon", project.next_milestone, true) +
    "</div>";
}

/* Etapes depliees a l'ouverture : l'etape en cours et celles qui posent
 * probleme (bloquees, taches en retard ou bloquees). */
function defaultOpenPhases(project) {
  const ouvertes = new Set(["none"]);
  project.phases.forEach((phase) => {
    if (phase.status === "blocked" || phase.states.late || phase.states.blocked) ouvertes.add(phase.id);
  });
  if (project.current_phase) ouvertes.add(project.current_phase.id);
  return ouvertes;
}

function phaseDate(phase) {
  if (phase.status === "done") {
    return phase.done_date ? "terminée le " + formatDeadline(phase.done_date) : "";
  }
  if (!phase.planned_date) return "";
  return "prévue le " + formatDeadline(phase.planned_date) +
    (phase.days_left !== null ? " · " + Pilot.daysLabel(phase.days_left) : "");
}

function phaseBlock(phase) {
  const enRetard = phase.status !== "done" && phase.days_left !== null && phase.days_left < 0;
  const corps = phase.tasks.length
    ? taskTree(nestTasks(phase.tasks))
    : '<p class="pj-faint pl-phase-empty">Aucune tâche dans cette étape.</p>';

  return (
    '<details class="pl-phase s-' + phase.status + '" data-phase-id="' + phase.id + '"' +
      (openPhases.has(phase.id) ? " open" : "") + ">" +
      "<summary>" +
        '<span class="pl-phase-seq">' + phase.sequence + "</span>" +
        '<span class="pl-phase-name">' + escapeHtml(phase.name) +
          "<small>" + (phase.stage ? escapeHtml(phase.stage) + " · " : "") + "poids " + phase.weight +
            (phaseDate(phase)
              ? ' · <span class="pl-phase-date' + (enRetard ? " is-late" : "") + '">' + phaseDate(phase) + "</span>"
              : "") +
          "</small>" +
        "</span>" +
        Pilot.phaseBadge(phase.status) +
        '<span class="pl-phase-progress"><span class="load-bar"><span class="load-fill" ' +
          Pilot.fillStyle(phase.progress_pct) + '></span></span><span>' + phase.progress_pct + " %</span></span>" +
        '<span class="pl-phase-count" title="Tâches terminées / total">' +
          phase.task_done + "/" + phase.task_total + "</span>" +
        (phase.status !== "done"
          ? '<button type="button" class="icon-btn" data-phase-done="' + phase.id +
            '" title="Terminer l’étape (et les précédentes encore ouvertes)">✓</button>'
          : "") +
        '<button type="button" class="icon-btn" data-phase-edit="' + phase.id + '" title="Modifier l’étape">✎</button>' +
      "</summary>" +
      '<div class="pl-phase-body">' + corps + "</div>" +
    "</details>"
  );
}

function renderPhaseTasks(phases, unphased) {
  let html = phases.map(phaseBlock).join("");
  if (unphased.length) {
    html +=
      '<details class="pl-phase s-none" data-phase-id="none"' + (openPhases.has("none") ? " open" : "") + ">" +
        '<summary><span class="pl-phase-seq">–</span><span class="pl-phase-name">Sans étape' +
          "<small>À ranger depuis le détail de la tâche</small></span>" +
          '<span class="pl-phase-count">' + unphased.length + "</span></summary>" +
        '<div class="pl-phase-body">' + taskTree(nestTasks(unphased)) + "</div>" +
      "</details>";
  }
  document.getElementById("pj-tasks").innerHTML = html;
}

function renderDonut(project) {
  document.getElementById("pl-donut").innerHTML = Pilot.donut(project.task_states, 132);
}

function renderWatch(watch) {
  const box = document.getElementById("pl-watch");
  if (!watch.length) {
    box.innerHTML = '<p class="pj-faint">Rien en retard, bloqué ni en attente du client.</p>';
    return;
  }
  box.innerHTML =
    '<ul class="pl-watch">' +
    watch
      .map(
        (task) =>
          '<li data-id="' + task.id + '">' +
            '<button type="button" class="as-link" data-action="detail">' + escapeHtml(task.title) + "</button>" +
            '<span class="pl-watch-tags">' +
              (task.state === "late" || task.state === "blocked" ? Pilot.stateBadge(task.state) : "") +
              Pilot.sideBadge(task.side) +
              (task.deadline ? '<span class="pj-faint">' + formatDeadline(task.deadline) + "</span>" : "") +
            "</span>" +
          "</li>"
      )
      .join("") +
    "</ul>";
}

/* ---------- Anomalies, livrables, registre ---------- */

function qualityItem(task, tags) {
  return (
    '<li data-id="' + task.id + '"' + (task.status === "done" ? ' class="is-done"' : "") + ">" +
      '<button type="button" class="as-link" data-action="detail">' + escapeHtml(task.title) + "</button>" +
      '<span class="pl-watch-tags">' + tags + "</span>" +
    "</li>"
  );
}

function moreLine(count, label) {
  return count > 0 ? '<p class="pj-faint pl-more">et ' + count + " " + label + "</p>" : "";
}

function renderAnomalies(rows, counts) {
  const box = document.getElementById("pl-anomalies");
  if (!rows.length) {
    box.innerHTML =
      '<p class="pj-faint">Aucune anomalie. Choisis « Anomalie » dans la saisie rapide pour en ouvrir une.</p>';
    return;
  }
  const ouvertes = rows.filter((task) => task.status !== "done");
  const synthese = ["critical", "major", "minor"]
    .filter((severity) => counts[severity])
    .map((severity) => Pilot.severityBadge(severity) + ' <span class="pl-count">' + counts[severity] + "</span>")
    .join(" ");
  box.innerHTML =
    '<div class="pl-quality-head">' +
      (synthese || '<span class="pj-faint">Aucune anomalie ouverte.</span>') +
      '<span class="pj-faint pl-quality-done">' + counts.fixed + " corrigée(s)</span>" +
    "</div>" +
    (ouvertes.length
      ? '<ul class="pl-watch">' +
        ouvertes.slice(0, QUALITY_SHOWN).map((task) =>
          qualityItem(
            task,
            Pilot.severityBadge(task.severity || "major") +
              (task.module ? '<span class="pj-faint">' + escapeHtml(task.module) + "</span>" : "") +
              (task.state !== "todo" ? Pilot.stateBadge(task.state) : "")
          )
        ).join("") +
        "</ul>" + moreLine(ouvertes.length - QUALITY_SHOWN, "autre(s) ouverte(s)")
      : "");
}

function renderDeliverables(rows, counts) {
  const box = document.getElementById("pl-deliverables");
  const total = counts.awaited + counts.received;
  if (!total) {
    box.innerHTML =
      '<p class="pj-faint">Aucun livrable suivi. Choisis « Livrable client » dans la saisie rapide : ' +
      "fichier de données, PV à signer…</p>";
    return;
  }
  const attendus = rows.filter((task) => task.status !== "done");
  const part = Math.round((counts.received / total) * 100);
  box.innerHTML =
    '<div class="pl-quality-head">' +
      "<strong>" + counts.received + " / " + total + "</strong>&nbsp;reçu(s)" +
      '<span class="load-bar pl-quality-bar" title="Livrables reçus"><span class="load-fill" ' +
        Pilot.fillStyle(part) + '></span></span>' +
    "</div>" +
    (attendus.length
      ? '<ul class="pl-watch">' +
        attendus.slice(0, QUALITY_SHOWN).map((task) =>
          qualityItem(
            task,
            Pilot.sideBadge(task.side) +
              (task.state === "late" || task.state === "blocked" ? Pilot.stateBadge(task.state) : "") +
              (task.progress_pct ? '<span class="pj-faint">' + task.progress_pct + " % reçu</span>" : "") +
              (task.deadline ? '<span class="pj-faint">' + formatDeadline(task.deadline) + "</span>" : "")
          )
        ).join("") +
        "</ul>" + moreLine(attendus.length - QUALITY_SHOWN, "autre(s) attendu(s)")
      : '<p class="pj-faint">Tout est reçu.</p>');
}

function renderRegister(entries) {
  const box = document.getElementById("pl-register");
  if (!entries.length) {
    box.innerHTML =
      '<p class="pj-faint">Aucune entrée. Dans « Commentaires et journal », choisis Décision, ' +
      "Changement de périmètre ou Dépendance.</p>";
    return;
  }
  const aRegler = entries.filter((entry) => entry.status === "open").length;
  box.innerHTML =
    (aRegler ? '<div class="pl-quality-head"><strong>' + aRegler + "</strong>&nbsp;point(s) à régler</div>" : "") +
    '<ul class="pl-register">' +
    entries.map((entry) =>
      '<li data-record="' + entry.id + '">' +
        '<div class="pl-register-head">' + Pilot.recordBadge(entry) +
          '<span class="work-log-when">' + formatDeadline(entry.created_at.slice(0, 10)) + "</span>" +
          '<button type="button" class="icon-btn danger" data-record-delete="' + entry.id + '" title="Supprimer">✕</button>' +
        "</div>" +
        '<p class="pj-journal-text">' + escapeHtml(entry.content) + "</p>" +
        (entry.task_id
          ? '<button type="button" class="as-link pl-register-task" data-task="' + entry.task_id + '">Tâche de suivi</button>'
          : "") +
      "</li>"
    ).join("") +
    "</ul>";
}

/* ---------- Colonne laterale ---------- */

function renderJournal(entries) {
  const list = document.getElementById("pj-journal");
  if (!entries.length) {
    list.innerHTML = '<li class="pj-faint">Aucun commentaire. Les notes des tâches et des sessions Claude apparaissent aussi ici.</li>';
    return;
  }
  list.innerHTML = entries
    .map((entry) => {
      const origine = [
        formatDeadline(entry.created_at.slice(0, 10)) + " " + entry.created_at.slice(11, 16),
        entry.source ? SOURCE_LABELS[entry.source] || entry.source : "",
        KIND_LABELS[entry.kind] || "",
      ].filter(Boolean).join(" · ");
      const cible = entry.task_title
        ? '<span class="pj-journal-task">' + escapeHtml(entry.task_title) + "</span>"
        : "";
      const supprimer = entry.task_id
        ? ""
        : '<button type="button" class="icon-btn danger" data-delete="' + entry.id + '" title="Supprimer">✕</button>';
      return (
        "<li>" +
          '<div class="pj-journal-head"><span class="work-log-when">' + escapeHtml(origine) + "</span>" +
            cible + supprimer + "</div>" +
          '<p class="pj-journal-text">' + escapeHtml(entry.content) +
            (entry.minutes ? ' <span class="work-log-min">' + formatDuration(entry.minutes) + "</span>" : "") +
          "</p>" +
        "</li>"
      );
    })
    .join("");
}

function renderActivity(weeks) {
  const box = document.getElementById("pj-activity");
  if (!weeks.length) {
    box.innerHTML = '<p class="pj-faint">Pas encore d\'activité mesurée.</p>';
    return;
  }
  const max = Math.max(...weeks.map((week) => week.minutes), 1);
  box.innerHTML = weeks
    .map(
      (week) =>
        '<div class="bar-row">' +
          '<span class="bar-label">' + escapeHtml(formatDeadline(week.week)) + "</span>" +
          '<div class="bar-track"><div class="bar-fill" ' +
            Pilot.fillStyle((week.minutes / max) * 100) + '></div></div>' +
          '<span class="bar-value">' + (formatDuration(week.minutes) || "0 min") + " · " +
            week.done + " ✓</span>" +
        "</div>"
    )
    .join("");
}

function renderPeople(people, waiting) {
  const box = document.getElementById("pj-people");
  if (!people.length) {
    box.innerHTML = '<p class="pj-faint">Aucun interlocuteur rattaché. <a class="link" href="/contacts">Contacts</a></p>';
    return;
  }
  const attentes = {};
  waiting.forEach((entry) => {
    (attentes[entry.person_id] = attentes[entry.person_id] || []).push(entry);
  });
  box.innerHTML = people
    .map((person) => {
      const lignes = (attentes[person.id] || [])
        .map(
          (entry) =>
            '<li class="' + (entry.overdue ? "is-late" : "") + '">On attend : ' + escapeHtml(entry.subject) +
            " (" + entry.days_open + " j)</li>"
        )
        .join("");
      return (
        '<div class="pj-person"><strong>' + escapeHtml(person.name) + "</strong>" +
          (person.role ? ' <span class="pj-faint">' + escapeHtml(person.role) + "</span>" : "") +
          (person.owed_count ? ' <span class="pj-faint">· je lui dois ' + person.owed_count + "</span>" : "") +
          (lignes ? '<ul class="pj-waiting">' + lignes + "</ul>" : "") +
        "</div>"
      );
    })
    .join("");
}

function renderEvents(events) {
  const box = document.getElementById("pj-events");
  if (!events.length) {
    box.innerHTML = '<p class="pj-faint">Rien de prévu dans les 30 prochains jours.</p>';
    return;
  }
  box.innerHTML = events
    .slice(0, 8)
    .map(
      (event) =>
        '<div class="pj-event"><span class="work-log-when">' +
          escapeHtml(formatDeadline(event.starts_at.slice(0, 10)) + " " + event.starts_at.slice(11, 16)) +
          "</span> " + escapeHtml(event.title) + "</div>"
    )
    .join("");
}

/* ---------- Chargement ---------- */

/* Cascade des listes : la page est rechargee apres chaque action, seule la
 * premiere entree en scene est animee (.is-settled coupe les suivantes). */
function staggerLists() {
  Pilot.stagger(document.getElementById("pj-tasks").children);
  ["pl-watch", "pl-anomalies", "pl-deliverables", "pl-register"].forEach((id) => {
    Pilot.stagger(document.querySelectorAll("#" + id + " li"));
  });
  Pilot.stagger(document.getElementById("pj-journal").children);
}

async function reload() {
  try {
    if (fiche) rootEl.classList.add("is-settled");
    fiche = await callApi("/api/projects/" + projectId + "/detail");
    if (openPhases === null) openPhases = defaultOpenPhases(fiche.project);
    renderHead(fiche.project);
    renderPhasesSummary(fiche.project);
    renderTasks(fiche.tasks);
    renderDonut(fiche.project);
    renderWatch(fiche.tasks.watch);
    renderAnomalies(fiche.tasks.anomalies, fiche.project.anomalies);
    renderDeliverables(fiche.tasks.deliverables, fiche.project.deliverables);
    renderRegister(fiche.register);
    renderJournal(fiche.journal);
    renderActivity(fiche.activity);
    renderPeople(fiche.people, fiche.waiting);
    renderEvents(fiche.events);
    staggerLists();
    // Entree terminee : un repli d'etape ou une action ne rejoue rien.
    window.setTimeout(() => rootEl.classList.add("is-settled"), SETTLE_DELAY);
  } catch (error) {
    document.getElementById("pj-tasks").innerHTML = '<p class="empty">' + escapeHtml(error.message) + "</p>";
  }
}

/* ---------- Actions ---------- */

function phaseSavedMessage(phase, fallback) {
  return phase && phase.cascaded
    ? "Étape terminée, ainsi que " + phase.cascaded + " étape(s) précédente(s)."
    : fallback;
}

async function finishPhase(phaseId) {
  const phase = fiche.project.phases.find((item) => item.id === phaseId);
  const ouvertes = fiche.project.phases.filter(
    (item) => item.sequence < phase.sequence && item.stored_status !== "done"
  ).length;
  const question = "Terminer « " + phase.name + " »" +
    (ouvertes ? " et les " + ouvertes + " étape(s) précédente(s) encore ouvertes" : "") + " ?";
  if (!confirm(question)) return;
  const resultat = await callApi("/api/projects/" + projectId + "/phases/" + phaseId, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "done" }),
  });
  await reload();
  toast(phaseSavedMessage(resultat, "Étape terminée."));
}

document.getElementById("pj-tasks").addEventListener("click", async (event) => {
  const fin = event.target.closest("[data-phase-done]");
  if (fin) {
    // Bouton du <summary> : ne pas replier l'etape.
    event.preventDefault();
    try {
      await finishPhase(Number(fin.dataset.phaseDone));
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  const edit = event.target.closest("[data-phase-edit]");
  if (edit) {
    // Le bouton vit dans le <summary> : sans cela, il replierait l'etape.
    event.preventDefault();
    openPhaseModal(Number(edit.dataset.phaseEdit));
    return;
  }
  const fold = event.target.closest("[data-fold]");
  if (fold) {
    toggleFold(fold.dataset.fold);
    renderTasks(fiche.tasks);
    return;
  }

  const trigger = event.target.closest("[data-action]");
  if (!trigger) return;
  const taskId = trigger.closest("[data-id]")?.dataset.id;
  if (!taskId) return;

  try {
    if (trigger.dataset.action === "detail") {
      await window.TaskDetail.open(taskId, { onChange: reload });
    } else if (trigger.dataset.action === "toggle") {
      await callApi("/api/tasks/" + taskId + "/toggle", { method: "POST" });
      await reload();
    }
  } catch (error) {
    toast(error.message, true);
  }
});

const kindEl = document.getElementById("pj-task-kind");
kindEl.addEventListener("change", () => {
  document.getElementById("pj-task-severity").hidden = kindEl.value !== "anomaly";
});

document.getElementById("pj-task-add").addEventListener("submit", async (event) => {
  event.preventDefault();
  const titleEl = document.getElementById("pj-task-title");
  try {
    await callApi("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: titleEl.value,
        project_id: projectId,
        estimate_min: document.getElementById("pj-task-estimate").value,
        deadline: document.getElementById("pj-task-deadline").value,
        phase_id: document.getElementById("pj-task-phase").value || null,
        kind: kindEl.value,
        severity: kindEl.value === "anomaly" ? document.getElementById("pj-task-severity").value : null,
      }),
    });
    titleEl.value = "";
    await reload();
    toast({ anomaly: "Anomalie ouverte.", deliverable: "Livrable ajouté." }[kindEl.value] || "Tâche ajoutée au projet.");
  } catch (error) {
    toast(error.message, true);
  }
});

const commentKindEl = document.getElementById("pj-comment-kind");
const followWrap = document.getElementById("pj-comment-follow-wrap");
// Un changement de perimetre ou une dependance se regle : proposer une tache de suivi.
commentKindEl.addEventListener("change", () => {
  followWrap.hidden = !["scope", "dependency"].includes(commentKindEl.value);
});

document.getElementById("pj-comment-add").addEventListener("submit", async (event) => {
  event.preventDefault();
  const zone = document.getElementById("pj-comment");
  if (!zone.value.trim()) {
    zone.focus();
    return;
  }
  try {
    await callApi("/api/projects/" + projectId + "/journal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: zone.value,
        kind: commentKindEl.value,
        follow_up: !followWrap.hidden && document.getElementById("pj-comment-follow").checked,
      }),
    });
    zone.value = "";
    commentKindEl.value = "note";
    followWrap.hidden = true;
    await reload();
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("pj-journal").addEventListener("click", async (event) => {
  const bouton = event.target.closest("[data-delete]");
  if (!bouton || !confirm("Supprimer ce commentaire ?")) return;
  try {
    await callApi("/api/projects/" + projectId + "/journal/" + bouton.dataset.delete, { method: "DELETE" });
    await reload();
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("pj-attach").addEventListener("click", async () => {
  try {
    const result = await callApi("/api/projects/" + projectId + "/attach", { method: "POST" });
    await reload();
    toast(result.attached + " tâche(s) rattachée(s).");
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("pj-delete").addEventListener("click", async () => {
  if (!confirm("Supprimer ce projet ? Ses tâches sont conservées, simplement détachées.")) return;
  try {
    await callApi("/api/projects/" + projectId, { method: "DELETE" });
    window.location.href = "/projets";
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Fusion d'un projet en double ---------- */

const mergeModal = document.getElementById("pj-merge-modal");
const mergeError = document.getElementById("pj-merge-error");

/* Noms proches d'abord (l'un contient l'autre) : c'est le doublon le plus
 * probable, celui cree par l'import Odoo sous son nom complet. */
async function fillMergeOptions() {
  const nom = fiche.project.name.toLowerCase();
  const proche = (autre) => {
    const texte = autre.name.toLowerCase();
    return texte.includes(nom) || nom.includes(texte);
  };
  const data = await callApi("/api/projects");
  const autres = data.projects
    .filter((autre) => autre.id !== projectId)
    .sort((a, b) => Number(proche(b)) - Number(proche(a)) || a.name.localeCompare(b.name));
  document.getElementById("pj-mg-source").innerHTML = autres.map((autre) =>
    '<option value="' + autre.id + '">' + escapeHtml(autre.name) + " — " +
      autre.open_count + " ouverte(s), " + autre.done_count + " terminée(s)</option>"
  ).join("");
  return autres.length;
}

document.getElementById("pj-merge").addEventListener("click", async () => {
  if (!fiche) return;
  mergeError.hidden = true;
  document.getElementById("pj-mg-target").textContent = fiche.project.name;
  try {
    if (!(await fillMergeOptions())) {
      toast("Aucun autre projet à fusionner.", true);
      return;
    }
    mergeModal.showModal();
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("pj-mg-cancel").addEventListener("click", () => mergeModal.close());

document.getElementById("pj-merge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const select = document.getElementById("pj-mg-source");
  const nom = select.selectedOptions[0]?.textContent.split(" — ")[0] || "ce projet";
  if (!confirm("Fusionner « " + nom + " » dans « " + fiche.project.name + " » ? Il sera supprimé.")) return;
  try {
    await callApi("/api/projects/" + projectId + "/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ merge_id: Number(select.value) }),
    });
    mergeModal.close();
    toast("« " + nom + " » fusionné ici.");
    await reload();
  } catch (error) {
    mergeError.textContent = error.message;
    mergeError.hidden = false;
  }
});

/* ---------- Modification du projet ---------- */

const editModal = document.getElementById("pj-edit-modal");
const editError = document.getElementById("pj-edit-error");

/* Liste des contacts pour le choix du chef de projet, chargee a l'ouverture :
   un contact cree entre-temps doit y figurer. */
async function fillManagerOptions(selectedId) {
  const select = document.getElementById("pj-ed-manager");
  try {
    const data = await callApi("/api/people");
    select.innerHTML =
      '<option value="">Aucun</option>' +
      data.people
        .map(
          (person) =>
            '<option value="' + person.id + '">' + escapeHtml(person.name) +
            (person.role ? " — " + escapeHtml(person.role) : "") + "</option>"
        )
        .join("");
  } catch (error) {
    // Liste indisponible : garder le choix actuel plutot que l'effacer a l'enregistrement.
    select.innerHTML =
      '<option value="">Aucun</option>' +
      (selectedId ? '<option value="' + selectedId + '">Chef de projet actuel</option>' : "");
    toast(error.message, true);
  }
  select.value = selectedId ? String(selectedId) : "";
}

document.getElementById("pj-edit").addEventListener("click", async () => {
  if (!fiche) return;
  const project = fiche.project;
  editError.hidden = true;
  // Attendu avant l'ouverture : une liste vide enregistrerait « Aucun ».
  await fillManagerOptions(project.manager_id);
  document.getElementById("pj-ed-name").value = project.name;
  document.getElementById("pj-ed-color").value = project.color;
  document.getElementById("pj-ed-deadline").value = project.deadline || "";
  document.getElementById("pj-ed-client").value = project.client || "";
  document.getElementById("pj-ed-odoo").value = project.odoo_version || "";
  document.getElementById("pj-ed-space").value = project.space || "pro";
  document.getElementById("pj-ed-status").value = project.status;
  document.getElementById("pj-ed-go-live").value = project.go_live || "";
  document.getElementById("pj-ed-weather").value = project.weather || "";
  document.getElementById("pj-ed-weather-note").value = project.weather_note || "";
  editModal.showModal();
});

document.getElementById("pj-ed-cancel").addEventListener("click", () => editModal.close());

document.getElementById("pj-edit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await callApi("/api/projects/" + projectId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: document.getElementById("pj-ed-name").value,
        color: document.getElementById("pj-ed-color").value,
        deadline: document.getElementById("pj-ed-deadline").value,
        client: document.getElementById("pj-ed-client").value,
        odoo_version: document.getElementById("pj-ed-odoo").value,
        space: document.getElementById("pj-ed-space").value,
        status: document.getElementById("pj-ed-status").value,
        go_live: document.getElementById("pj-ed-go-live").value,
        weather: document.getElementById("pj-ed-weather").value,
        weather_note: document.getElementById("pj-ed-weather-note").value,
        manager_id: document.getElementById("pj-ed-manager").value,
      }),
    });
    editModal.close();
    await reload();
    toast("Projet mis à jour.");
  } catch (error) {
    editError.textContent = error.message;
    editError.hidden = false;
  }
});

/* ---------- Etapes : edition ---------- */

const phaseModal = document.getElementById("pl-phase-modal");
const phaseError = document.getElementById("pl-phase-error");
let editedPhaseId = null;

function openPhaseModal(phaseId) {
  const phase = phaseId ? fiche.project.phases.find((item) => item.id === phaseId) : null;
  editedPhaseId = phase ? phase.id : null;
  phaseError.hidden = true;
  document.getElementById("pl-phase-title").textContent = phase ? "Modifier l'étape" : "Nouvelle étape";
  document.getElementById("pl-ph-name").value = phase ? phase.name : "";
  document.getElementById("pl-ph-stage").value = phase ? phase.stage || "" : "";
  document.getElementById("pl-ph-weight").value = phase ? phase.weight : 0;
  document.getElementById("pl-ph-status").value = phase ? phase.stored_status : "todo";
  // Une etape qui a des taches est mesuree par elles : saisie manuelle inutile.
  const calcule = Boolean(phase && phase.has_tasks);
  const progress = document.getElementById("pl-ph-progress");
  progress.value = phase ? phase.manual_progress : 0;
  progress.disabled = calcule;
  document.getElementById("pl-ph-progress-hint").hidden = !calcule;
  document.getElementById("pl-ph-planned").value = phase ? phase.planned_date || "" : "";
  document.getElementById("pl-ph-done").value = phase ? phase.done_date || "" : "";
  document.getElementById("pl-ph-delete").hidden = !phase;
  phaseModal.showModal();
}

document.getElementById("pl-phase-new").addEventListener("click", () => openPhaseModal(null));
document.getElementById("pl-ph-cancel").addEventListener("click", () => phaseModal.close());

document.getElementById("pl-phases-summary").addEventListener("click", (event) => {
  const pastille = event.target.closest("[data-phase]");
  if (!pastille) return;
  const id = Number(pastille.dataset.phase);
  openPhases.add(id);
  renderTasks(fiche.tasks);
  const bloc = document.querySelector('.pl-phase[data-phase-id="' + id + '"]');
  if (bloc) bloc.scrollIntoView({ behavior: "smooth", block: "start" });
});

// « toggle » ne remonte pas : ecoute en phase de capture.
document.getElementById("pj-tasks").addEventListener(
  "toggle",
  (event) => {
    const bloc = event.target.closest(".pl-phase");
    if (!bloc || !openPhases) return;
    const cle = bloc.dataset.phaseId === "none" ? "none" : Number(bloc.dataset.phaseId);
    if (bloc.open) openPhases.add(cle);
    else openPhases.delete(cle);
  },
  true
);

document.getElementById("pl-phase-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    name: document.getElementById("pl-ph-name").value,
    stage: document.getElementById("pl-ph-stage").value,
    weight: document.getElementById("pl-ph-weight").value,
    status: document.getElementById("pl-ph-status").value,
    planned_date: document.getElementById("pl-ph-planned").value,
    done_date: document.getElementById("pl-ph-done").value,
  };
  const progress = document.getElementById("pl-ph-progress");
  if (!progress.disabled) payload.progress_pct = progress.value;
  const base = "/api/projects/" + projectId + "/phases";
  try {
    const resultat = await callApi(editedPhaseId ? base + "/" + editedPhaseId : base, {
      method: editedPhaseId ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    phaseModal.close();
    await reload();
    toast(phaseSavedMessage(resultat, editedPhaseId ? "Étape mise à jour." : "Étape ajoutée."));
  } catch (error) {
    phaseError.textContent = error.message;
    phaseError.hidden = false;
  }
});

document.getElementById("pl-ph-delete").addEventListener("click", async () => {
  if (!editedPhaseId || !confirm("Supprimer cette étape ? Ses tâches restent dans le projet, sans étape.")) return;
  try {
    await callApi("/api/projects/" + projectId + "/phases/" + editedPhaseId, { method: "DELETE" });
    phaseModal.close();
    await reload();
  } catch (error) {
    phaseError.textContent = error.message;
    phaseError.hidden = false;
  }
});

document.getElementById("pl-template").addEventListener("click", async () => {
  try {
    await callApi("/api/projects/" + projectId + "/phases/template", { method: "POST" });
    openPhases = null;
    await reload();
    toast("Étapes du gabarit créées. Renseigne leurs statuts et leurs dates.");
  } catch (error) {
    toast(error.message, true);
  }
});

["pl-watch", "pl-anomalies", "pl-deliverables"].forEach((id) => {
  document.getElementById(id).addEventListener("click", async (event) => {
    const trigger = event.target.closest('[data-action="detail"]');
    if (!trigger) return;
    try {
      await window.TaskDetail.open(trigger.closest("[data-id]").dataset.id, { onChange: reload });
    } catch (error) {
      toast(error.message, true);
    }
  });
});

document.getElementById("pl-register").addEventListener("click", async (event) => {
  const lien = event.target.closest("[data-task]");
  const supprimer = event.target.closest("[data-record-delete]");
  try {
    if (lien) {
      await window.TaskDetail.open(lien.dataset.task, { onChange: reload });
    } else if (supprimer && confirm("Supprimer cette entrée du registre ? Sa tâche de suivi est conservée.")) {
      await callApi("/api/projects/" + projectId + "/journal/" + supprimer.dataset.recordDelete, { method: "DELETE" });
      await reload();
    }
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

reload();

// Rechargement a chaud : etapes depliees gardees, aucune entree rejouee (.is-settled).
J.live.onChange(reload);
