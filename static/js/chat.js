/* NOMAIA — chat : conversations separees avec l'assistant.
 *
 * Chaque conversation a son fil et son plan en attente. La conversation
 * affichee est memorisee dans l'adresse (?c=<id>) : un lien la rouvre.
 *
 * Tout le contenu renvoye par l'API est du texte produit par un modele de
 * langage : il est toujours pose via textContent, jamais via innerHTML.
 */

const { toast, callApi, formatMoment } = window.J;

const threadEl = document.getElementById("chat-thread");
const inputEl = document.getElementById("chat-input");
const formEl = document.getElementById("chat-form");
const sendEl = document.getElementById("chat-send");
const workingEl = document.getElementById("chat-working");
const workingLabelEl = document.getElementById("chat-working-label");
const warningEl = document.getElementById("chat-warning");
const pendingEl = document.getElementById("chat-pending");
const providerEl = document.getElementById("chat-provider");
const clearEl = document.getElementById("chat-clear");
const titleEl = document.getElementById("chat-title");
const convItemsEl = document.getElementById("conv-items");
const convFilterEl = document.getElementById("conv-filter");
const showArchivedEl = document.getElementById("conv-show-archived");
const archiveEl = document.getElementById("conv-archive");

const ROLE_LABELS = { user: "Moi", assistant: "NOMAIA" };

// Exemples proposes quand la conversation est vide : un clic remplit la saisie.
const EXAMPLES = [
  "Qu'est-ce que je fais maintenant ?",
  "Où en est ALPHA ?",
  "Décale le crash paie à demain",
];

const INPUT_MAX_HEIGHT = 190;

// Reponses au plan en attente : il n'y a pas de route dediee, les boutons
// envoient un message normal que le serveur reconnait.
const CONFIRM_MESSAGE = "oui";
const CANCEL_MESSAGE = "non";

let busy = false;
let workingTimer = null;

// Conversation affichee, et la liste connue des conversations.
let conversationId = Number(new URLSearchParams(window.location.search).get("c")) || null;
let conversations = [];
let telegramConversationId = null;
let currentArchived = false;
// Empreinte du fil et du plan affiches : le rechargement a chaud ne redessine
// que ce qui a change.
let threadKey = "";
let pendingKey = "";

/* ---------- Rendu du fil ---------- */

function messageNode(message) {
  // Le fournisseur parle d'« assistant », la feuille de style de « nomaia ».
  const role = message.role === "user" ? "user" : "nomaia";
  const node = document.createElement("div");
  node.className = "bubble " + role;

  const who = document.createElement("span");
  who.className = "bubble-who";
  who.textContent = ROLE_LABELS[message.role] || "NOMAIA";
  if (message.created_at) {
    const moment = document.createElement("span");
    moment.className = "bubble-time";
    moment.textContent = formatMoment(message.created_at);
    who.appendChild(moment);
  }
  node.appendChild(who);

  const text = document.createElement("div");
  text.className = "bubble-text";
  text.textContent = message.content == null ? "" : String(message.content);
  node.appendChild(text);

  const tools = message.tools || [];
  if (tools.length) {
    const box = document.createElement("div");
    box.className = "tool-tags";
    tools.forEach((tool) => {
      const tag = document.createElement("span");
      tag.className = "tool-tag";
      tag.textContent = tool;
      box.appendChild(tag);
    });
    node.appendChild(box);
  }

  // Elements cites par la reponse : on ouvre la tache, le projet ou la fiche.
  const sources = message.sources || [];
  if (sources.length) {
    const box = document.createElement("div");
    box.className = "source-links";
    box.setAttribute("aria-label", "Sources");
    sources.forEach((source) => {
      const link = document.createElement("a");
      link.className = "source-link";
      link.href = source.url;
      link.title = `${source.kind_label} : ${source.label}`;
      const kind = document.createElement("span");
      kind.className = "source-kind";
      kind.textContent = source.kind_label;
      link.append(kind, document.createTextNode(source.label));
      box.appendChild(link);
    });
    node.appendChild(box);
  }

  return node;
}

function errorNode(text) {
  const node = document.createElement("p");
  node.className = "error-box chat-error";
  node.textContent = text;
  return node;
}

