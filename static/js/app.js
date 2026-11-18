/* NOMAIA — logique du tableau de bord (vanilla JS, aucune dependance). */

const API = "/api/tasks";

const GROUPS = [
  { key: "late", label: "En retard" },
  { key: "today", label: "Aujourd'hui" },
  { key: "soon", label: "À venir" },
  { key: "someday", label: "Sans échéance" },
  { key: "done", label: "Terminées" },
];

const PRIORITY_LABELS = { 1: "Critique", 2: "Haute", 3: "Normale", 4: "Basse" };
const ENERGY_LABELS = { low: "Énergie faible", med: "Énergie moyenne", high: "Énergie forte" };

const board = document.getElementById("board");
const modal = document.getElementById("edit-modal");
const filterProject = document.getElementById("filter-project");
const {
  toast, callApi, escapeHtml, formatDuration, formatDeadline,
  nestTasks, treeTasks, toggleFold, foldButton, renderTree,
} = window.J;

// Rang d'urgence des groupes, du plus pressant au moins pressant.
const GROUP_RANK = Object.fromEntries(GROUPS.map((group, index) => [group.key, index]));

// Derniere reponse de l'API : replier une branche re-rend sans recharger.
let lastPayload = null;

// Apparition en cascade des lignes : au chargement et au changement de filtre
// seulement. Apres une action, la liste se redessine sans rejouer l'entree.
let animateRows = true;
let rowIndex = 0;

// Tache que l'on vient de cocher : sa coche apparait avec un petit rebond.
let poppedId = null;

/* ---------- Rendu ---------- */

function taskRow(task, depth) {
  const meta = [];
  meta.push('<span class="tag">P' + task.priority + " · " + PRIORITY_LABELS[task.priority] + "</span>");
  meta.push("<span>" + formatDuration(task.estimate_min) + "</span>");
  if (task.deadline) {
    const cls = task.bucket === "late" ? "tag tag-late" : "tag";
    meta.push('<span class="' + cls + '">' + formatDeadline(task.deadline) + "</span>");
  }
  // Echeance repoussee plusieurs fois : la tache traine, elle merite un regard.
  if (task.deadline_shifts >= 2 && task.status !== "done") {
    meta.push('<span class="tag tag-shifted" title="Échéance repoussée ' + task.deadline_shifts +
      ' fois">reportée ' + task.deadline_shifts + " fois</span>");
  }
  meta.push("<span>" + ENERGY_LABELS[task.energy] + "</span>");
  if (task.project_name) {
    meta.push(
      '<span class="task-project">' +
        '<i class="project-dot" style="background:' + escapeHtml(task.project_color) + '"></i>' +
        escapeHtml(task.project_name) +
      "</span>"
    );
  }
  if (task.capture_id) {
    meta.push('<span class="tag tag-ai">déduite par NOMAIA</span>');
  }
  if (task.needs_review) {
    meta.push('<span class="tag tag-review">à vérifier</span>');
  }

  const checked = task.status === "done" ? " checked" : "";
  const pop = checked && String(task.id) === poppedId ? " anim-pop" : "";
  const enter = animateRows ? " anim-in" : "";
  const delay = animateRows ? "--i:" + Math.min(rowIndex++, 12) + ";" : "";
  const doneClass = task.status === "done" ? " done" : "";
  const treeClass = depth ? " is-subtask" : "";
  // Parent clos qui garde des sous-taches ouvertes : en-tete grise, pas « terminee ».
  const holder = task.status === "done" && task.children.some((child) => child.status !== "done")
    ? " is-holder"
    : "";

  // Le label elargit la zone de clic de la case ; il ne porte pas de
  // data-action, seul le clic relaye a la case declenche l'action.
  return (
    '<article class="task' + doneClass + treeClass + holder + enter + '" data-id="' + task.id +
      '" data-priority="' + task.priority + '" style="' + delay + "--depth:" + (depth || 0) + '">' +
      '<label class="check-hit" title="Marquer terminée">' +
        '<input type="checkbox" class="task-check' + pop + '" data-action="toggle"' + checked +
          ' aria-label="Marquer terminée">' +
      "</label>" +
      '<div class="task-body">' +
        '<span class="task-title">' + escapeHtml(task.title) + window.J.spaceBadge(task.space) + "</span>" +
        '<div class="task-meta">' + foldButton(task) + meta.join("") + "</div>" +
      "</div>" +
      '<div class="task-actions">' +
        '<button type="button" class="icon-btn" data-action="edit">Modifier</button>' +
        '<button type="button" class="icon-btn danger" data-action="delete">Supprimer</button>' +
      "</div>" +
    "</article>"
  );
}

