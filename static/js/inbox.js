/* NOMAIA — boite de reception : depot d'entrees brutes et dialogue de triage. */

const { toast, callApi, escapeHtml, formatDuration, formatDeadline, formatMoment } = window.J;

const listEl = document.getElementById("thread-list");
const scrollEl = document.getElementById("thread-scroll");
const moreEl = document.getElementById("thread-more");
const filterEl = document.querySelector(".inbox-filter");
const viewEl = document.getElementById("thread-view");
const textEl = document.getElementById("cp-text");
const fileEl = document.getElementById("cp-file");
const previewEl = document.getElementById("cp-preview");
const previewImg = document.getElementById("cp-preview-img");
const previewMail = document.getElementById("cp-preview-mail");

const STATUS_LABELS = {
  new: "en attente",
  processing: "analyse en cours",
  waiting: "réponse attendue",
  done: "traitée",
  failed: "échec",
};

const PRIORITY_LABELS = { 1: "Critique", 2: "Haute", 3: "Normale", 4: "Basse" };

// Taille d'une page de la liste : « Afficher plus » en ajoute autant.
const LIST_PAGE = 30;

let pendingImage = null;
let pendingMail = null;
let selectedId = null;
let pollTimer = null;
// Derniere liste affichee : le sondage ne touche au DOM que si elle a change,
// sans quoi le survol et l'apparition se rejoueraient toutes les 1,5 s.
let listHtml = "";
// Entrees deja affichees : seule une entree nouvelle rejoue l'apparition en cascade.
const shownCaptures = new Set();

// Filtre de la liste ('open' : a traiter, 'all' : tout). null jusqu'au premier
// chargement, qui choisit « A traiter » s'il reste au moins une entree a traiter.
let listFilter = null;
let listLimit = LIST_PAGE;
let openCount = 0;
// Numero du dernier rafraichissement lance : une reponse depassee (filtre
// change entre-temps) n'est pas affichee.
let refreshSeq = 0;

// Empreinte du dernier fil affiche : le sondage ne reconstruit la vue que si
// le contenu a change. Sans elle, la reponse en cours de saisie etait effacee
// toutes les quelques secondes.
let renderedSignature = null;

// Brouillons de reponse par entree : survivent a un rafraichissement et a un
// changement d'entree selectionnee.
const answerDrafts = {};

/* ---------- Composition ---------- */

function isEml(file) {
  return file.type === "message/rfc822" || /\.eml$/i.test(file.name || "");
}

/* Une seule piece jointe a la fois : image ou mail .eml. */
function setPendingFile(file) {
  pendingImage = null;
  pendingMail = null;
  previewImg.removeAttribute("src");
  previewImg.hidden = true;
  previewMail.hidden = true;
  previewEl.hidden = !file;
  if (!file) return;

  if (isEml(file)) {
    pendingMail = file;
    previewMail.textContent = "Mail : " + file.name;
    previewMail.hidden = false;
  } else {
    pendingImage = file;
    previewImg.src = URL.createObjectURL(file);
    previewImg.hidden = false;
  }
}

document.addEventListener("paste", (event) => {
  const item = Array.from(event.clipboardData?.items || []).find(
    (entry) => entry.type.startsWith("image/")
  );
  if (!item) return;
  setPendingFile(item.getAsFile());
  toast("Capture d'écran attachée.");
});

fileEl.addEventListener("change", () => setPendingFile(fileEl.files[0] || null));

textEl.addEventListener("dragover", (event) => event.preventDefault());

textEl.addEventListener("drop", (event) => {
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  event.preventDefault();
  if (!isEml(file) && !file.type.startsWith("image/")) {
    toast("Dépose une image ou un mail .eml.", true);
    return;
  }
  setPendingFile(file);
  toast(isEml(file) ? "Mail attaché." : "Image attachée.");
});

document.getElementById("cp-preview-remove").addEventListener("click", () => {
  fileEl.value = "";
  setPendingFile(null);
});