function emptyNode() {
  const box = document.createElement("div");
  box.className = "chat-empty";

  const title = document.createElement("p");
  title.className = "chat-empty-title";
  title.textContent = "Demande-lui quelque chose.";
  box.appendChild(title);

  const chips = document.createElement("div");
  chips.className = "chips";
  EXAMPLES.forEach((example) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = example;
    chip.addEventListener("click", () => {
      inputEl.value = example;
      autoGrow();
      inputEl.focus();
    });
    chips.appendChild(chip);
  });
  box.appendChild(chips);

  return box;
}

/* animateReply : apres un envoi, seules les reponses qui suivent le dernier
 * message de l'utilisateur apparaissent en cascade. Ce message-la est deja a
 * l'ecran (pose par appendNode) : il ne rejoue pas son entree. */
function renderThread(messages, animateReply) {
  threadKey = JSON.stringify(messages || []);
  threadEl.replaceChildren();
  if (!messages || !messages.length) {
    threadEl.appendChild(emptyNode());
    return;
  }
  let animateFrom = messages.length;
  if (animateReply) {
    let lastUser = -1;
    messages.forEach((message, index) => {
      if (message.role === "user") lastUser = index;
    });
    animateFrom = lastUser + 1;
  }
  messages.forEach((message, index) => {
    const node = messageNode(message);
    if (index >= animateFrom) markNew(node, index - animateFrom);
    threadEl.appendChild(node);
  });
  scrollToBottom();
}

// Apparition en cascade (motion.css) ; --i decale chaque element suivant.
function markNew(node, rank) {
  node.classList.add("anim-in");
  node.style.setProperty("--i", String(rank || 0));
}

function appendNode(node) {
  // Le premier message chasse l'etat vide ou le message de chargement.
  const placeholder = threadEl.querySelector(".chat-empty, .empty");
  if (placeholder) placeholder.remove();
  markNew(node, 0);
  threadEl.appendChild(node);
  scrollToBottom();
}

function scrollToBottom() {
  threadEl.scrollTop = threadEl.scrollHeight;
}

function renderWarning(warning) {
  warningEl.textContent = warning || "";
  warningEl.hidden = !warning;
}

/* Hauteur occupee par l'avertissement (marge comprise), deduite de celle du
   fil : sans elle, la zone de saisie passe sous le bas de l'ecran. Suivie a
   chaque changement, le texte pouvant passer sur deux lignes. */
new ResizeObserver(() => {
  const hauteur = warningEl.hidden ? 0 : warningEl.offsetHeight + parseFloat(getComputedStyle(warningEl).marginBottom);
  document.documentElement.style.setProperty("--chat-warning-h", `${Math.round(hauteur)}px`);
}).observe(warningEl);

function renderProvider(provider) {
  providerEl.textContent = provider ? "moteur : " + provider : "—";
}

/* ---------- Plan en attente de confirmation ---------- */

/* NOMAIA peut enchainer plusieurs actions sur une seule phrase. Tant qu'il n'a
 * pas le feu vert, rien n'est applique : le plan s'affiche sous le fil, la
 * saisie reste libre et repondre autre chose l'abandonne. Les libelles viennent
 * du serveur, donc d'un modele de langage : textContent uniquement. */

let pendingPlan = null;
let confirmEl = null;
let cancelEl = null;

function normalizePending(pending) {
  if (!pending || !Array.isArray(pending.steps) || !pending.steps.length) return null;
  return pending;
}

function stepNode(step, index) {
  const item = document.createElement("li");
  item.className = "pending-step";

  const destructive = Boolean(step && step.destructive);
  if (destructive) {
    item.classList.add("is-destructive");
    // Marque textuelle : la couleur seule ne suffit pas a reperer le risque.
    const flag = document.createElement("span");
    flag.className = "pending-flag";
    flag.textContent = "Irréversible";
    flag.title = "Action destructive";
    item.appendChild(flag);
  }

  const label = document.createElement("span");
  label.className = "pending-label";
  const fallback = step && step.tool ? String(step.tool) : "Action " + (index + 1);
  label.textContent = step && step.label ? String(step.label) : fallback;
  item.appendChild(label);

  return item;
}

