/* NOMAIA — configuration : moteur, economie de jetons, seuils, horaires, systeme. */

const { toast, callApi, escapeHtml, formatDuration } = window.J;

const PROVIDER_HELP = {
  claude_cli:
    "Passe par ton abonnement Claude Code : aucun coût supplémentaire, mais chaque appel " +
    "recharge le prompt système du CLI, soit environ 40 000 jetons par triage.",
  anthropic:
    "Appel direct à l'API Claude : seul le prompt de NOMAIA est envoyé, environ 2 000 jetons " +
    "par triage. Le bloc d'instructions est mis en cache et facturé au dixième ensuite.",
  gemini:
    "Appel direct à l'API Gemini de Google. Tarifs les plus bas du comparatif, vision incluse.",
  ollama:
    "Modèle local, aucun jeton facturé et aucune donnée envoyée sur Internet. " +
    "Qualité d'extraction nettement inférieure et 8 Go de VRAM occupés.",
};

let state = { settings: {}, secrets: {}, providers: [] };

/* ---------- Chargement ---------- */

async function reload() {
  state = await callApi("/api/settings");
  renderProviders();
  fillForm();
  renderSystem();
  await reloadUsage();
}

function providerById(id) {
  return state.providers.find((item) => item.id === id);
}

function renderProviders() {
  const select = document.getElementById("cf-provider");
  select.innerHTML = state.providers
    .map(
      (item) =>
        '<option value="' + item.id + '"' +
        (item.id === state.settings.llm_provider ? " selected" : "") +
        ">" + escapeHtml(item.label) + "</option>"
    )
    .join("");
  renderModels();
  renderChatProviders();
}

function renderChatProviders() {
  // Le chat a son propre moteur : un tour enchaine plusieurs appels, ce que le
  // moteur du triage ne supporte pas toujours economiquement.
  const select = document.getElementById("cf-chat-provider");
  if (!select) return;
  select.innerHTML = state.providers
    .map(
      (item) =>
        '<option value="' + item.id + '"' +
        (item.id === state.settings.chat_provider ? " selected" : "") +
        ">" + escapeHtml(item.label) + "</option>"
    )
    .join("");
}

// Avertissement sur le modele choisi. Volontairement separe de renderModels :
// cette derniere reconstruit les options, et l'appeler sur un changement de
// selection remettait l'ancienne valeur, donc empechait tout changement.
function renderModelWarning() {
  const alerte = document.getElementById("model-warning");
  if (!alerte) return;

  const providerId = document.getElementById("cf-provider").value;
  const modeles = providerById(providerId)?.models || [];
  const choisi = document.getElementById("cf-model").value;
  const connu = modeles.find((model) => model.id === choisi);

  if (choisi && !connu) {
    alerte.textContent =
      "« " + choisi + " » n'est pas installé. Lance : ollama pull " + choisi +
      " — ou choisis un modèle de la liste.";
    alerte.hidden = false;
  } else if (providerId === "ollama" && !modeles.length) {
    alerte.textContent =
      "Ollama ne répond pas : la liste ci-dessus est celle des modèles connus," +
      " pas de ceux que tu as. Démarre Ollama puis recharge la page.";
    alerte.hidden = false;
  } else if (connu && connu.vision === false) {
    alerte.textContent =
      "Ce modèle ne lit pas les images : les captures d'écran déposées ne seront" +
      " pas analysées. Le texte, lui, fonctionne.";
    alerte.hidden = false;
  } else {
    alerte.hidden = true;
  }
}


function renderModels() {
  const providerId = document.getElementById("cf-provider").value;
  const provider = providerById(providerId);
  const select = document.getElementById("cf-model");
  const courant = state.settings["model_" + providerId];

  const modeles = provider?.models || [];

  // Un modele configure mais absent du poste doit rester visible et signale.
  // Sans cette entree, le select retombait sur le premier de la liste : on
  // croyait le reglage bon, et le triage echouait en 404 a chaque capture.
  const inconnu = courant && !modeles.some((model) => model.id === courant);
  let html = inconnu
    ? '<option value="' + escapeHtml(courant) + '" selected>' +
      escapeHtml(courant) + " — NON INSTALLE</option>"
    : "";

  html += modeles
    .map((model) => {
      const prix = model.in
        ? " — " + model.in + " $ / " + model.out + " $ par million"
        : " — inclus";
      return (
        '<option value="' + escapeHtml(model.id) + '"' +
        (model.id === courant ? " selected" : "") + ">" +
        escapeHtml(model.label) + prix + "</option>"
      );
    })
    .join("");

  select.innerHTML = html;

  renderModelWarning();

  document.getElementById("provider-help").textContent = PROVIDER_HELP[providerId] || "";

  const bloc = document.getElementById("cf-key-block");
  bloc.hidden = !provider?.needs_key;

  if (provider?.needs_key) {
    const secret = state.secrets[provider.key_setting] || {};
    document.getElementById("cf-key-state").textContent = secret.defined
      ? "enregistrée " + secret.hint
      : "absente";
    document.getElementById("cf-key-state").classList.toggle("ok", Boolean(secret.defined));
    document.getElementById("cf-key").value = "";
  }
}