document.getElementById("cp-send").addEventListener("click", send);

textEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) send();
});

async function send() {
  const text = textEl.value.trim();
  if (!text && !pendingImage && !pendingMail) {
    toast("Écris quelque chose ou attache une capture.", true);
    return;
  }

  const button = document.getElementById("cp-send");
  button.disabled = true;

  try {
    let capture;
    if (pendingMail) {
      const form = new FormData();
      form.append("eml", pendingMail);
      if (text) form.append("text", text);
      capture = await callApi("/api/capture/eml", { method: "POST", body: form });
    } else if (pendingImage) {
      const form = new FormData();
      form.append("image", pendingImage);
      if (text) form.append("text", text);
      capture = await callApi("/api/capture", { method: "POST", body: form });
    } else {
      capture = await callApi("/api/capture", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
    }

    textEl.value = "";
    fileEl.value = "";
    setPendingFile(null);
    selectedId = capture.id;
    toast("NOMAIA analyse l'entrée…");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

/* ---------- Liste des entrees ---------- */

function captureExcerpt(capture) {
  if (capture.raw_text) return capture.raw_text.slice(0, 90);
  return capture.image_path ? "Capture d'écran" : "(vide)";
}

function renderList(captures) {
  const emptyText = listFilter === "open" ? "Rien à traiter." : "Aucune entrée pour l'instant.";
  const html = captures.length ? listItemsHtml(captures) : '<p class="empty">' + emptyText + "</p>";
  if (html === listHtml) return;
  listHtml = html;
  // Le sondage ne doit pas ramener la liste en haut.
  const scrollTop = scrollEl.scrollTop;
  listEl.innerHTML = html;
  scrollEl.scrollTop = scrollTop;
  markNewItems();
}

function renderFilter() {
  document.getElementById("filter-open-count").textContent = "(" + openCount + ")";
  filterEl.querySelectorAll("[data-filter]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.filter === listFilter));
  });
}

filterEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button || button.dataset.filter === listFilter) return;
  listFilter = button.dataset.filter;
  listLimit = LIST_PAGE;
  scrollEl.scrollTop = 0;
  renderFilter();
  refresh();
});

moreEl.addEventListener("click", () => {
  listLimit += LIST_PAGE;
  refresh();
});

/* Apparition en cascade des seules entrees jamais affichees (rang plafonne a 12). */
function markNewItems() {
  let rank = 0;
  listEl.querySelectorAll(".thread-item").forEach((item) => {
    if (shownCaptures.has(item.dataset.id)) return;
    shownCaptures.add(item.dataset.id);
    item.classList.add("anim-in");
    item.style.setProperty("--i", String(Math.min(rank, 12)));
    rank += 1;
  });
}

function listItemsHtml(captures) {
  return captures
    .map((capture) => {
      const active = String(capture.id) === String(selectedId) ? " active" : "";
      const summary = capture.proposal?.summary || captureExcerpt(capture);
      return (
        '<button type="button" class="thread-item' + active + '" data-id="' + capture.id + '">' +
          '<span class="thread-status s-' + capture.status + '">' +
            (STATUS_LABELS[capture.status] || capture.status) +
          "</span>" +
          '<span class="thread-summary">' + escapeHtml(summary) + window.J.spaceBadge(capture.space) + "</span>" +
          '<span class="thread-time">' + formatMoment(capture.created_at) +
            (capture.source !== "web" ? " · " + escapeHtml(capture.source) : "") +
          "</span>" +
        "</button>"
      );
    })
    .join("");
}

listEl.addEventListener("click", async (event) => {
  const item = event.target.closest(".thread-item");
  if (!item) return;
  selectedId = item.dataset.id;
  await refresh();
  revealThread();
});

/* Telephone : les colonnes sont empilees, on descend jusqu'au detail choisi. */
function revealThread() {
  if (!window.matchMedia("(max-width: 860px)").matches) return;
  const reduced = window.J.prefs?.get("motion") === "reduced" ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  viewEl.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
}