function renderPending(pending) {
  pendingKey = JSON.stringify(pending || null);
  pendingPlan = normalizePending(pending);
  confirmEl = null;
  cancelEl = null;
  pendingEl.replaceChildren();

  if (!pendingPlan) {
    pendingEl.hidden = true;
    return;
  }

  const title = document.createElement("p");
  title.className = "pending-title";
  title.textContent = "Plan en attente de ton feu vert";
  pendingEl.appendChild(title);

  const list = document.createElement("ol");
  list.className = "pending-steps";
  pendingPlan.steps.forEach((step, index) => list.appendChild(stepNode(step, index)));
  pendingEl.appendChild(list);

  const actions = document.createElement("div");
  actions.className = "pending-actions";

  confirmEl = document.createElement("button");
  confirmEl.type = "button";
  confirmEl.className = "btn-primary";
  confirmEl.textContent = "Confirmer";
  confirmEl.addEventListener("click", () => send(CONFIRM_MESSAGE));
  actions.appendChild(confirmEl);

  cancelEl = document.createElement("button");
  cancelEl.type = "button";
  cancelEl.className = "btn-ghost";
  cancelEl.textContent = "Annuler";
  cancelEl.addEventListener("click", () => send(CANCEL_MESSAGE));
  actions.appendChild(cancelEl);

  pendingEl.appendChild(actions);

  const hint = document.createElement("p");
  hint.className = "pending-hint";
  hint.textContent = "Tu peux aussi répondre autre chose : le plan sera abandonné.";
  pendingEl.appendChild(hint);

  pendingEl.hidden = false;
  confirmEl.disabled = busy;
  cancelEl.disabled = busy;
  scrollToBottom();
}

function setPendingBusy(value) {
  if (confirmEl) confirmEl.disabled = value;
  if (cancelEl) cancelEl.disabled = value;
}

/* ---------- Zone de saisie ---------- */

function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, INPUT_MAX_HEIGHT) + "px";
}

inputEl.addEventListener("input", autoGrow);

inputEl.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  formEl.requestSubmit();
});

/* Telephone : le clavier reduit la zone visible sans redimensionner la page.
 * On ramene alors la saisie a l'ecran, sans defilement si elle y est deja. */
const phoneQuery = window.matchMedia("(max-width: 860px)");

if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => {
    if (!phoneQuery.matches || document.activeElement !== inputEl) return;
    formEl.scrollIntoView({ block: "nearest" });
  });
}

/* ---------- Indicateur de travail ---------- */

function setBusy(value) {
  busy = value;
  inputEl.disabled = value;
  sendEl.disabled = value;
  clearEl.disabled = value;
  workingEl.hidden = !value;
  setPendingBusy(value);

  clearInterval(workingTimer);
  if (!value) return;

  // Un tour peut durer une minute : le compteur prouve que ca travaille.
  const start = Date.now();
  workingLabelEl.textContent = "NOMAIA réfléchit…";
  workingTimer = setInterval(() => {
    const seconds = Math.round((Date.now() - start) / 1000);
    workingLabelEl.textContent = "NOMAIA réfléchit… " + seconds + " s";
  }, 1000);
}

/* ---------- Envoi ---------- */

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});

/* Appele sans argument depuis le formulaire, avec un texte depuis les boutons
 * du plan en attente : dans ce cas la zone de saisie n'est pas touchee. */
async function send(forced) {
  if (busy) return;

  const fromInput = forced == null;
  const message = (fromInput ? inputEl.value : forced).trim();
  if (!message) return;

  if (fromInput) {
    inputEl.value = "";
    autoGrow();
  }

  const sent = messageNode({ role: "user", content: message });
  appendNode(sent);
  setBusy(true);

  try {
    const data = await callApi("/api/chat" + archivedQuery("?"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: conversationId }),
    });

    applyConversations(data);
    if (data.conversation) showConversation(data.conversation);

    if (data.messages) {
      renderThread(data.messages, true);
    } else {
      // Repli si le serveur ne renvoie que la reponse du tour.
      appendNode(messageNode({ role: "assistant", content: data.reply, tools: data.tools, sources: data.sources }));
    }
    // Le serveur decide : un nouveau plan remplace l'ancien, son absence l'efface.
    renderPending(data.pending);
    scrollToBottom();
  } catch (error) {
    // Le message n'est pas parti : on le retire du fil et, s'il venait de la
    // saisie, on le rend a l'utilisateur pour qu'il ne perde pas ce qu'il a
    // ecrit. Le plan en attente, lui, reste affiche : rien n'a ete applique.
    sent.remove();
    let note = "Échec de l'envoi : " + error.message;
    if (fromInput) {
      inputEl.value = message;
      autoGrow();
      note += " Ton message est revenu dans la zone de saisie.";
    }
    appendNode(errorNode(note));
  } finally {
    setBusy(false);
    inputEl.focus();
  }
}

