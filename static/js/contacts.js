/* NOMAIA — contacts : ce qu'on me doit, ce que je dois, par interlocuteur.
 *
 * Deux directions a ne jamais confondre :
 *   - waiting  « on me doit » : ce que j'attends de quelqu'un, donc a relancer.
 *   - owed     « je dois »    : mes propres taches rattachees a la personne,
 *                               affichees en lecture seule, modifiees ailleurs.
 *
 * Les noms, roles et sujets viennent de l'API : ils peuvent contenir du HTML
 * ecrit a la main ou produit par un modele de langage. Tout est donc construit
 * en DOM, jamais avec innerHTML.
 */

const { toast, callApi, formatDeadline } = window.J;

const errorEl = document.getElementById("ct-error");

const waitListEl = document.getElementById("ct-wait-list");
const waitCountEl = document.getElementById("ct-wait-count");
const personListEl = document.getElementById("ct-person-list");
const personCountEl = document.getElementById("ct-person-count");
const personFilterEl = document.getElementById("ct-person-filter");

const waitFormEl = document.getElementById("ct-wait-add");
const personFormEl = document.getElementById("ct-person-add");
const waitSaveEl = document.getElementById("ct-wa-save");
const personSaveEl = document.getElementById("ct-pe-save");

const modalEl = document.getElementById("ct-person-modal");
const sheetNameEl = document.getElementById("ct-sheet-name");
const sheetMetaEl = document.getElementById("ct-sheet-meta");
const sheetBodyEl = document.getElementById("ct-sheet-body");
const sheetErrorEl = document.getElementById("ct-sheet-error");
const sheetDeleteEl = document.getElementById("ct-sheet-delete");
const sheetEditEl = document.getElementById("ct-sheet-edit");
const sheetMergeEl = document.getElementById("ct-sheet-merge");

const waitFields = {
  person: document.getElementById("ct-wa-person"),
  subject: document.getElementById("ct-wa-subject"),
  due: document.getElementById("ct-wa-due"),
};

const personFields = {
  name: document.getElementById("ct-pe-name"),
  role: document.getElementById("ct-pe-role"),
  project: document.getElementById("ct-pe-project"),
  email: document.getElementById("ct-pe-email"),
  phone: document.getElementById("ct-pe-phone"),
  notes: document.getElementById("ct-pe-notes"),
};

const PRIORITY_LABELS = { 1: "Critique", 2: "Haute", 3: "Normale", 4: "Basse" };
const TASKS_URL = "/taches";

let people = [];
let waiting = [];
// Fiche ouverte : null quand la fenetre est fermee.
let openPersonId = null;
// Interlocuteur affiche dans la fiche : sert de base au formulaire de modification.
let openPerson = null;
// Lignes deja affichees : seule une ligne nouvelle rejoue l'apparition en cascade.
const shownWaiting = new Set();
const shownPeople = new Set();

/* ---------- Messages ---------- */

function showError(message) {
  if (!message) {
    errorEl.hidden = true;
    errorEl.textContent = "";
    return;
  }
  errorEl.textContent = message;
  errorEl.hidden = false;
}

function showSheetError(message) {
  if (!message) {
    sheetErrorEl.hidden = true;
    sheetErrorEl.textContent = "";
    return;
  }
  sheetErrorEl.textContent = message;
  sheetErrorEl.hidden = false;
}

/* ---------- Petits assembleurs DOM ---------- */

function makeNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function textOf(value) {
  return value == null ? "" : String(value).trim();
}

function emptyLine(message) {
  return makeNode("p", "empty", message);
}

/* Pose l'apparition en cascade sur une ligne jamais affichee ; renvoie le rang suivant. */
function markEntrance(node, shown, rank) {
  const key = node.dataset.id;
  if (shown.has(key)) return rank;
  shown.add(key);
  node.classList.add("anim-in");
  node.style.setProperty("--i", String(Math.min(rank, 12)));
  return rank + 1;
}

function initialOf(name) {
  const clean = textOf(name);
  return clean ? clean.charAt(0).toUpperCase() : "?";
}