/* ---------- Fil de discussion ---------- */

function proposedTaskRow(task) {
  const meta = [
    "P" + task.priority + " · " + PRIORITY_LABELS[task.priority],
    formatDuration(task.estimate_min),
  ];
  if (task.deadline) meta.push("échéance " + formatDeadline(task.deadline));
  if (task.project_hint) meta.push(escapeHtml(task.project_hint));
  if (task.duplicate_of) meta.push("doublon de la tâche #" + task.duplicate_of);

  return (
    '<div class="proposed" data-priority="' + task.priority + '">' +
      '<span class="proposed-title">' + escapeHtml(task.title) + "</span>" +
      '<span class="proposed-meta">' + meta.join(" · ") + "</span>" +
      (task.description
        ? '<span class="proposed-desc">' + escapeHtml(task.description) + "</span>"
        : "") +
    "</div>"
  );
}

function renderThread(thread) {
  const capture = thread.capture;
  const proposal = thread.proposal;
  const payload = proposal?.payload || {};
  let html = "";

  html += '<div class="thread-head">';
  html += '<span class="thread-status s-' + capture.status + '">' +
    (STATUS_LABELS[capture.status] || capture.status) + "</span>";
  if (proposal) {
    html += '<span class="confidence">confiance ' +
      Math.round((proposal.confidence || 0) * 100) + " %</span>";
  }
  html += "</div>";

  if (capture.image_path) {
    const relative = capture.image_path.split(/[\\/]/).slice(-2).join("/");
    html += '<img class="capture-image" src="/api/uploads/' + relative + '" alt="Capture">';
  }

  if (capture.raw_text) {
    html += '<pre class="capture-text">' + escapeHtml(capture.raw_text) + "</pre>";
  }

  if (capture.status === "failed") {
    html += '<p class="error-box">Échec du triage : ' + escapeHtml(capture.error || "") +
      ' <button type="button" class="btn-ghost" data-action="retry">Réessayer</button></p>';
  }

  thread.messages.forEach((message) => {
    const who = message.role === "nomaia" ? "NOMAIA" : "Moi";
    html += '<div class="bubble ' + message.role + '">' +
      '<span class="bubble-who">' + who + "</span>" +
      escapeHtml(message.content).replace(/\n/g, "<br>") +
      "</div>";
  });

  // Toute entree en attente accepte une reponse : question posee, proposition
  // peu sure ou « aucune action identifiee ». Sans zone de saisie, la seule
  // issue etait de valider ou rejeter.
  const questions = payload.questions || [];
  if (capture.status === "waiting") {
    const chips = questions
      .flatMap((question) => question.suggestions || [])
      .map((value) => '<button type="button" class="chip" data-chip="' + escapeHtml(value) + '">' +
        escapeHtml(value) + "</button>")
      .join("");
    const placeholder = questions.length ? "Ta réponse…" : "Précise ou corrige, NOMAIA reprend l'analyse…";
    html += '<div class="answer-box">' + (chips ? '<div class="chips">' + chips + "</div>" : "") +
      '<textarea id="answer-text" rows="2" placeholder="' + placeholder + '"></textarea>' +
      '<button type="button" class="btn-primary" data-action="answer">Répondre</button>' +
      "</div>";
  }

  if (proposal && proposal.status === "pending" && (payload.tasks || []).length) {
    html += '<div class="proposal-box">' +
      '<h3>Tâches proposées</h3>' +
      payload.tasks.map(proposedTaskRow).join("") +
      '<div class="proposal-actions">' +
        '<button type="button" class="btn-ghost" data-action="reject">Rejeter</button>' +
        '<button type="button" class="btn-primary" data-action="apply" data-proposal="' +
          proposal.id + '">Créer ces tâches</button>' +
      "</div>" +
    "</div>";
  }

  // Sortie sans valider : une salutation ou une question devenue sans objet ne
  // doit pas rester « à traiter » faute de tâches à rejeter.
  if (capture.status === "waiting" || capture.status === "failed") {
    html += '<div class="dismiss-row"><button type="button" class="btn-ghost" data-action="dismiss"' +
      ' title="Fermer cette entrée sans créer de tâche ; elle reste visible dans « Tout »">Classer sans suite</button></div>';
  }

  if (thread.tasks.length) {
    html += '<div class="created-box"><h3>Tâches créées</h3>' +
      thread.tasks
        .map((task) =>
          '<div class="proposed" data-priority="' + task.priority + '">' +
            '<span class="proposed-title">' + escapeHtml(task.title) + "</span>" +
            '<span class="proposed-meta">P' + task.priority + " · " +
              formatDuration(task.estimate_min) +
              (task.deadline ? " · échéance " + formatDeadline(task.deadline) : "") +
            "</span>" +
          "</div>"
        )
        .join("") +
      '<a class="link" href="/taches">Voir dans les tâches</a></div>';
  }

  const signature = capture.id + "|" + html;
  if (signature === renderedSignature) return;

  // La zone de reponse est recreee : on lui rend brouillon, focus et curseur.
  const previous = document.getElementById("answer-text");
  const hadFocus = previous && document.activeElement === previous;
  const caret = hadFocus ? previous.selectionStart : null;

  // Autre entree : le detail, qui defile seul sur grand ecran, repart du haut.
  if (viewEl.dataset.captureId !== String(capture.id)) viewEl.scrollTop = 0;
  viewEl.innerHTML = html;
  viewEl.dataset.captureId = capture.id;
  renderedSignature = signature;

  const box = document.getElementById("answer-text");
  if (box) {
    box.value = answerDrafts[capture.id] || "";
    if (hadFocus) {
      box.focus();
      box.setSelectionRange(caret, caret);
    }
  }
}