/* ---------- Effacement ---------- */

clearEl.addEventListener("click", async () => {
  if (busy) return;
  if (!confirm("Vider cette conversation ? NOMAIA oubliera ce fil.")) return;

  try {
    await callApi("/api/chat?conversation_id=" + conversationId, { method: "DELETE" });
    renderThread([]);
    renderWarning(null);
    renderPending(null);
    toast("Conversation effacée.");
  } catch (error) {
    toast(error.message, true);
  }
  inputEl.focus();
});

/* ---------- Chargement initial ---------- */

/* ---------- Conversations ---------- */

function showConversation(conversation) {
  conversationId = conversation.id;
  currentArchived = Boolean(conversation.archived);
  archiveEl.textContent = currentArchived ? "Désarchiver" : "Archiver";
  titleEl.textContent = conversation.title || "Conversation";
  const url = new URL(window.location.href);
  url.searchParams.set("c", conversation.id);
  window.history.replaceState(null, "", url);
  renderConversations();
}

function applyConversations(data) {
  if (data.conversations) conversations = data.conversations;
  if ("telegram_conversation_id" in data) telegramConversationId = data.telegram_conversation_id;
}

function conversationItem(conversation) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "conv-item" + (conversation.id === conversationId ? " is-active" : "");
  item.dataset.id = conversation.id;

  const title = document.createElement("span");
  title.className = "conv-title";
  title.textContent = conversation.title || "Nouvelle conversation";
  title.insertAdjacentHTML("beforeend", window.J.spaceBadge(conversation.space));
  item.appendChild(title);

  const meta = document.createElement("span");
  meta.className = "conv-meta";
  meta.textContent = formatMoment(conversation.updated_at) + " · " + conversation.message_count + " msg" +
    (conversation.archived ? " · archivée" : "");
  if (conversation.archived) item.classList.add("is-archived");
  if (conversation.id === telegramConversationId) {
    const badge = document.createElement("span");
    badge.className = "conv-badge";
    badge.textContent = "Telegram";
    badge.title = "Conversation suivie depuis Telegram";
    meta.appendChild(badge);
  }
  item.appendChild(meta);

  if (conversation.last_message) {
    const last = document.createElement("span");
    last.className = "conv-last";
    last.textContent = conversation.last_message;
    item.appendChild(last);
  }
  return item;
}

function renderConversations() {
  const filtre = convFilterEl.value.trim().toLowerCase();
  const visibles = conversations.filter((conversation) =>
    !filtre ||
    (conversation.title || "").toLowerCase().includes(filtre) ||
    (conversation.last_message || "").toLowerCase().includes(filtre)
  );
  convItemsEl.replaceChildren();
  if (!visibles.length) {
    const vide = document.createElement("p");
    vide.className = "empty";
    vide.textContent = filtre ? "Aucune conversation trouvée." : "Aucune conversation.";
    convItemsEl.appendChild(vide);
    return;
  }
  visibles.forEach((conversation) => convItemsEl.appendChild(conversationItem(conversation)));
}

convFilterEl.addEventListener("input", renderConversations);

function archivedQuery(separator) {
  return showArchivedEl.checked ? separator + "archived=1" : "";
}

showArchivedEl.addEventListener("change", async () => {
  try {
    applyConversations(await callApi("/api/conversations" + archivedQuery("?")));
    renderConversations();
  } catch (error) {
    toast(error.message, true);
  }
});

/* Telephone : la liste des conversations se replie derriere un bouton. */
function setConversationsOpen(open) {
  const liste = document.querySelector(".conv-list");
  liste.classList.toggle("is-open", open);
  document.getElementById("conv-toggle").setAttribute("aria-expanded", String(open));
}