function daysOpenOf(entry) {
  if (typeof entry.days_open === "number") return entry.days_open;
  const since = textOf(entry.since);
  if (!since) return 0;
  const start = new Date(since + "T00:00:00");
  if (Number.isNaN(start.getTime())) return 0;
  const gap = Math.floor((Date.now() - start.getTime()) / 86400000);
  return gap > 0 ? gap : 0;
}

function dayWord(count) {
  return count > 1 ? "jours" : "jour";
}

/* ---------- Zone « A relancer » ---------- */

function waitingMeta(entry, withPerson) {
  const bits = [];
  if (withPerson && textOf(entry.person_name)) bits.push(textOf(entry.person_name));
  if (textOf(entry.since)) bits.push("depuis le " + formatDeadline(entry.since));
  if (textOf(entry.due)) bits.push("attendu le " + formatDeadline(entry.due));
  return bits.join(" · ");
}

function waitingActions(entry) {
  const box = makeNode("div", "ct-wait-actions");

  const received = makeNode("button", "icon-btn", "Reçu");
  received.type = "button";
  received.dataset.action = "received";
  received.dataset.id = String(entry.id);
  box.appendChild(received);

  const dropped = makeNode("button", "icon-btn danger", "Abandonner");
  dropped.type = "button";
  dropped.dataset.action = "dropped";
  dropped.dataset.id = String(entry.id);
  box.appendChild(dropped);

  return box;
}

function waitingCard(entry, withPerson) {
  const days = daysOpenOf(entry);

  const card = makeNode("article", "ct-wait");
  if (entry.overdue) card.classList.add("is-overdue");
  card.dataset.id = String(entry.id);

  const counter = makeNode("span", "ct-wait-days");
  counter.appendChild(makeNode("strong", null, days));
  counter.appendChild(makeNode("span", null, dayWord(days)));
  card.appendChild(counter);

  const body = makeNode("div", "ct-wait-body");
  body.appendChild(makeNode("span", "ct-wait-subject", textOf(entry.subject) || "(sans sujet)"));

  const meta = waitingMeta(entry, withPerson);
  if (meta) body.appendChild(makeNode("span", "ct-wait-meta", meta));

  if (entry.overdue) {
    const tag = makeNode("span", "tag tag-late", "en retard");
    body.appendChild(tag);
  }

  if (entry.task_id) {
    const link = makeNode("a", "link", "tâche liée : " + (textOf(entry.task_title) || "#" + entry.task_id));
    link.href = TASKS_URL;
    body.appendChild(link);
  }

  card.appendChild(body);
  card.appendChild(waitingActions(entry));
  return card;
}

function renderWaiting() {
  // La plus ancienne d'abord : c'est celle qui coince depuis le plus longtemps.
  const sorted = waiting.slice().sort((a, b) => {
    const gap = daysOpenOf(b) - daysOpenOf(a);
    if (gap) return gap;
    return textOf(a.since).localeCompare(textOf(b.since));
  });

  waitCountEl.textContent = sorted.length ? String(sorted.length) : "0";
  waitListEl.replaceChildren();

  if (!sorted.length) {
    waitListEl.appendChild(emptyLine("Rien à relancer."));
    return;
  }

  const list = makeNode("div", "ct-wait-list");
  let rank = 0;
  sorted.forEach((entry) => {
    const card = waitingCard(entry, true);
    rank = markEntrance(card, shownWaiting, rank);
    list.appendChild(card);
  });
  waitListEl.appendChild(list);
}

/* ---------- Zone « Interlocuteurs » ---------- */

function personMeta(person) {
  const bits = [];
  if (textOf(person.role)) bits.push(textOf(person.role));
  // Tous ses projets, le principal en tete (ordre fourni par le serveur).
  const projets = (person.projects || []).map((projet) => textOf(projet.name)).filter(Boolean);
  if (projets.length) bits.push(projets.join(", "));
  else if (textOf(person.project_name)) bits.push(textOf(person.project_name));
  return bits.join(" · ");
}