function fillForm() {
  const s = state.settings;
  const set = (id, value) => { document.getElementById(id).value = value ?? ""; };

  set("cf-timeout", s.llm_timeout_s);
  set("cf-claude-path", s.claude_path);
  set("cf-claude-dir", s.claude_config_dir);
  set("cf-ctx-tasks", s.context_task_limit);
  set("cf-ctx-turns", s.context_history_turns);
  set("cf-chat-turns", s.chat_history_turns);
  set("cf-chat-steps", s.chat_max_steps);
  set("cf-chat-tg", s.telegram_chat_mode);
  set("cf-chat-chars", s.telegram_capture_chars);
  set("cf-foresight", s.foresight_enabled);
  set("cf-risk-horizon", s.risk_horizon_days);
  set("cf-validation-hours", s.validation_after_hours);
  set("cf-dormant-days", s.dormant_after_days);
  set("cf-idle-days", s.project_idle_days);
  set("cf-voice", s.voice_enabled);
  set("cf-voice-model", s.voice_model);
  set("cf-voice-lang", s.voice_language);
  set("cf-voice-max", s.voice_max_s);
  set("cf-auto", s.auto_apply_confidence);
  set("cf-manual", s.manual_confidence);
  set("cf-calibration", s.estimate_calibration);
  set("cf-work-start", s.work_start);
  set("cf-work-end", s.work_end);
  set("cf-lunch-start", s.lunch_start);
  set("cf-lunch-end", s.lunch_end);
  set("cf-perso-enabled", s.perso_hours_enabled);
  set("cf-perso-start", s.perso_start);
  set("cf-perso-end", s.perso_end);
  set("cf-perso-offday-start", s.perso_offday_start);
  set("cf-perso-offday-end", s.perso_offday_end);
  set("cf-buffer", s.plan_buffer_pct);
  set("cf-horizon", s.plan_horizon_days);
  set("cf-hb-enabled", s.heartbeat_enabled);
  set("cf-hb-morning", s.morning_brief_at);
  set("cf-hb-evening", s.evening_brief_at);
  set("cf-drift", s.drift_alert_days);
  set("cf-ev-reminder", s.event_reminder_enabled);
  set("cf-ev-delay", s.event_reminder_min);
  set("cf-extract", s.extract_people);
  set("cf-nudge-after", s.nudge_after_days);
  set("cf-nudge-quiet", s.nudge_quiet_days);
  set("cf-auto-project", s.auto_create_project);
  set("cf-tg-digest", s.telegram_pinned_digest);
  set("cf-cal-sync", s.calendar_sync_min);
  set("cf-odoo-url", s.odoo_url);
  set("cf-odoo-db", s.odoo_db);
  set("cf-odoo-login", s.odoo_login);
  set("cf-odoo-protocol", s.odoo_protocol);
  set("cf-mail-enabled", s.mail_enabled);

  const cleOdoo = state.secrets.odoo_api_key || {};
  const etatCle = document.getElementById("cf-odoo-key-state");
  etatCle.textContent = cleOdoo.defined ? "enregistrée " + cleOdoo.hint : "absente";
  etatCle.classList.toggle("ok", Boolean(cleOdoo.defined));

  fillWorkDays(s.work_days);
  fillHolidayCountries(s.holiday_country);
  fillSpaceColors(s);

  const telegram = state.status.telegram || {};
  document.getElementById("tg-state").textContent = telegram.ok
    ? "Bot @" + (telegram.bot || "?") + (telegram.paired
        ? " — téléphone appairé."
        : " — non appairé, code : " + (telegram.pair_code || "?"))
    : "Passerelle inactive : " + (telegram.detail || "jeton absent.");

  const jeton = state.secrets.telegram_token || {};
  document.getElementById("cf-tg-key-state").textContent = jeton.defined
    ? "enregistré " + jeton.hint
    : "absent";
  document.getElementById("cf-tg-key-state").classList.toggle("ok", Boolean(jeton.defined));
}

/* ---------- Couleurs des espaces ---------- */

/* Couleurs d'origine, identiques aux defauts de models/setting.py et shell.css. */
const SPACE_COLOR_DEFAULTS = {
  pro: { from: "#6366f1", to: "#22d3ee" },
  perso: { from: "#f59e0b", to: "#ec4899" },
};

function spaceColorInput(space, edge) {
  return document.getElementById(`cf-space-${space}-${edge}`);
}

/* Apercu du degrade d'un espace, d'apres les deux champs couleur. */
function renderSpacePreview(space) {
  const debut = spaceColorInput(space, "from").value;
  const fin = spaceColorInput(space, "to").value;
  document.getElementById(`cf-space-${space}-preview`).style.background =
    `linear-gradient(90deg, ${debut}, ${fin})`;
}

function fillSpaceColors(s) {
  for (const space of Object.keys(SPACE_COLOR_DEFAULTS)) {
    for (const edge of ["from", "to"]) {
      spaceColorInput(space, edge).value =
        s[`space_${space}_color_${edge}`] || SPACE_COLOR_DEFAULTS[space][edge];
    }
    renderSpacePreview(space);
  }
}

document.querySelectorAll("[data-space-colors]").forEach((row) => {
  row.addEventListener("input", () => renderSpacePreview(row.dataset.spaceColors));
});

/* Applique les couleurs enregistrees a la page ouverte, sans rechargement :
   meme variables que la balise <style> de layout.html. */
function applySpaceColors() {
  const root = document.documentElement.style;
  for (const space of Object.keys(SPACE_COLOR_DEFAULTS)) {
    for (const edge of ["from", "to"]) {
      root.setProperty(`--space-${space}-${edge}`, state.settings[`space_${space}_color_${edge}`]);
    }
  }
}