/* Groupe d'un arbre : le plus urgent parmi ses taches ouvertes. Une
 * sous-tache en retard fait remonter tout son arbre dans « En retard ». */
function treeGroup(root) {
  const ouvertes = treeTasks(root).filter((task) => task.status !== "done");
  if (!ouvertes.length) return "done";
  return ouvertes.reduce(
    (best, task) => (GROUP_RANK[task.bucket] < GROUP_RANK[best] ? task.bucket : best),
    ouvertes[0].bucket
  );
}

function render(payload) {
  lastPayload = payload;
  rowIndex = 0;
  const tasks = payload.tasks;
  const stats = payload.stats;

  document.getElementById("stat-todo").textContent = stats.todo_count;
  document.getElementById("stat-late").textContent = stats.late_count;
  document.getElementById("stat-today").textContent = stats.today_count;
  document.getElementById("stat-load").textContent = formatDuration(stats.today_load) || "0 min";
  document.getElementById("stat-done").textContent = stats.done_today;

  if (!tasks.length) {
    board.innerHTML = '<p class="empty">Aucune tâche. Ajoute la première ci-dessus.</p>';
    return;
  }

  const roots = nestTasks(tasks);
  let html = "";
  GROUPS.forEach((group) => {
    const rows = roots.filter((root) => treeGroup(root) === group.key);
    if (!rows.length) return;
    html +=
      '<section class="group group-' + group.key + '">' +
        '<div class="group-head">' +
          '<span class="group-title">' + group.label + "</span>" +
          '<span class="group-count">' + rows.length + "</span>" +
        "</div>" +
        renderTree(rows, taskRow) +
      "</section>";
  });
  board.innerHTML = html;
  animateRows = false;
  poppedId = null;
}

/* Construit l'URL de liste : espace choisi dans la barre du haut, puis
 * filtre projet courant. */
function listUrl() {
  const projet = filterProject.value;
  return API + "?space=current" + (projet ? "&project_id=" + encodeURIComponent(projet) : "");
}

/* Espace a poser sur une tache sans projet ni parente (sinon le serveur
 * lui donne celui du projet ou de la parente) ; undefined = ne rien envoyer. */
function orphanSpace(projectId, parentId, chosen) {
  if (projectId || parentId) return undefined;
  return chosen || undefined;
}

async function reload() {
  try {
    render(await callApi(listUrl()));
  } catch (error) {
    board.innerHTML = '<p class="empty">' + escapeHtml(error.message) + "</p>";
  }
}

filterProject.addEventListener("change", () => {
  animateRows = true;
  reload();
});

/* ---------- Saisie rapide ---------- */