viewEl.addEventListener("input", (event) => {
  if (event.target.id === "answer-text") {
    answerDrafts[viewEl.dataset.captureId] = event.target.value;
  }
});

viewEl.addEventListener("click", async (event) => {
  const trigger = event.target.closest("[data-action], [data-chip]");
  if (!trigger) return;

  const captureId = viewEl.dataset.captureId;

  if (trigger.dataset.chip) {
    const box = document.getElementById("answer-text");
    box.value = box.value ? box.value + ", " + trigger.dataset.chip : trigger.dataset.chip;
    answerDrafts[captureId] = box.value;
    box.focus();
    return;
  }

  try {
    if (trigger.dataset.action === "answer") {
      const answer = document.getElementById("answer-text").value.trim();
      if (!answer) return;
      await callApi("/api/capture/" + captureId + "/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      });
      delete answerDrafts[captureId];
      toast("Réponse envoyée, NOMAIA reprend l'analyse.");
    } else if (trigger.dataset.action === "apply") {
      const result = await callApi("/api/proposal/" + trigger.dataset.proposal + "/apply", {
        method: "POST",
      });
      toast(result.tasks.length + " tâche(s) créée(s).");
    } else if (trigger.dataset.action === "reject") {
      const proposalId = viewEl.querySelector("[data-proposal]")?.dataset.proposal;
      if (proposalId) {
        await callApi("/api/proposal/" + proposalId + "/reject", { method: "POST" });
      }
      toast("Proposition rejetée.");
    } else if (trigger.dataset.action === "dismiss") {
      await callApi("/api/capture/" + captureId + "/dismiss", { method: "POST" });
      toast("Entrée classée sans suite.");
    } else if (trigger.dataset.action === "retry") {
      await callApi("/api/capture/" + captureId + "/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer: "Reprends l'analyse." }),
      });
      toast("Nouvelle tentative lancée.");
    }
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Radar d'analyse ---------- */

function renderRadar(enCours) {
  const zone = document.getElementById("radar-zone");
  if (!enCours) {
    zone.hidden = true;
    return;
  }
  document.getElementById("radar-label").textContent =
    enCours > 1 ? enCours + " entrées en analyse" : "Analyse en cours";
  zone.hidden = false;
}

/* ---------- Rafraichissement ---------- */

async function refresh() {
  const seq = ++refreshSeq;
  try {
    const filter = listFilter || "open";
    const requested = listLimit;
    const data = await callApi("/api/capture?filter=" + filter + "&limit=" + requested);
    if (seq !== refreshSeq) return;

    openCount = Number(data.open_count) || 0;
    if (listFilter === null) {
      listFilter = openCount ? "open" : "all";
      if (listFilter !== filter) {
        renderFilter();
        return refresh();
      }
    }
    renderFilter();
    renderList(data.captures);
    // Limite plafonnee par le serveur : inutile de proposer davantage.
    moreEl.hidden = !data.has_more || data.limit < requested;

    if (selectedId) {
      renderThread(await callApi("/api/capture/" + selectedId));
    }

    // Tant qu'une entree est en cours d'analyse, on interroge plus souvent.
    const enCours = data.captures.filter(
      (capture) => capture.status === "new" || capture.status === "processing"
    );
    const busy = enCours.length > 0;
    renderRadar(enCours.length);
    schedulePoll(busy ? 1500 : 6000);
  } catch (error) {
    if (seq !== refreshSeq) return;
    listHtml = "";
    listEl.innerHTML = '<p class="empty">' + escapeHtml(error.message) + "</p>";
    moreEl.hidden = true;
    schedulePoll(8000);
  }
}

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, delay);
}

/* ---------- Appairage du telephone ---------- */

async function refreshPairing() {
  const banner = document.getElementById("pair-banner");
  try {
    const data = await callApi("/api/status");
    const telegram = data.telegram || {};

    if (!telegram.ok || telegram.paired) {
      banner.hidden = true;
      return;
    }

    banner.innerHTML =
      "<strong>Relier le téléphone</strong> — ouvre <code>@" +
      escapeHtml(telegram.bot) +
      "</code> sur Telegram et envoie&nbsp;: <code>/pair " +
      escapeHtml(telegram.pair_code || "") +
      "</code>";
    banner.hidden = false;
  } catch (error) {
    banner.hidden = true;
  }
}

/* ---------- Mails Outlook ---------- */

const mailButton = document.getElementById("mail-read");

/* Le bouton n'apparait que si la lecture des mails est activee dans Config. */
async function refreshMailButton() {
  try {
    const data = await callApi("/api/mail/status");
    mailButton.hidden = !data.enabled;
  } catch (error) {
    mailButton.hidden = true;
  }
}

mailButton.addEventListener("click", async () => {
  mailButton.disabled = true;
  const label = mailButton.textContent;
  mailButton.textContent = "Lecture en cours…";
  try {
    const data = await callApi("/api/mail/read", { method: "POST" });
    toast(data.count
      ? data.count + " mail(s) déposé(s), à valider."
      : "Aucun nouveau mail utile.");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    mailButton.disabled = false;
    mailButton.textContent = label;
  }
});

/* ---------- Initialisation ---------- */

/* Hauteur des colonnes : l'ecran moins leur position de depart dans la page
   (sous le composeur, dont la hauteur varie) et une marge basse. Remesuree
   quand la fenetre ou le composeur changent de taille. */
const inboxEl = document.querySelector(".inbox");
function fitColumns() {
  const offset = inboxEl.getBoundingClientRect().top + window.scrollY + 16;
  inboxEl.style.setProperty("--inbox-offset", `${Math.round(offset)}px`);
}
new ResizeObserver(fitColumns).observe(document.getElementById("composer"));
window.addEventListener("resize", fitColumns);
fitColumns();

document.getElementById("header-date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

textEl.focus();
refresh();
refreshPairing();
setInterval(refreshPairing, 15000);
refreshMailButton();

// Alt+N depuis une autre page (static/js/shell.js) : arrive ici avec ?capturer=1.
if (new URLSearchParams(window.location.search).has("capturer")) {
  history.replaceState(null, "", window.location.pathname);
  document.getElementById("cp-text").focus();
}