/* Remet les champs aux couleurs d'origine ; l'enregistrement reste explicite. */
document.getElementById("cf-space-reset").addEventListener("click", () => {
  fillSpaceColors({});
});

/* ---------- Jours travailles et feries ---------- */

function workDayBoxes() {
  return Array.from(document.querySelectorAll("#cf-work-days input[type=checkbox]"));
}

// work_days arrive sous forme "0,1,2,3,4" (lundi = 0).
function fillWorkDays(value) {
  const jours = String(value ?? "").split(",").map((part) => part.trim());
  workDayBoxes().forEach((box) => { box.checked = jours.includes(box.value); });
}

function readWorkDays() {
  return workDayBoxes()
    .filter((box) => box.checked)
    .map((box) => Number(box.value))
    .sort((a, b) => a - b)
    .join(",");
}

function fillHolidayCountries(courant) {
  const pays = state.holiday_countries || [];
  const select = document.getElementById("cf-holiday-country");
  select.innerHTML =
    '<option value="">Aucun</option>' +
    pays
      .map(
        (item) =>
          '<option value="' + escapeHtml(item.id) + '">' + escapeHtml(item.label) + "</option>"
      )
      .join("");
  select.value = courant ?? "";
}

// Le serveur refuse une semaine sans jour travaille : on empeche de decocher le dernier.
document.getElementById("cf-work-days").addEventListener("change", (event) => {
  if (!event.target.matches("input[type=checkbox]")) return;
  if (!readWorkDays()) {
    event.target.checked = true;
    toast("Garde au moins un jour travaillé.", true);
  }
});

function renderSystem() {
  const s = state.system;
  const llm = state.status.llm || {};

  document.getElementById("system-facts").innerHTML =
    [
      ["Version", s.version],
      ["Moteur actif", (llm.label || "?") + " · " + (llm.model || "—")],
      ["État du moteur", llm.ok ? "opérationnel" : "indisponible : " + llm.detail],
      ["Worker de triage", state.status.worker_running ? "en marche" : "arrêté"],
      ["Rendez-vous quotidiens", state.status.heartbeat_running ? "en marche" : "arrêtés"],
      ["Transcription vocale", state.status.voice_available ? "disponible" : "faster-whisper absent"],
      ["Données", s.data_dir],
      ["Base", s.db_size_kb + " Ko"],
      ["Sauvegardes", s.backups],
    ]
      .map(
        ([cle, valeur]) =>
          "<dt>" + escapeHtml(cle) + "</dt><dd>" + escapeHtml(String(valeur)) + "</dd>"
      )
      .join("");
}

/* ---------- Consommation ---------- */

function formatTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + " M";
  if (n >= 1000) return Math.round(n / 1000) + " k";
  return String(n);
}

async function reloadUsage() {
  const data = await callApi("/api/usage?days=30");
  const t = data.total;

  document.getElementById("usage-stats").innerHTML = [
    [t.calls, "Appels sur 30 j"],
    [formatTokens(t.input_tokens + t.output_tokens), "Jetons consommés"],
    [formatTokens(t.cached_tokens), "Jetons servis du cache"],
    [formatTokens(t.avg_tokens), "Moyenne par appel"],
    ["$ " + t.cost_usd.toFixed(3), "Coût estimé"],
  ]
    .map(
      ([valeur, label]) =>
        '<div class="stat"><span class="stat-value">' + escapeHtml(String(valeur)) +
        '</span><span class="stat-label">' + label + "</span></div>"
    )
    .join("");

  const detail = document.getElementById("usage-detail");
  if (!data.by_provider.length) {
    detail.innerHTML =
      '<p class="empty">Aucun appel enregistré. La consommation apparaîtra ici dès le premier triage.</p>';
    return;
  }

  const max = Math.max(...data.by_provider.map((row) => row.input_tokens + row.output_tokens), 1);

  detail.innerHTML =
    '<div class="review-head">Consommation par modèle — 30 derniers jours</div>' +
    data.by_provider
      .map((row) => {
        const total = row.input_tokens + row.output_tokens;
        return (
          '<div class="bar-row">' +
            '<span class="bar-label">' + escapeHtml(row.model || row.provider) + "</span>" +
            '<div class="bar-track"><div class="bar-fill" style="width:' +
              Math.round((total / max) * 100) + '%"></div></div>' +
            '<span class="bar-value">' + formatTokens(total) + "</span>" +
          "</div>" +
          '<p class="usage-note">' + row.calls + " appel(s) · entrée " +
            formatTokens(row.input_tokens) + " · sortie " + formatTokens(row.output_tokens) +
            (row.cached_tokens ? " · cache " + formatTokens(row.cached_tokens) : "") +
            " · $ " + (row.cost_usd || 0).toFixed(3) + "</p>"
        );
      })
      .join("");
}

/* ---------- Agendas externes ---------- */

// Noms et messages d'erreur viennent de l'utilisateur ou d'un serveur distant :
// la liste est construite en DOM (textContent), jamais en innerHTML.

const SOURCES_API = "/api/calendar-sources";
const DEFAULT_SOURCE_NAME = "Outlook";

// Imports de l'agenda Odoo : cle de l'option renvoyee par l'API -> reglage.
const ODOO_OPTIONS = [
  { key: "leaves", setting: "odoo_cal_leaves", label: "Mes congés validés" },
  { key: "pending", setting: "odoo_cal_pending", label: "Congés en attente (affichés, non bloquants)" },
  { key: "holidays", setting: "odoo_cal_holidays", label: "Fériés et fermetures de la société" },
  { key: "meetings", setting: "odoo_cal_meetings", label: "Réunions" },
];