document.getElementById("quick-add").addEventListener("submit", async (event) => {
  event.preventDefault();
  const titleInput = document.getElementById("qa-title");

  const payload = {
    title: titleInput.value,
    priority: document.getElementById("qa-priority").value,
    estimate_min: document.getElementById("qa-estimate").value,
    deadline: document.getElementById("qa-deadline").value,
    energy: document.getElementById("qa-energy").value,
    project_id: document.getElementById("qa-project").value,
    // Espace filtre dans la barre du haut : la tache reste visible apres ajout.
    space: orphanSpace(
      document.getElementById("qa-project").value, null,
      window.J.space === "all" ? "" : window.J.space
    ),
  };

  try {
    await callApi(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    titleInput.value = "";
    titleInput.focus();
    await reload();
    toast("Tâche ajoutée.");
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Actions sur une tache (delegation d'evenements) ---------- */

board.addEventListener("click", async (event) => {
  const fold = event.target.closest("[data-fold]");
  if (fold) {
    toggleFold(fold.dataset.fold);
    render(lastPayload);
    return;
  }

  const trigger = event.target.closest("[data-action]");
  if (!trigger) return;

  const taskId = trigger.closest(".task").dataset.id;
  const action = trigger.dataset.action;

  // Coche : rebond immediat, rejoue sur la ligne redessinee.
  if (action === "toggle" && trigger.checked) {
    trigger.classList.add("anim-pop");
    poppedId = taskId;
  }

  try {
    if (action === "toggle") {
      await callApi(API + "/" + taskId + "/toggle", { method: "POST" });
      await reload();
    } else if (action === "delete") {
      if (!confirm("Supprimer cette tâche définitivement ?")) return;
      await callApi(API + "/" + taskId, { method: "DELETE" });
      await reload();
      toast("Tâche supprimée.");
    } else if (action === "edit") {
      openEditModal(await callApi(API + "/" + taskId));
    }
  } catch (error) {
    toast(error.message, true);
    await reload();
  }

  /* La tache a modifier est relue seule : inutile de recharger toute la liste. */
  function openEditModal(task) {
    if (!task) return;
    document.getElementById("ed-id").value = task.id;
    document.getElementById("ed-title").value = task.title;
    document.getElementById("ed-description").value = task.description || "";
    document.getElementById("ed-priority").value = task.priority;
    document.getElementById("ed-estimate").value = task.estimate_min;
    document.getElementById("ed-deadline").value = task.deadline || "";
    document.getElementById("ed-energy").value = task.energy;
    document.getElementById("ed-project").value = task.project_id || "";
    document.getElementById("ed-person").value = task.person_id || "";
    document.getElementById("ed-space").value = task.space || "pro";
    modal.dataset.parentId = task.parent_id || "";
    syncSpaceField();
    modal.showModal();
  }
});

/* ---------- Fenetre de modification ---------- */

/* L'espace ne se choisit que pour une tache sans projet ni parente :
 * sinon il suit celui du projet (ou de la parente). */
function syncSpaceField() {
  const field = document.getElementById("ed-space");
  field.disabled = Boolean(document.getElementById("ed-project").value || modal.dataset.parentId);
}

document.getElementById("ed-project").addEventListener("change", syncSpaceField);

document.getElementById("ed-cancel").addEventListener("click", () => modal.close());

document.getElementById("edit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const taskId = document.getElementById("ed-id").value;

  const payload = {
    title: document.getElementById("ed-title").value,
    description: document.getElementById("ed-description").value,
    priority: document.getElementById("ed-priority").value,
    estimate_min: document.getElementById("ed-estimate").value,
    deadline: document.getElementById("ed-deadline").value,
    energy: document.getElementById("ed-energy").value,
    project_id: document.getElementById("ed-project").value,
    person_id: document.getElementById("ed-person").value,
    space: orphanSpace(
      document.getElementById("ed-project").value, modal.dataset.parentId,
      document.getElementById("ed-space").value
    ),
  };

  try {
    await callApi(API + "/" + taskId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    modal.close();
    await reload();
    toast("Tâche mise à jour.");
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Initialisation ---------- */

const headerDate = document.getElementById("header-date");
headerDate.textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

// Destinataires : la liste vient de l'API, pas du gabarit, parce que le chat
// et le triage en creent en cours de session sans que la page soit rechargee.
async function loadPeople() {
  const select = document.getElementById("ed-person");
  if (!select) return;

  try {
    const data = await callApi("/api/people");
    (data.people || []).forEach((person) => {
      const option = document.createElement("option");
      option.value = person.id;
      option.textContent = person.project_name
        ? person.name + " (" + person.project_name + ")"
        : person.name;
      select.appendChild(option);
    });
  } catch (error) {
    // Sans destinataires, la modale reste utilisable : le champ est facultatif.
    console.warn("Interlocuteurs non charges :", error.message);
  }
}

document.getElementById("qa-title").focus();
loadPeople();
reload();
// Rechargement a chaud : la liste se redessine sans rejouer l'entree, filtre garde.
J.live.onChange(reload);