/* Filtre « par projet » de la liste : un contact y figure s'il suit ce projet. */
function visiblePeople() {
  const projet = Number(personFilterEl.value) || null;
  if (!projet) return people;
  return people.filter((person) => (person.projects || []).some((item) => item.id === projet));
}

function personCard(person) {
  const card = makeNode("article", "ct-person");
  card.dataset.id = String(person.id);

  const open = makeNode("button", "ct-person-open");
  open.type = "button";
  open.dataset.action = "open";
  open.title = "Ouvrir la fiche";

  open.appendChild(makeNode("span", "ct-avatar", initialOf(person.name)));

  const body = makeNode("div", "ct-person-body");
  body.appendChild(makeNode("span", "ct-person-name", textOf(person.name) || "(sans nom)"));
  body.lastChild.insertAdjacentHTML("beforeend", window.J.spaceBadge(person.space));

  const meta = personMeta(person);
  if (meta) body.appendChild(makeNode("span", "ct-person-meta", meta));
  open.appendChild(body);

  const counters = makeNode("div", "ct-counters");
  const waitingCount = Number(person.waiting_count) || 0;
  const owedCount = Number(person.owed_count) || 0;

  const waitingTag = makeNode("span", "ct-counter is-waiting", "on me doit " + waitingCount);
  if (waitingCount) waitingTag.classList.add("is-on");
  counters.appendChild(waitingTag);

  const owedTag = makeNode("span", "ct-counter is-owed", "je dois " + owedCount);
  if (owedCount) owedTag.classList.add("is-on");
  counters.appendChild(owedTag);

  open.appendChild(counters);
  card.appendChild(open);

  const remove = makeNode("button", "icon-btn danger", "Supprimer");
  remove.type = "button";
  remove.dataset.action = "delete";
  card.appendChild(remove);

  return card;
}

function renderPeople() {
  const shown = visiblePeople();
  personCountEl.textContent = String(shown.length);
  personListEl.replaceChildren();

  if (!shown.length) {
    personListEl.appendChild(emptyLine(people.length
      ? "Aucun interlocuteur sur ce projet."
      : "Aucun interlocuteur. Ajoute la première personne ci-dessus."));
    return;
  }

  const list = makeNode("div", "ct-person-list");
  let rank = 0;
  shown.forEach((person) => {
    const card = personCard(person);
    rank = markEntrance(card, shownPeople, rank);
    list.appendChild(card);
  });
  personListEl.appendChild(list);
}

function fillPersonSelect() {
  const previous = waitFields.person.value;
  waitFields.person.replaceChildren();

  const head = makeNode("option", null, "Qui ?");
  head.value = "";
  waitFields.person.appendChild(head);

  people.forEach((person) => {
    const option = makeNode("option", null, textOf(person.name) || "Personne " + person.id);
    option.value = String(person.id);
    waitFields.person.appendChild(option);
  });

  // La saisie en cours n'est pas perdue par un rechargement de la liste.
  if (previous && people.some((person) => String(person.id) === previous)) {
    waitFields.person.value = previous;
  }
}

/* ---------- Chargement ---------- */

async function loadPeople() {
  const data = await callApi("/api/people");
  people = data.people || [];
  renderPeople();
  fillPersonSelect();
}

async function loadWaiting() {
  const data = await callApi("/api/waiting?status=open");
  waiting = data.waiting || [];
  renderWaiting();
}

personFilterEl.addEventListener("change", renderPeople);

async function reload() {
  try {
    await Promise.all([loadWaiting(), loadPeople()]);
    showError(null);
  } catch (error) {
    showError("Contacts indisponibles : " + error.message);
  }
}

/* ---------- Fiche d'un interlocuteur ---------- */