function pluralItems(count) {
  const n = Number(count) || 0;
  return n + (n > 1 ? " éléments" : " élément");
}

// last_sync_at arrive en "AAAA-MM-JJ HH:MM", heure locale du poste.
function relativeStamp(stamp) {
  const moment = new Date(String(stamp).replace(" ", "T"));
  if (Number.isNaN(moment.getTime())) return String(stamp);
  const minutes = Math.round((Date.now() - moment.getTime()) / 60000);
  if (minutes < 1) return "à l'instant";
  if (minutes < 60) return "il y a " + minutes + " min";
  if (minutes < 24 * 60) return "il y a " + Math.floor(minutes / 60) + " h";
  return "le " + moment.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) +
    " à " + moment.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function sourceStatus(source) {
  if (!source.last_sync_at) return { text: "Jamais synchronisé.", ko: Boolean(source.last_error) };
  const quand = relativeStamp(source.last_sync_at);
  if (source.last_error) return { text: "Échec " + quand + " : " + source.last_error, ko: true };
  return { text: "Synchronisé " + quand + " · " + pluralItems(source.last_count), ko: false };
}

// Phrase de compte rendu d'une synchronisation renvoyee par l'API.
function describeSync(sync) {
  if (!sync) return "";
  return sync.ok
    ? pluralItems(sync.count) + " importé(s)."
    : "Synchronisation en échec : " + (sync.error || "erreur inconnue.");
}

function showSourcesError(message) {
  const box = document.getElementById("cal-error");
  box.textContent = message || "";
  box.hidden = !message;
}

function sourceRow(source) {
  const row = document.createElement("div");
  row.className = "cal-source";
  row.classList.toggle("is-off", !source.enabled);

  const body = document.createElement("div");
  body.className = "cal-source-body";

  const name = document.createElement("span");
  name.className = "cal-source-name";
  name.textContent = source.name ? String(source.name) : "(sans nom)";
  body.appendChild(name);

  const hint = document.createElement("span");
  hint.className = "cal-source-hint";
  if (source.kind === "odoo") {
    hint.textContent = "connexion de Connexions > Tâches Odoo";
  } else {
    hint.textContent = source.url_hint ? String(source.url_hint) : "adresse absente";
  }
  body.appendChild(hint);

  const status = sourceStatus(source);
  const state = document.createElement("span");
  state.className = "cal-source-state";
  state.classList.toggle("ko", status.ko);
  state.textContent = status.text;
  body.appendChild(state);

  row.appendChild(body);

  const actions = document.createElement("div");
  actions.className = "cal-source-actions";

  const toggle = document.createElement("label");
  toggle.className = "cal-switch";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(source.enabled);
  box.addEventListener("change", () => toggleSource(source, box));
  toggle.appendChild(box);
  toggle.appendChild(document.createTextNode(source.enabled ? "Actif" : "Inactif"));
  actions.appendChild(toggle);

  // Espace des evenements importes ; l'agenda Odoo reste professionnel.
  const space = document.createElement("select");
  space.className = "cal-space";
  space.setAttribute("aria-label", "Espace du calendrier " + name.textContent);
  [["pro", "Pro"], ["perso", "Perso"]].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = (source.space || "pro") === value;
    space.appendChild(option);
  });
  space.disabled = source.kind === "odoo";
  space.addEventListener("change", () => setSourceSpace(source, space));
  actions.appendChild(space);

  const sync = document.createElement("button");
  sync.type = "button";
  sync.className = "btn-ghost";
  sync.textContent = "Synchroniser";
  // Une source coupee n'est pas relue par le battement : on ne la relit pas non plus ici.
  sync.disabled = !source.enabled;
  sync.title = source.enabled ? "Relire le calendrier maintenant" : "Active le calendrier pour le relire";
  sync.addEventListener("click", () => syncSource(source, sync));
  actions.appendChild(sync);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "icon-btn danger";
  remove.title = "Supprimer";
  remove.setAttribute("aria-label", "Supprimer le calendrier " + name.textContent);
  remove.textContent = "✕";
  remove.addEventListener("click", () => removeSource(source, remove));
  actions.appendChild(remove);

  row.appendChild(actions);
  return row;
}

function renderSources(sources) {
  const list = document.getElementById("cal-sources");
  list.replaceChildren();
  if (!sources.length) {
    const nothing = document.createElement("p");
    nothing.className = "cal-empty";
    nothing.textContent = "Aucun agenda externe relié.";
    list.appendChild(nothing);
    return;
  }
  sources.forEach((source) => {
    list.appendChild(sourceRow(source));
    if (source.kind === "odoo") list.appendChild(odooOptions(source));
  });
}

/* Cases a cocher des imports Odoo, sous la ligne de la source. */
function odooOptions(source) {
  const box = document.createElement("div");
  box.className = "cal-odoo-options";
  const current = source.options || {};
  ODOO_OPTIONS.forEach((option) => {
    const label = document.createElement("label");
    label.className = "cal-switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(current[option.key]);
    input.addEventListener("change", () => toggleOdooOption(source, option, input));
    label.appendChild(input);
    label.appendChild(document.createTextNode(option.label));
    box.appendChild(label);
  });
  return box;
}

