/* NOMAIA — fiche de detail d'une tache, partagee par le plan et les projets.
 *
 * Edition complete, avancement, journal et etapes. La page hote fournit le
 * gabarit templates/_task_detail.html et appelle :
 *
 *   window.TaskDetail.open(taskId, { day: "AAAA-MM-JJ", onChange: rafraichir })
 *
 * `day` est le jour vise par « Faire maintenant » (masque pour un jour passe),
 * `onChange` est rappele apres toute modification.
 *
 * Enveloppe dans une fonction : les constantes de premier niveau d'un script
 * classique sont partagees avec les autres scripts de la page.
 */
(function () {
  const { toast, callApi, formatDuration, formatDeadline } = window.J;

  const modal = document.getElementById("task-detail");
  if (!modal) return;

  const form = document.getElementById("td-form");
  const errorEl = document.getElementById("td-error");
  const stepsList = document.getElementById("td-steps-list");
  const projectSelect = document.getElementById("td-project");
  const progressEl = document.getElementById("td-progress");
  const progressValueEl = document.getElementById("td-progress-value");
  const journalList = document.getElementById("td-journal-list");
  const nowEl = document.getElementById("td-now");
  const phaseSelect = document.getElementById("td-phase");
  const phaseField = phaseSelect.parentElement;
  const kindSelect = document.getElementById("td-kind");
  const sideSelect = document.getElementById("td-side");

  const SOURCE_LABELS = { claude: "session Claude", chat: "chat NOMAIA" };

  let current = null;
  let options = {};
  let projectsLoaded = false;

  function today() {
    const now = new Date();
    return now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-" +
      String(now.getDate()).padStart(2, "0");
  }

  /* Gravite et module ne concernent qu'une anomalie. */
  function syncKindFields() {
    const anomalie = kindSelect.value === "anomaly";
    document.getElementById("td-severity-field").hidden = !anomalie;
    document.getElementById("td-module-field").hidden = !anomalie;
  }

  function showError(message) {
    errorEl.hidden = !message;
    errorEl.textContent = message || "";
  }

  async function changed() {
    if (typeof options.onChange === "function") await options.onChange();
  }

  async function loadProjectOptions() {
    if (projectsLoaded) return;
    const data = await callApi("/api/projects");
    (data.projects || [])
      .filter((project) => project.status !== "done")
      .forEach((project) => {
        const option = document.createElement("option");
        option.value = project.id;
        option.textContent = project.name;
        projectSelect.appendChild(option);
      });
    projectsLoaded = true;
  }

  /* Etapes du projet choisi. Le champ disparait pour une tache sans projet
   * ou un projet qui n'est pas decoupe en etapes. */
  async function loadPhaseOptions(projectId, selected) {
    phaseSelect.replaceChildren(new Option("Sans étape", ""));
    let phases = [];
    if (projectId) {
      try {
        phases = (await callApi("/api/projects/" + projectId + "/phases")).phases || [];
      } catch (error) {
        // Liste indisponible : la tache reste editable, sans etape.
      }
    }
    phases.forEach((phase) => {
      phaseSelect.appendChild(new Option(phase.sequence + ". " + phase.name, phase.id));
    });
    phaseSelect.value = selected || "";
    phaseField.hidden = !phases.length;
  }

  function renderSteps(steps) {
    stepsList.replaceChildren();
    if (!steps.length) {
      const vide = document.createElement("p");
      vide.className = "plan-hint";
      vide.textContent = "Aucune étape.";
      stepsList.appendChild(vide);
      return;
    }
    steps.forEach((step) => {
      const row = document.createElement("label");
      row.className = "td-step" + (step.status === "done" ? " is-done" : "");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = step.status === "done";
      check.dataset.stepId = step.id;
      const title = document.createElement("span");
      title.textContent = step.title + " · " + formatDuration(step.estimate_min);
      row.appendChild(check);
      row.appendChild(title);
      stepsList.appendChild(row);
    });
  }

  function renderJournal(task) {
    progressEl.value = task.progress_pct || 0;
    progressValueEl.textContent = (task.progress_pct || 0) + " %";
    progressEl.dataset.initial = String(task.progress_pct || 0);
    journalList.replaceChildren();
    (task.journal || []).forEach((entry) => {
      const item = document.createElement("li");
      const when = document.createElement("span");
      when.className = "work-log-when";
      when.textContent = formatDeadline(entry.created_at.slice(0, 10)) +
        (entry.source ? " · " + (SOURCE_LABELS[entry.source] || entry.source) : "") + " ";
      item.appendChild(when);
      item.appendChild(document.createTextNode(entry.content));
      if (entry.minutes) {
        const minutes = document.createElement("span");
        minutes.className = "work-log-min";
        minutes.textContent = " " + formatDuration(entry.minutes);
        item.appendChild(minutes);
      }
      journalList.appendChild(item);
    });
  }

  async function open(taskId, opts) {
    if (opts) options = opts;
    showError(null);
    try {
      await loadProjectOptions();
    } catch (error) {
      // Sans projets, la fiche reste utilisable : le champ est facultatif.
    }
    const task = await callApi("/api/tasks/" + taskId);
    current = task;
    document.getElementById("td-title").value = task.title;
    document.getElementById("td-description").value = task.description || "";
    document.getElementById("td-priority").value = task.priority;
    document.getElementById("td-estimate").value = task.estimate_min;
    document.getElementById("td-deadline").value = task.deadline || "";
    document.getElementById("td-energy").value = task.energy || "med";
    // Projet termine ou liste indisponible : l'option manque, et sans elle
    // l'enregistrement detacherait la tache. On l'ajoute pour cette tache.
    if (task.project_id && !projectSelect.querySelector('option[value="' + task.project_id + '"]')) {
      const option = document.createElement("option");
      option.value = task.project_id;
      option.textContent = task.project_name || "Projet " + task.project_id;
      projectSelect.appendChild(option);
    }
    projectSelect.value = task.project_id || "";
    await loadPhaseOptions(task.project_id, task.phase_id);
    sideSelect.value = task.side || "us";
    document.getElementById("td-blocked").checked = Boolean(task.blocked);
    kindSelect.value = task.kind || "task";
    document.getElementById("td-severity").value = task.severity || "major";
    document.getElementById("td-module").value = task.module || "";
    document.getElementById("td-cause").value = task.cause || "";
    // Pieces jointes : un livrable recu (PV, fichier client) s'attache ici.
    window.JAttachments.mount(document.getElementById("td-attachments"), "task", task.id);
    document.getElementById("td-solution").value = task.solution || "";
    syncKindFields();
    const day = options.day || today();
    nowEl.hidden = task.status !== "todo" || day < today();
    renderSteps(task.subtasks || []);
    renderJournal(task);
    if (!modal.open) modal.showModal();
  }

  function close() {
    current = null;
    if (modal.open) modal.close();
  }

  document.getElementById("td-close").addEventListener("click", close);
  document.getElementById("td-cancel").addEventListener("click", close);

  kindSelect.addEventListener("change", () => {
    syncKindFields();
    // Un livrable est attendu du client : on le propose, sans l'imposer.
    if (kindSelect.value === "deliverable" && sideSelect.value === "us") sideSelect.value = "client";
  });

  projectSelect.addEventListener("change", () => {
    loadPhaseOptions(projectSelect.value, null);
  });

  progressEl.addEventListener("input", () => {
    progressValueEl.textContent = progressEl.value + " %";
  });

  document.getElementById("td-note-add").addEventListener("click", async () => {
    if (!current) return;
    const noteEl = document.getElementById("td-note");
    const progressChanged = progressEl.value !== progressEl.dataset.initial;
    const content = noteEl.value.trim() || (progressChanged ? "Avancement : " + progressEl.value + " %." : "");
    if (!content) {
      noteEl.focus();
      return;
    }
    try {
      await callApi("/api/tasks/" + current.id + "/journal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: content,
          progress_pct: progressChanged ? Number(progressEl.value) : null,
        }),
      });
      noteEl.value = "";
      await open(current.id);
      await changed();
    } catch (error) {
      showError(error.message);
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!current) return;
    const payload = {
      title: document.getElementById("td-title").value,
      description: document.getElementById("td-description").value,
      priority: document.getElementById("td-priority").value,
      estimate_min: document.getElementById("td-estimate").value,
      deadline: document.getElementById("td-deadline").value,
      energy: document.getElementById("td-energy").value,
      side: sideSelect.value,
      blocked: document.getElementById("td-blocked").checked,
      kind: kindSelect.value,
    };
    if (kindSelect.value === "anomaly") {
      payload.severity = document.getElementById("td-severity").value;
      payload.module = document.getElementById("td-module").value;
      payload.cause = document.getElementById("td-cause").value;
      payload.solution = document.getElementById("td-solution").value;
    }
    if (projectSelect.value !== String(current.project_id || "")) {
      payload.project_id = projectSelect.value;
    }
    if (phaseSelect.value !== String(current.phase_id || "")) {
      payload.phase_id = phaseSelect.value || null;
    }
    try {
      await callApi("/api/tasks/" + current.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      close();
      await changed();
      toast("Tâche mise à jour.");
    } catch (error) {
      showError(error.message);
    }
  });

  nowEl.addEventListener("click", async () => {
    if (!current) return;
    try {
      await callApi("/api/plan/pin/" + current.id, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: options.day || today(), position: "now" }),
      });
      close();
      await changed();
      toast("Placée en tête de journée.");
    } catch (error) {
      showError(error.message);
    }
  });

  document.getElementById("td-step-add").addEventListener("click", async () => {
    if (!current) return;
    const titleEl = document.getElementById("td-step-title");
    const title = titleEl.value.trim();
    if (!title) {
      titleEl.focus();
      return;
    }
    try {
      // L'etape herite du projet et de l'echeance : elle sert le meme but.
      await callApi("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title,
          parent_id: current.id,
          project_id: current.project_id,
          deadline: current.deadline,
          priority: current.priority,
          estimate_min: document.getElementById("td-step-estimate").value,
        }),
      });
      titleEl.value = "";
      await open(current.id);
      await changed();
    } catch (error) {
      showError(error.message);
    }
  });

  document.getElementById("td-step-title").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      document.getElementById("td-step-add").click();
    }
  });

  stepsList.addEventListener("change", async (event) => {
    const stepId = event.target.dataset.stepId;
    if (!stepId || !current) return;
    try {
      await callApi("/api/tasks/" + stepId + "/toggle", { method: "POST" });
      await open(current.id);
      await changed();
    } catch (error) {
      showError(error.message);
    }
  });

  window.TaskDetail = { open: open, close: close };
})();