document.getElementById("conv-toggle").addEventListener("click", () => {
  setConversationsOpen(!document.querySelector(".conv-list").classList.contains("is-open"));
});

convItemsEl.addEventListener("click", (event) => {
  const item = event.target.closest(".conv-item");
  if (!item || busy) return;
  setConversationsOpen(false);
  const cible = Number(item.dataset.id);
  if (cible === conversationId) return;
  conversationId = cible;
  load();
});

document.getElementById("conv-new").addEventListener("click", async () => {
  if (busy) return;
  try {
    const conversation = await callApi("/api/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    conversationId = conversation.id;
    setConversationsOpen(false);
    await load();
    inputEl.focus();
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("conv-rename").addEventListener("click", async () => {
  if (busy || !conversationId) return;
  const titre = prompt("Nouveau titre de la conversation :", titleEl.textContent);
  if (titre == null || !titre.trim()) return;
  try {
    const conversation = await callApi("/api/conversations/" + conversationId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: titre }),
    });
    conversations = conversations.map((item) => (item.id === conversation.id ? { ...item, title: conversation.title } : item));
    showConversation(conversation);
  } catch (error) {
    toast(error.message, true);
  }
});

archiveEl.addEventListener("click", async () => {
  if (busy || !conversationId) return;
  const archiver = !currentArchived;
  try {
    await callApi("/api/conversations/" + conversationId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archived: archiver }),
    });
    if (archiver) conversationId = null;
    await load();
    toast(archiver
      ? "Conversation archivée : « Afficher les archivées » la retrouve."
      : "Conversation désarchivée.");
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("conv-delete").addEventListener("click", async () => {
  if (busy || !conversationId) return;
  if (!confirm("Supprimer définitivement cette conversation et tout son fil ?")) return;
  try {
    await callApi("/api/conversations/" + conversationId, { method: "DELETE" });
    conversationId = null;
    await load();
    toast("Conversation supprimée.");
  } catch (error) {
    toast(error.message, true);
  }
});

/* ---------- Chargement ---------- */

async function load() {
  try {
    let data;
    try {
      data = await callApi("/api/chat" + (conversationId
        ? "?conversation_id=" + conversationId + archivedQuery("&")
        : archivedQuery("?")));
    } catch (error) {
      // Conversation supprimee entre-temps : on retombe sur la courante.
      if (!conversationId) throw error;
      conversationId = null;
      data = await callApi("/api/chat");
    }
    applyConversations(data);
    if (data.conversation) showConversation(data.conversation);
    renderProvider(data.provider);
    renderWarning(data.warning);
    renderThread(data.messages);
    // Un plan peut attendre depuis un autre message, Telegram compris.
    renderPending(data.pending);
  } catch (error) {
    threadEl.replaceChildren(errorNode("Historique indisponible : " + error.message));
  }
}

document.getElementById("header-date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

/* ---------- Rechargement a chaud ---------- */

/* Liste des conversations et fil courant relus quand la base change (message
 * venu de Telegram, session Claude...). Differe pendant une reponse en cours.
 * Fil et plan ne sont redessines que s'ils ont change, sans cascade ; la
 * position de lecture est gardee, sauf si l'on etait deja en bas du fil. */
async function liveReload() {
  if (busy) return false;
  const shown = conversationId;
  let data;
  try {
    data = await callApi("/api/chat" + (shown
      ? "?conversation_id=" + shown + archivedQuery("&")
      : archivedQuery("?")));
  } catch (error) {
    return true; // conversation supprimee ailleurs ou serveur injoignable : l'affichage reste
  }
  // Une reponse a pu partir, ou une autre conversation s'ouvrir, pendant l'appel.
  if (busy) return false;
  if (conversationId !== shown) return true;
  applyConversations(data);
  if (data.conversation) showConversation(data.conversation);
  else renderConversations();
  renderProvider(data.provider);
  renderWarning(data.warning);
  if (JSON.stringify(data.messages || []) !== threadKey) {
    const atBottom = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 40;
    const top = threadEl.scrollTop;
    renderThread(data.messages);
    if (!atBottom) threadEl.scrollTop = top;
  }
  if (JSON.stringify(data.pending || null) !== pendingKey) renderPending(data.pending);
  return true;
}

autoGrow();
inputEl.focus();
load();
J.live.onChange(liveReload);