async function toggleOdooOption(source, option, input) {
  input.disabled = true;
  try {
    await callApi("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [option.setting]: input.checked ? "1" : "0" }),
    });
    // L'effet doit se voir tout de suite : la source active est relue.
    if (source.enabled) {
      const data = await callApi(SOURCES_API + "/" + source.id + "/sync", { method: "POST" });
      toast("Agenda Odoo mis à jour. " + describeSync(data.sync), Boolean(data.sync && !data.sync.ok));
    } else {
      toast("Réglage enregistré.");
    }
  } catch (error) {
    toast(error.message, true);
  }
  await loadSources();
}

function showOdooAvailable(available) {
  document.getElementById("cal-odoo-box").hidden = !available;
  document.getElementById("cal-odoo-help").hidden = !available;
}

async function loadSources() {
  try {
    const data = await callApi(SOURCES_API);
    renderSources(data.sources || []);
    showOdooAvailable(Boolean(data.odoo_available));
    showSourcesError(null);
  } catch (error) {
    document.getElementById("cal-sources").replaceChildren();
    showOdooAvailable(false);
    showSourcesError("Agendas externes indisponibles : " + error.message);
  }
}

async function toggleSource(source, box) {
  box.disabled = true;
  try {
    const data = await callApi(SOURCES_API + "/" + source.id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: box.checked }),
    });
    // La reactivation declenche une synchro cote serveur : son issue est rapportee.
    const issue = describeSync(data.sync);
    toast((box.checked ? "Calendrier activé." : "Calendrier désactivé.") + (issue ? " " + issue : ""),
      Boolean(data.sync && !data.sync.ok));
  } catch (error) {
    toast(error.message, true);
  }
  await loadSources();
}

async function setSourceSpace(source, select) {
  select.disabled = true;
  try {
    await callApi(SOURCES_API + "/" + source.id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ space: select.value }),
    });
    toast("Calendrier rangé dans l'espace " + select.selectedOptions[0].textContent + ".");
  } catch (error) {
    toast(error.message, true);
  }
  await loadSources();
}

async function syncSource(source, button) {
  button.disabled = true;
  button.textContent = "Lecture…";
  try {
    const data = await callApi(SOURCES_API + "/" + source.id + "/sync", { method: "POST" });
    toast(describeSync(data.sync) || "Synchronisation terminée.", Boolean(data.sync && !data.sync.ok));
  } catch (error) {
    toast(error.message, true);
  }
  await loadSources();
}

async function removeSource(source, button) {
  if (!confirm("Supprimer ce calendrier et tout ce qu'il a importé ?")) return;
  button.disabled = true;
  try {
    await callApi(SOURCES_API + "/" + source.id, { method: "DELETE" });
    toast("Calendrier supprimé.");
  } catch (error) {
    toast(error.message, true);
  }
  await loadSources();
}

function showAddResult(message, level) {
  const resultat = document.getElementById("cal-add-result");
  resultat.className = "config-result" + (level ? " " + level : "");
  resultat.textContent = message;
  resultat.hidden = !message;
}

document.getElementById("cal-add-odoo").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  bouton.disabled = true;
  showAddResult("Ajout et première lecture de l'agenda Odoo…", "");
  try {
    const data = await callApi(SOURCES_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "odoo", name: "Odoo" }),
    });
    const sync = data.sync || {};
    if (sync.ok) {
      showAddResult("Agenda Odoo ajouté : " + describeSync(sync), "ok");
    } else {
      showAddResult("Agenda Odoo ajouté, mais la première lecture a échoué : " +
        (sync.error || "erreur inconnue.") + " Vérifie la connexion Odoo puis relance « Synchroniser ».", "warn");
    }
    toast("Agenda Odoo ajouté.");
  } catch (error) {
    showAddResult(error.message, "ko");
  } finally {
    bouton.disabled = false;
  }
  await loadSources();
});

document.getElementById("cal-add").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  const nameEl = document.getElementById("cal-name");
  const urlEl = document.getElementById("cal-url");
  const name = nameEl.value.trim() || DEFAULT_SOURCE_NAME;
  const url = urlEl.value.trim();

  if (!url) {
    showAddResult("Colle le lien ICS publié depuis Outlook.", "ko");
    return;
  }

  bouton.disabled = true;
  showAddResult("Ajout et première lecture en cours…", "");
  try {
    const data = await callApi(SOURCES_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, url: url }),
    });
    // La source existe des ce point : le lien secret ne reste pas a l'ecran.
    urlEl.value = "";
    nameEl.value = DEFAULT_SOURCE_NAME;
    const sync = data.sync || {};
    if (sync.ok) {
      showAddResult("Calendrier ajouté : " + describeSync(sync), "ok");
    } else {
      // Echec de la synchro initiale : la source est creee, c'est un avertissement.
      showAddResult("Calendrier ajouté, mais la première lecture a échoué : " +
        (sync.error || "erreur inconnue.") + " Vérifie le lien puis relance « Synchroniser ».", "warn");
    }
    toast("Calendrier ajouté.");
    await loadSources();
  } catch (error) {
    // Saisie refusee : le lien reste en place pour etre corrige.
    showAddResult(error.message, "ko");
  } finally {
    bouton.disabled = false;
  }
});

/* ---------- Enregistrement ---------- */