function taskRow(task) {
  const row = makeNode("article", "ct-task");
  row.dataset.priority = String(task.priority || 3);
  if (task.status === "done") row.classList.add("is-done");

  const body = makeNode("div", "ct-task-body");
  body.appendChild(makeNode("span", "ct-task-title", textOf(task.title) || "(sans titre)"));

  const bits = [];
  if (task.priority) bits.push("P" + task.priority + " · " + (PRIORITY_LABELS[task.priority] || ""));
  if (textOf(task.project_name)) bits.push(textOf(task.project_name));
  if (textOf(task.deadline)) bits.push("échéance " + formatDeadline(task.deadline));
  if (task.status === "done") bits.push("terminée");
  if (bits.length) body.appendChild(makeNode("span", "ct-task-meta", bits.join(" · ")));

  row.appendChild(body);
  return row;
}

function sheetSection(title, count) {
  const section = makeNode("section", "ct-sheet-section");
  const head = makeNode("div", "group-head");
  head.appendChild(makeNode("h3", "group-title", title));
  head.appendChild(makeNode("span", "group-count", String(count)));
  section.appendChild(head);
  return section;
}

function sheetContact(person) {
  const bits = [];
  if (textOf(person.email)) bits.push(textOf(person.email));
  if (textOf(person.phone)) bits.push(textOf(person.phone));
  const aliases = person.aliases || [];
  if (!bits.length && !textOf(person.notes) && !aliases.length) return null;

  const box = makeNode("div", "ct-sheet-contact");
  if (bits.length) box.appendChild(makeNode("p", "ct-sheet-line", bits.join(" · ")));
  if (aliases.length) {
    box.appendChild(makeNode("p", "ct-sheet-line", "Aussi cité : " + aliases.map(textOf).join(", ")));
  }
  if (textOf(person.notes)) box.appendChild(makeNode("p", "ct-sheet-notes", textOf(person.notes)));
  return box;
}

function renderSheet(detail) {
  const person = detail.person || {};
  const personWaiting = detail.waiting || [];
  const personOwed = detail.owed || [];

  openPersonId = person.id == null ? openPersonId : person.id;
  openPerson = person.id == null ? openPerson : person;
  setSheetActions(true);

  sheetNameEl.textContent = textOf(person.name) || "(sans nom)";
  sheetMetaEl.textContent = personMeta(person);
  sheetBodyEl.replaceChildren();

  const contact = sheetContact(person);
  if (contact) sheetBodyEl.appendChild(contact);

  const waitingSection = sheetSection("On me doit", personWaiting.length);
  if (!personWaiting.length) {
    waitingSection.appendChild(emptyLine("Rien en attente de sa part."));
  } else {
    const list = makeNode("div", "ct-wait-list");
    personWaiting.forEach((entry) => list.appendChild(waitingCard(entry, false)));
    waitingSection.appendChild(list);
  }
  sheetBodyEl.appendChild(waitingSection);

  const owedSection = sheetSection("Je dois", personOwed.length);
  if (!personOwed.length) {
    owedSection.appendChild(emptyLine("Aucune tâche de mon côté."));
  } else {
    const list = makeNode("div", "ct-task-list");
    personOwed.forEach((task) => list.appendChild(taskRow(task)));
    owedSection.appendChild(list);

    const hint = makeNode("p", "ct-sheet-hint");
    hint.appendChild(document.createTextNode("Lecture seule ici. "));
    const link = makeNode("a", "link", "Modifier dans les tâches");
    link.href = TASKS_URL;
    hint.appendChild(link);
    owedSection.appendChild(hint);
  }
  sheetBodyEl.appendChild(owedSection);
}

async function openSheet(personId) {
  openPersonId = Number(personId);
  showSheetError(null);
  sheetNameEl.textContent = "Chargement…";
  sheetMetaEl.textContent = "";
  sheetBodyEl.replaceChildren();
  if (!modalEl.open) modalEl.showModal();

  try {
    const detail = await callApi("/api/people/" + openPersonId);
    renderSheet(detail);
  } catch (error) {
    sheetNameEl.textContent = "Fiche indisponible";
    showSheetError(error.message);
  }
}

async function refreshSheet() {
  if (openPersonId == null || !modalEl.open) return;
  try {
    const detail = await callApi("/api/people/" + openPersonId);
    renderSheet(detail);
  } catch (error) {
    showSheetError(error.message);
  }
}

