/* NOMAIA — « Ou en etais-je ? » sur la fiche projet (lot 4).
 *
 * Synthese de reprise lue sur /api/projects/<id>/resume : derniere action,
 * prochaine etape, etape en cours, ce qui bloque, anomalies, livrables,
 * decisions, sessions Claude ; vue chronologique en second onglet. Le
 * resume redige par l'IA est facultatif : ses references [T12] (tache) et
 * [J8] (journal) deviennent des liens vers la source.
 */
(function () {
  const panel = document.getElementById("rs-panel");
  if (!panel) return;
  const { callApi, escapeHtml, toast } = window.J;
  const projectId = Number(document.getElementById("pj-root").dataset.projectId);
  const body = document.getElementById("rs-body");
  const aiBox = document.getElementById("rs-ai-box");
  const aiButton = document.getElementById("rs-ai");
  let data = null;
  let tab = "synthese";

  const SIDE = { client: "attend le client", both: "conjoint" };

  function ago(days) {
    if (days === null || days === undefined) return "";
    if (days <= 0) return "aujourd'hui";
    if (days === 1) return "hier";
    return "il y a " + days + " jours";
  }

  function shortDate(stamp) {
    if (!stamp) return "";
    const day = new Date(String(stamp).slice(0, 10) + "T00:00:00");
    return day.toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
  }

  function cut(text, size) {
    const value = String(text || "").replace(/\s+/g, " ").trim();
    return value.length > size ? value.slice(0, size - 1) + "…" : value;
  }

  function taskLink(task, extra) {
    return '<button type="button" class="link rs-task" data-task="' + task.id + '">' +
      escapeHtml(task.title) + "</button>" + (extra || "");
  }

  function block(title, inner, modifier) {
    return '<div class="rs-block' + (modifier ? " " + modifier : "") + '"><p class="rs-label">' + title + "</p>" +
      inner + "</div>";
  }

  function listOf(items, render, empty) {
    if (!items.length) return '<p class="rs-muted">' + empty + "</p>";
    return '<ul class="rs-list">' + items.map((item) => "<li>" + render(item) + "</li>").join("") + "</ul>";
  }

  function renderSynthese() {
    const last = data.last_action;
    const step = data.next_step;
    const phase = data.current_phase;
    const milestone = data.next_milestone;

    // Tache terminee : son titre est le texte, il ouvre la tache (pas de doublon).
    const lastIsTask = last && last.type === "done";
    const lastHtml = last
      ? '<p class="rs-main">' + (lastIsTask ? taskLink({ id: last.task_id, title: last.text })
        : escapeHtml(cut(last.text, 220))) + "</p>" +
        '<p class="rs-muted">' + escapeHtml(last.label) + " · " + ago(data.idle_days) +
        (last.task_id && !lastIsTask ? " · " + taskLink({ id: last.task_id, title: last.task_title || "tâche" }) : "") +
        "</p>"
      : '<p class="rs-muted">Rien n\'est encore enregistré sur ce projet.</p>';

    const stepHtml = step
      ? '<p class="rs-main">' + taskLink(step) + "</p>" + '<p class="rs-muted">' +
        [step.progress_pct ? step.progress_pct + " % fait" : "pas commencée",
         step.deadline ? "échéance " + shortDate(step.deadline) : ""].filter(Boolean).join(" · ") + "</p>"
      : '<p class="rs-muted">Rien à faire de notre côté : tout attend le client ou un déblocage.</p>';

    const phaseHtml = phase
      ? '<p class="rs-main">' + escapeHtml(phase.name) + " · " + (phase.progress_pct || 0) + " %</p>" +
        (milestone && milestone.date ? '<p class="rs-muted">Prochain jalon : ' + escapeHtml(milestone.name) +
          " le " + shortDate(milestone.date) + "</p>" : "")
      : '<p class="rs-muted">Aucune étape définie.</p>';

    const blocked = data.blocked.slice(0, 4);
    const blockedBadge = (task) => task.blocked
      ? ' <span class="badge badge-p1">bloquée</span>'
      : ' <span class="badge badge-p2">' + escapeHtml(SIDE[task.side] || "client") + "</span>";
    const blockedHtml = listOf(blocked, (task) => taskLink(task, blockedBadge(task)), "Rien de bloqué.") +
      (data.waiting.length
      ? '<p class="rs-muted">' + data.waiting.length + " attente" + (data.waiting.length > 1 ? "s" : "") +
        " de relance : " + data.waiting.slice(0, 3).map((w) => escapeHtml(w.person_name)).join(", ") + "</p>" : "");

    const quality = listOf(data.anomalies.slice(0, 3), (task) => taskLink(task, ' <span class="badge badge-p1">' +
      escapeHtml(task.severity || "anomalie") + "</span>"), "Aucune anomalie ouverte.") +
      listOf(data.deliverables.slice(0, 3), (task) => taskLink(task, ' <span class="badge badge-indigo">livrable</span>'), "");

    const decisions = listOf(data.decisions.slice(0, 3), (entry) => escapeHtml(cut(entry.content, 140)) +
      ' <span class="rs-muted">' + shortDate(entry.created_at) + "</span>", "Aucune décision au registre.");

    const claude = listOf(data.claude_sessions.slice(0, 3), (item) => escapeHtml(cut(item.text, 140)) +
      ' <span class="rs-muted">' + shortDate(item.at) + "</span>", "Aucune session Claude enregistrée.");

    return '<div class="rs-grid">' +
      block("Dernière action", lastHtml, "rs-wide") +
      block("Prochaine étape", stepHtml, "rs-accent") +
      block("Étape en cours", phaseHtml) +
      block("Ce qui bloque (" + data.blocked.length + ")", blockedHtml) +
      block("Anomalies et livrables", quality) +
      block("Décisions récentes", decisions) +
      block("Sessions Claude", claude) +
      "</div>";
  }

  function renderChrono() {
    if (!data.timeline.length) return '<p class="empty">Aucun événement enregistré.</p>';
    return '<ol class="rs-timeline">' + data.timeline.map((item, index) =>
      '<li class="anim-in" style="--i:' + Math.min(index, 12) + '" id="rs-ref-' + escapeHtml(item.ref) + '">' +
        '<span class="rs-when">' + shortDate(item.at) + "</span>" +
        '<span class="badge ' + (item.type === "done" ? "badge-ok" : item.type === "decision" ? "badge-indigo" : "") + '">' +
          escapeHtml(item.label) + "</span>" +
        '<span class="rs-what">' + escapeHtml(cut(item.text, 260)) +
          (item.task_title && item.type !== "done" ? ' <span class="rs-muted">· ' + taskLink({ id: item.task_id, title: item.task_title }) + "</span>" : "") +
        "</span></li>").join("") + "</ol>";
  }

  function render() {
    panel.querySelectorAll("[data-tab]").forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.tab === tab));
    });
    body.innerHTML = tab === "synthese" ? renderSynthese() : renderChrono();
  }

  async function load() {
    try {
      data = await callApi("/api/projects/" + projectId + "/resume");
      render();
    } catch (error) {
      body.innerHTML = '<p class="error-box">' + escapeHtml(error.message) + "</p>";
    }
  }

  /* Resume IA : references [T12] / [J8] rendues cliquables, sources verifiees cote serveur. */
  function linkRefs(text) {
    return escapeHtml(text).replace(/\[([TJ])(\d+)\]/g, (match, kind, id) =>
      '<button type="button" class="rs-ref" data-ref="' + kind + id + '" title="Voir la source">' + kind + id + "</button>");
  }

  async function summarize() {
    aiButton.disabled = true;
    aiBox.hidden = false;
    aiBox.innerHTML = '<div class="skel skel-line"></div><div class="skel skel-line" style="width:70%"></div>';
    try {
      const result = await callApi("/api/projects/" + projectId + "/resume/summary", { method: "POST" });
      aiBox.innerHTML = '<p class="rs-ai-text">' + linkRefs(result.summary) + "</p>" +
        (result.next_action ? '<p class="rs-ai-next"><strong>Prochaine action :</strong> ' + escapeHtml(result.next_action) + "</p>" : "") +
        '<p class="rs-muted">Rédigé par l\'IA à partir des seules données enregistrées ; chaque référence ouvre sa source.</p>';
    } catch (error) {
      aiBox.hidden = true;
      toast(error.message, true);
    } finally {
      aiButton.disabled = false;
    }
  }

  function openTask(taskId) {
    window.TaskDetail.open(taskId, {
      onChange: () => { load(); if (typeof window.reload === "function") window.reload(); },
    });
  }

  panel.addEventListener("click", (event) => {
    const tabButton = event.target.closest("[data-tab]");
    if (tabButton) { tab = tabButton.dataset.tab; render(); return; }
    const taskButton = event.target.closest("[data-task]");
    if (taskButton) { openTask(Number(taskButton.dataset.task)); return; }
    const ref = event.target.closest(".rs-ref");
    if (!ref) return;
    if (ref.dataset.ref[0] === "T") { openTask(Number(ref.dataset.ref.slice(1))); return; }
    // Entree de journal : montree dans la chronologie, mise en evidence.
    tab = "chrono";
    render();
    const target = document.getElementById("rs-ref-" + ref.dataset.ref);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("is-flash");
      window.setTimeout(() => target.classList.remove("is-flash"), 1600);
    } else {
      toast("Cette entrée n'est plus dans les 25 dernières.");
    }
  });

  aiButton.addEventListener("click", summarize);
  load();
})();