const GROUPS = {
  moteur: () => {
    const providerId = document.getElementById("cf-provider").value;
    const provider = providerById(providerId);
    const payload = {
      llm_provider: providerId,
      ["model_" + providerId]: document.getElementById("cf-model").value,
      llm_timeout_s: document.getElementById("cf-timeout").value,
      claude_path: document.getElementById("cf-claude-path").value,
      claude_config_dir: document.getElementById("cf-claude-dir").value,
    };
    const cle = document.getElementById("cf-key").value.trim();
    if (provider?.needs_key && cle) payload[provider.key_setting] = cle;
    return payload;
  },
  economie: () => ({
    context_task_limit: document.getElementById("cf-ctx-tasks").value,
    context_history_turns: document.getElementById("cf-ctx-turns").value,
  }),
  conversation: () => ({
    chat_provider: document.getElementById("cf-chat-provider").value,
    chat_history_turns: document.getElementById("cf-chat-turns").value,
    chat_max_steps: document.getElementById("cf-chat-steps").value,
    telegram_chat_mode: document.getElementById("cf-chat-tg").value,
    telegram_capture_chars: document.getElementById("cf-chat-chars").value,
    voice_enabled: document.getElementById("cf-voice").value,
    voice_model: document.getElementById("cf-voice-model").value,
    voice_language: document.getElementById("cf-voice-lang").value,
    voice_max_s: document.getElementById("cf-voice-max").value,
  }),
  autonomie: () => ({
    auto_apply_confidence: document.getElementById("cf-auto").value,
    manual_confidence: document.getElementById("cf-manual").value,
    estimate_calibration: document.getElementById("cf-calibration").value,
  }),
  journee: () => {
    const workDays = readWorkDays();
    // Garde-fou : sans jour coche, rien n'est envoye (le serveur repondrait 400).
    if (!workDays) return null;
    return {
      work_start: document.getElementById("cf-work-start").value,
      work_end: document.getElementById("cf-work-end").value,
      lunch_start: document.getElementById("cf-lunch-start").value,
      lunch_end: document.getElementById("cf-lunch-end").value,
      perso_hours_enabled: document.getElementById("cf-perso-enabled").value,
      perso_start: document.getElementById("cf-perso-start").value,
      perso_end: document.getElementById("cf-perso-end").value,
      perso_offday_start: document.getElementById("cf-perso-offday-start").value,
      perso_offday_end: document.getElementById("cf-perso-offday-end").value,
      plan_buffer_pct: document.getElementById("cf-buffer").value,
      plan_horizon_days: document.getElementById("cf-horizon").value,
      work_days: workDays,
      holiday_country: document.getElementById("cf-holiday-country").value,
    };
  },
  proactivite: () => ({
    foresight_enabled: document.getElementById("cf-foresight").value,
    risk_horizon_days: document.getElementById("cf-risk-horizon").value,
    validation_after_hours: document.getElementById("cf-validation-hours").value,
    dormant_after_days: document.getElementById("cf-dormant-days").value,
    project_idle_days: document.getElementById("cf-idle-days").value,
  }),
  interlocuteurs: () => ({
    extract_people: document.getElementById("cf-extract").value,
    nudge_after_days: document.getElementById("cf-nudge-after").value,
    nudge_quiet_days: document.getElementById("cf-nudge-quiet").value,
  }),
  rappel: () => ({
    event_reminder_enabled: document.getElementById("cf-ev-reminder").value,
    event_reminder_min: document.getElementById("cf-ev-delay").value,
  }),
  rendezvous: () => ({
    heartbeat_enabled: document.getElementById("cf-hb-enabled").value,
    morning_brief_at: document.getElementById("cf-hb-morning").value,
    evening_brief_at: document.getElementById("cf-hb-evening").value,
    drift_alert_days: document.getElementById("cf-drift").value,
    auto_create_project: document.getElementById("cf-auto-project").value,
  }),
  agendas: () => ({
    calendar_sync_min: document.getElementById("cf-cal-sync").value,
  }),
  telegram: () => {
    const payload = { telegram_pinned_digest: document.getElementById("cf-tg-digest").value };
    const jeton = document.getElementById("cf-tg-token").value.trim();
    if (jeton) payload.telegram_token = jeton;
    return payload;
  },
  odoo: () => {
    const payload = {
      odoo_url: document.getElementById("cf-odoo-url").value.trim(),
      odoo_db: document.getElementById("cf-odoo-db").value.trim(),
      odoo_login: document.getElementById("cf-odoo-login").value.trim(),
      odoo_protocol: document.getElementById("cf-odoo-protocol").value,
    };
    const cle = document.getElementById("cf-odoo-key").value.trim();
    if (cle) payload.odoo_api_key = cle;
    return payload;
  },
  mail: () => ({
    mail_enabled: document.getElementById("cf-mail-enabled").value,
  }),
  espaces: () => ({
    space_pro_color_from: spaceColorInput("pro", "from").value,
    space_pro_color_to: spaceColorInput("pro", "to").value,
    space_perso_color_from: spaceColorInput("perso", "from").value,
    space_perso_color_to: spaceColorInput("perso", "to").value,
  }),
};