function closeSheet() {
  openPersonId = null;
  if (modalEl.open) modalEl.close();
}

/* Les actions de la fiche n'ont pas de sens pendant une modification ou une
 * fusion : le formulaire porte alors ses propres boutons. */
function setSheetActions(visible) {
  sheetEditEl.hidden = !visible;
  sheetMergeEl.hidden = !visible;
  sheetDeleteEl.hidden = !visible;
}

/* ---------- Modification d'un interlocuteur ---------- */

function formField(label, input) {
  const box = makeNode("div");
  const caption = makeNode("label", null, label);
  caption.htmlFor = input.id;
  box.appendChild(caption);
  box.appendChild(input);
  return box;
}

function textInput(id, value, maxLength, type) {
  const input = makeNode("input");
  input.type = type || "text";
  input.id = id;
  input.maxLength = maxLength;
  input.value = textOf(value);
  return input;
}

function formButtons(saveLabel, onCancel) {
  const actions = makeNode("div", "modal-actions");
  const cancel = makeNode("button", "btn-ghost", "Annuler");
  cancel.type = "button";
  cancel.addEventListener("click", onCancel);
  const save = makeNode("button", "btn-primary", saveLabel);
  save.type = "submit";
  actions.appendChild(cancel);
  actions.appendChild(save);
  return actions;
}

function cancelForm() {
  showSheetError(null);
  refreshSheet();
}

function renderEditForm(person) {
  showSheetError(null);
  setSheetActions(false);
  sheetBodyEl.replaceChildren();

  const form = makeNode("form", "ct-edit-form");
  form.autocomplete = "off";

  const name = textInput("ct-ed-name", person.name, 120);
  form.appendChild(formField("Nom", name));

  const role = textInput("ct-ed-role", person.role, 120);
  form.appendChild(formField("Rôle", role));

  // Projets suivis : une case par projet (ceux du formulaire d'ajout, rendus
  // cote serveur). Un contact intervient souvent sur plusieurs projets.
  const suivis = new Set((person.projects || []).map((projet) => projet.id));
  const picks = makeNode("div", "ct-project-picks");
  Array.from(personFields.project.options)
    .filter((option) => option.value)
    .forEach((option) => {
      const label = makeNode("label", "ct-project-pick");
      const box = makeNode("input");
      box.type = "checkbox";
      box.value = option.value;
      box.checked = suivis.has(Number(option.value));
      label.append(box, document.createTextNode(option.textContent));
      picks.appendChild(label);
    });
  form.appendChild(formField("Projets suivis", picks));

  const email = textInput("ct-ed-email", person.email, 200, "email");
  const phone = textInput("ct-ed-phone", person.phone, 40);
  const contactRow = makeNode("div", "modal-row");
  contactRow.appendChild(formField("Email", email));
  contactRow.appendChild(formField("Téléphone", phone));
  form.appendChild(contactRow);

  const aliases = textInput("ct-ed-aliases", (person.aliases || []).join(", "), 600);
  aliases.placeholder = "Paul M., M. Martin";
  form.appendChild(formField("Aussi cité sous (séparés par des virgules)", aliases));

  // Commun : visible dans les deux espaces (client devenu ami, collègue...).
  const space = makeNode("select");
  space.id = "ct-ed-space";
  [["pro", "Pro"], ["perso", "Perso"], ["commun", "Commun (les deux espaces)"]].forEach(([value, label]) => {
    const option = makeNode("option", null, label);
    option.value = value;
    option.selected = (person.space || "pro") === value;
    space.appendChild(option);
  });
  form.appendChild(formField("Espace", space));

  const notes = makeNode("textarea");
  notes.id = "ct-ed-notes";
  notes.rows = 3;
  notes.maxLength = 2000;
  notes.value = textOf(person.notes);
  form.appendChild(formField("Notes", notes));

  form.appendChild(formButtons("Enregistrer", cancelForm));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!name.value.trim()) {
      showSheetError("Le nom est obligatoire.");
      name.focus();
      return;
    }

    const payload = {
      name: name.value.trim(),
      role: role.value.trim() || null,
      project_ids: Array.from(picks.querySelectorAll("input:checked")).map((box) => Number(box.value)),
      email: email.value.trim() || null,
      phone: phone.value.trim() || null,
      aliases: aliases.value,
      notes: notes.value.trim() || null,
      space: space.value,
    };

    try {
      await callApi("/api/people/" + person.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await reload();
      await refreshSheet();
      toast("Interlocuteur modifié.");
    } catch (error) {
      showSheetError("Modification impossible : " + error.message);
    }
  });

  sheetBodyEl.appendChild(form);
  name.focus();
}