document.addEventListener("click", async (event) => {
  const bouton = event.target.closest("[data-save]");
  if (!bouton) return;

  const payload = GROUPS[bouton.dataset.save]();
  if (!payload) {
    toast("Rien à enregistrer.", true);
    return;
  }

  bouton.disabled = true;
  try {
    await callApi("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await reload();
    if (bouton.dataset.save === "odoo") loadSources();
    if (bouton.dataset.save === "mail") loadMail();
    if (bouton.dataset.save === "espaces") applySpaceColors();
    toast("Configuration enregistrée.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    bouton.disabled = false;
  }
});

document.getElementById("cf-provider").addEventListener("change", renderModels);
document.getElementById("cf-model").addEventListener("change", renderModelWarning);

document.getElementById("cf-test").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  const resultat = document.getElementById("cf-test-result");
  bouton.disabled = true;
  resultat.hidden = false;
  resultat.className = "config-result";
  resultat.textContent = "Appel en cours…";

  try {
    const data = await callApi("/api/settings/test", { method: "POST" });
    resultat.textContent = "Réponse obtenue en " + data.seconds + " s.";
    resultat.classList.add("ok");
    await reloadUsage();
  } catch (error) {
    resultat.textContent = error.message;
    resultat.classList.add("ko");
  } finally {
    bouton.disabled = false;
  }
});

/* ---------- Sessions Claude ---------- */

let agentSpaces = { pro: false, perso: false };

/* Libelle du bouton selon que l'espace choisi a deja une cle. */
function refreshAgentButton() {
  const space = document.getElementById("ag-space").value;
  document.getElementById("ag-generate").textContent = agentSpaces[space]
    ? "Nouvelle clé"
    : "Générer une clé";
}

async function loadAgentSetup() {
  const stateEl = document.getElementById("ag-state");
  try {
    const data = await callApi("/api/agent-setup");
    agentSpaces = data.spaces || { pro: data.defined, perso: false };
    const etat = (space) => (agentSpaces[space] ? "clé active" : "aucune clé");
    stateEl.textContent = `Pro : ${etat("pro")} · Perso : ${etat("perso")}.`
      + " Générer une nouvelle clé coupe les sessions enregistrées avec l'ancienne clé de cet espace.";
    refreshAgentButton();
    if (!data.script_exists) {
      stateEl.textContent += " Serveur introuvable : " + data.script;
    }
  } catch (error) {
    stateEl.textContent = error.message;
  }
}

document.getElementById("ag-generate").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  const space = document.getElementById("ag-space").value;
  if (agentSpaces[space] &&
      !confirm(`Remplacer la clé ${space} ? Les sessions enregistrées avec l'ancienne ne pourront plus écrire.`)) {
    return;
  }
  bouton.disabled = true;
  try {
    const data = await callApi("/api/agent-setup/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ space }),
    });
    document.getElementById("ag-command").value = data.command;
    document.getElementById("ag-remove").textContent = data.remove_command;
    document.getElementById("ag-command-box").hidden = false;
    document.getElementById("ag-copy").hidden = false;
    await loadAgentSetup();
loadMobile();
loadOdoo();
    toast("Clé générée : lance la commande dans un terminal.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    bouton.disabled = false;
  }
});

document.getElementById("ag-space").addEventListener("change", refreshAgentButton);

document.getElementById("ag-copy").addEventListener("click", async () => {
  const zone = document.getElementById("ag-command");
  try {
    await navigator.clipboard.writeText(zone.value);
    toast("Commande copiée.");
  } catch (error) {
    zone.select();
    toast("Sélectionne et copie avec Ctrl+C.", true);
  }
});

/* ---------- Taches Odoo ---------- */

function describeOdooReport(report) {
  if (!report) return "";
  const parts = [
    report.total + " tâche(s) suivie(s)",
    report.created + " créée(s)",
    report.updated + " mise(s) à jour",
    report.closed + " close(s)",
  ];
  if (report.projects_created) parts.push(report.projects_created + " projet(s) créé(s)");
  return parts.join(" · ");
}

function showOdooResult(message, level) {
  const resultat = document.getElementById("odoo-result");
  resultat.className = "config-result" + (level ? " " + level : "");
  resultat.textContent = message;
  resultat.hidden = !message;
}

async function loadOdoo() {
  const etat = document.getElementById("odoo-state");
  try {
    const data = await callApi("/api/odoo");
    if (!data.configured) {
      etat.textContent = "Connexion non renseignée.";
    } else if (data.last_sync) {
      etat.textContent = "Dernière synchronisation " + relativeStamp(data.last_sync) +
        " : " + describeOdooReport(data.last_report) + ".";
    } else {
      etat.textContent = "Jamais synchronisé.";
    }
  } catch (error) {
    etat.textContent = error.message;
  }
}

/* Enregistre d'abord le formulaire : tester ou synchroniser porte sur ce qui est affiche. */
async function saveOdoo() {
  await callApi("/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(GROUPS.odoo()),
  });
  document.getElementById("cf-odoo-key").value = "";
  // Connexion renseignee : le bouton « Ajouter l'agenda Odoo » peut apparaitre.
  loadSources();
}

document.getElementById("odoo-test").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  bouton.disabled = true;
  showOdooResult("Connexion…");
  try {
    await saveOdoo();
    const data = await callApi("/api/odoo/test", { method: "POST" });
    showOdooResult("Connecté : " + data.user + " (" + data.login + ") · " +
      data.open_tasks + " tâche(s) ouverte(s) assignée(s).", "ok");
    await reload();
  } catch (error) {
    showOdooResult(error.message, "ko");
  } finally {
    bouton.disabled = false;
  }
});

document.getElementById("odoo-sync").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  bouton.disabled = true;
  showOdooResult("Synchronisation…");
  try {
    await saveOdoo();
    const data = await callApi("/api/odoo/sync", { method: "POST" });
    const avertissements = (data.report.warnings || []).join(" ");
    showOdooResult(describeOdooReport(data.report) + "." +
      (avertissements ? " " + avertissements : ""), avertissements ? "warn" : "ok");
    await loadOdoo();
  } catch (error) {
    showOdooResult(error.message, "ko");
  } finally {
    bouton.disabled = false;
  }
});

/* ---------- Mails Outlook ---------- */

function showMailResult(message, level) {
  const resultat = document.getElementById("mail-result");
  resultat.className = "config-result" + (level ? " " + level : "");
  resultat.textContent = message;
  resultat.hidden = !message;
}

async function loadMail() {
  const etat = document.getElementById("mail-state");
  const bouton = document.getElementById("mail-read-now");
  try {
    const data = await callApi("/api/mail/status");
    bouton.disabled = !data.available;
    if (!data.available) {
      etat.textContent = data.refusal;
    } else if (data.running) {
      etat.textContent = "Lecture en cours…";
    } else if (!data.last_run) {
      etat.textContent = "Jamais lu.";
    } else if (data.last_error) {
      etat.textContent = "Échec " + relativeStamp(data.last_run) + " : " + data.last_error;
    } else {
      etat.textContent = "Dernier passage " + relativeStamp(data.last_run) + " · " +
        data.last_count + " mail(s) importé(s).";
    }
  } catch (error) {
    etat.textContent = error.message;
  }
}

document.getElementById("mail-read-now").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  bouton.disabled = true;
  showMailResult("Lecture en cours, une à deux minutes…");
  try {
    const data = await callApi("/api/mail/read", { method: "POST" });
    showMailResult(data.count
      ? data.count + " mail(s) déposé(s) dans la boîte de réception, à valider."
      : "Aucun nouveau mail utile.", "ok");
  } catch (error) {
    showMailResult(error.message, "ko");
  } finally {
    bouton.disabled = false;
    await loadMail();
  }
});

/* ---------- Telephone ---------- */

let pairTimer = null;

async function loadMobile() {
  try {
    const data = await callApi("/api/mobile");
    document.getElementById("mb-origin").value = data.origin || "";
    document.getElementById("mb-serve").textContent = "tailscale serve --bg " + data.port;
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveMobile() {
  const data = await callApi("/api/mobile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ origin: document.getElementById("mb-origin").value }),
  });
  document.getElementById("mb-origin").value = data.origin || "";
  return data;
}

document.getElementById("mb-save").addEventListener("click", async () => {
  try {
    const data = await saveMobile();
    toast(data.origin ? "Adresse enregistrée." : "Accès téléphone retiré.");
  } catch (error) {
    toast(error.message, true);
  }
});

/* Affiche le code et son compte a rebours ; il disparait a expiration. */
function showPairingCode(code, seconds) {
  const box = document.getElementById("mb-code-box");
  const help = document.getElementById("mb-code-help");
  document.getElementById("mb-code").textContent = code;
  box.hidden = false;
  clearInterval(pairTimer);
  let reste = seconds;
  const tick = () => {
    if (reste <= 0) {
      clearInterval(pairTimer);
      box.hidden = true;
      return;
    }
    help.textContent = "À saisir sur le téléphone · expire dans " +
      Math.floor(reste / 60) + ":" + String(reste % 60).padStart(2, "0");
    reste -= 1;
  };
  tick();
  pairTimer = setInterval(tick, 1000);
}

document.getElementById("mb-pair").addEventListener("click", async (event) => {
  const bouton = event.currentTarget;
  bouton.disabled = true;
  try {
    await saveMobile();
    const data = await callApi("/api/mobile/code", { method: "POST" });
    showPairingCode(data.code, data.expires_in);
  } catch (error) {
    toast(error.message, true);
  } finally {
    bouton.disabled = false;
  }
});

/* ---------- Initialisation ---------- */

/* Sections : une seule affichee a la fois, retenue dans l'adresse (#journee)
   pour qu'un lien ou un rechargement la rouvre. */
const CONFIG_GROUPS = [...document.querySelectorAll(".cfg-group")].map((section) => section.dataset.group);

function showGroup(name, focus = false) {
  const group = CONFIG_GROUPS.includes(name) ? name : CONFIG_GROUPS[0];
  document.querySelectorAll(".cfg-group").forEach((section) => {
    section.hidden = section.dataset.group !== group;
  });
  document.querySelectorAll(".cfg-nav-link").forEach((link) => {
    const active = link.dataset.group === group;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  });
  // Sur petit ecran, le menu est une rangee defilante : la section ouverte y reste visible.
  const nav = document.querySelector(".cfg-nav");
  const current = nav.querySelector(".cfg-nav-link.active");
  if (current && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = current.offsetLeft - (nav.clientWidth - current.offsetWidth) / 2;
  }
  if (focus) document.getElementById(`cfg-${group}-title`)?.focus({ preventScroll: true });
}

document.querySelector(".cfg-nav").addEventListener("click", (event) => {
  const link = event.target.closest(".cfg-nav-link");
  if (!link) return;
  event.preventDefault();
  history.replaceState(null, "", `#${link.dataset.group}`);
  showGroup(link.dataset.group, true);
  window.scrollTo({ top: 0 });
});

window.addEventListener("hashchange", () => showGroup(location.hash.slice(1)));
showGroup(location.hash.slice(1));

document.getElementById("header-date").textContent = new Date().toLocaleDateString("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
});

reload().catch((error) => toast(error.message, true));
// Appel distinct : une panne des sources ne doit pas bloquer le reste de la page.
loadSources();
loadAgentSetup();
loadMobile();
loadOdoo();
loadMail();