sheetEditEl.addEventListener("click", () => {
  if (openPerson) renderEditForm(openPerson);
});

/* ---------- Fusion de doublons ---------- */

function renderMergeForm(person) {
  showSheetError(null);
  const others = people.filter((item) => String(item.id) !== String(person.id));
  if (!others.length) {
    showSheetError("Aucun autre interlocuteur avec qui fusionner.");
    return;
  }

  setSheetActions(false);
  sheetBodyEl.replaceChildren();

  const form = makeNode("form", "ct-edit-form");
  form.appendChild(makeNode(
    "p",
    "ct-sheet-hint",
    "Choisis la fiche en double. Ses tâches et attentes passent sur " +
      (textOf(person.name) || "cette fiche") +
      ", ses champs complètent ceux qui sont vides, et son nom devient un alias : " +
      "le triage ne recréera pas le doublon.",
  ));

  const select = makeNode("select");
  select.id = "ct-mg-person";
  const head = makeNode("option", null, "Fiche à absorber…");
  head.value = "";
  select.appendChild(head);
  others.forEach((item) => {
    const meta = personMeta(item);
    const option = makeNode("option", null, textOf(item.name) + (meta ? " — " + meta : ""));
    option.value = String(item.id);
    select.appendChild(option);
  });
  form.appendChild(formField("Doublon", select));
  form.appendChild(formButtons("Fusionner", cancelForm));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const absorbed = others.find((item) => String(item.id) === select.value);
    if (!absorbed) {
      showSheetError("Choisis la fiche à absorber.");
      return;
    }
    const message =
      "Fusionner « " + textOf(absorbed.name) + " » dans « " + textOf(person.name) +
      " » ? La fiche « " + textOf(absorbed.name) + " » sera supprimée.";
    if (!confirm(message)) return;

    try {
      await callApi("/api/people/" + person.id + "/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merge_id: absorbed.id }),
      });
      await reload();
      await refreshSheet();
      toast("Fiches fusionnées.");
    } catch (error) {
      showSheetError("Fusion impossible : " + error.message);
    }
  });

  sheetBodyEl.appendChild(form);
  select.focus();
}

sheetMergeEl.addEventListener("click", () => {
  if (openPerson) renderMergeForm(openPerson);
});

/* ---------- Actions sur une attente ---------- */

async function setWaitingStatus(waitingId, status) {
  const label = status === "received" ? "Reçu, noté." : "Attente abandonnée.";
  try {
    await callApi("/api/waiting/" + waitingId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status }),
    });
    await reload();
    await refreshSheet();
    toast(label);
  } catch (error) {
    if (modalEl.open) {
      showSheetError("Mise à jour impossible : " + error.message);
    } else {
      showError("Mise à jour impossible : " + error.message);
    }
    toast(error.message, true);
  }
}

function bindWaitingActions(container) {
  container.addEventListener("click", (event) => {
    const button = event.target.closest('[data-action="received"], [data-action="dropped"]');
    if (!button) return;
    setWaitingStatus(button.dataset.id, button.dataset.action);
  });
}

bindWaitingActions(waitListEl);
bindWaitingActions(sheetBodyEl);

/* ---------- Liste des interlocuteurs ---------- */

personListEl.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;

  const card = button.closest(".ct-person");
  if (!card) return;

  const personId = card.dataset.id;
  const action = button.dataset.action;

  if (action === "open") {
    openSheet(personId);
    return;
  }

  if (action !== "delete") return;
  await removePerson(personId);
});

async function removePerson(personId) {
  const person = people.find((item) => String(item.id) === String(personId));
  const name = person ? textOf(person.name) || "cet interlocuteur" : "cet interlocuteur";
  const count = person ? Number(person.waiting_count) || 0 : 0;

  const detail = count
    ? " Ses " + count + " attente(s) en cours partent avec."
    : " Aucune attente en cours ne part avec.";

  if (!confirm("Supprimer " + name + " ?" + detail + " Les tâches sont conservées, simplement détachées.")) {
    return;
  }

  try {
    await callApi("/api/people/" + personId, { method: "DELETE" });
    if (String(openPersonId) === String(personId)) closeSheet();
    await reload();
    toast("Interlocuteur supprimé.");
  } catch (error) {
    showError("Suppression impossible : " + error.message);
    toast(error.message, true);
  }
}

sheetDeleteEl.addEventListener("click", () => {
  if (openPersonId == null) return;
  removePerson(openPersonId);
});

document.getElementById("ct-sheet-close").addEventListener("click", closeSheet);
modalEl.addEventListener("close", () => {
  openPersonId = null;
  openPerson = null;
});

/* ---------- Creation d'un interlocuteur ---------- */

personFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();

  const name = personFields.name.value.trim();
  if (!name) {
    showError("Le nom de l'interlocuteur est obligatoire.");
    personFields.name.focus();
    return;
  }

  const payload = {
    name: name,
    role: personFields.role.value.trim() || null,
    project_id: personFields.project.value ? Number(personFields.project.value) : null,
    email: personFields.email.value.trim() || null,
    phone: personFields.phone.value.trim() || null,
    notes: personFields.notes.value.trim() || null,
  };

  personSaveEl.disabled = true;
  try {
    await callApi("/api/people", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    // La saisie n'est vidée qu'une fois la creation confirmee par le serveur.
    personFields.name.value = "";
    personFields.role.value = "";
    personFields.email.value = "";
    personFields.phone.value = "";
    personFields.notes.value = "";
    personFields.project.value = "";
    showError(null);
    await reload();
    toast("Interlocuteur ajouté.");
  } catch (error) {
    showError("Ajout impossible : " + error.message);
  } finally {
    personSaveEl.disabled = false;
  }
});

/* ---------- Creation d'une attente ---------- */

waitFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();

  const personId = waitFields.person.value;
  const subject = waitFields.subject.value.trim();

  if (!personId) {
    showError("Choisis la personne dont tu attends quelque chose.");
    waitFields.person.focus();
    return;
  }
  if (!subject) {
    showError("Le sujet de l'attente est obligatoire.");
    waitFields.subject.focus();
    return;
  }

  const payload = {
    person_id: Number(personId),
    subject: subject,
    due: waitFields.due.value || null,
  };

  waitSaveEl.disabled = true;
  try {
    await callApi("/api/waiting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    waitFields.subject.value = "";
    waitFields.due.value = "";
    showError(null);
    await reload();
    await refreshSheet();
    toast("Attente enregistrée.");
  } catch (error) {
    showError("Ajout impossible : " + error.message);
  } finally {
    waitSaveEl.disabled = false;
  }
});

/* ---------- Initialisation ---------- */

document.getElementById("header-date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

reload().then(() => {
  // Lien profond /contacts?personne=<id> (palette Ctrl+K) : ouvre la fiche de l'interlocuteur.
  const params = new URLSearchParams(window.location.search);
  const personId = Number(params.get("personne"));
  if (!personId) return;
  history.replaceState(null, "", window.location.pathname);
  openSheet(personId);
});

// Rechargement a chaud : seules les lignes nouvelles rejouent l'entree, filtre
// et saisie en cours gardes ; fiche ouverte (fenetre modale) : differe.
J.live.onChange(reload);
