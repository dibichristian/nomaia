/* NOMAIA — briques du pilotage des projets, partagees par la vue d'ensemble
 * et la fiche projet : anneau des statuts, barre empilee, frise des etapes,
 * meteo et badges.
 *
 * Tout rend une chaine HTML ; les textes venus de l'API passent par
 * escapeHtml. Les couleurs portent sur des classes (s-<statut>) definies dans
 * pages/pilot.css : un statut n'est jamais signale par la couleur seule,
 * toujours accompagne d'un libelle (legende, badge ou infobulle).
 *
 * Mouvement (pages Projets, Projet, Bilan) : barres remplies par scaleX
 * (fillStyle), listes en cascade (stagger), chiffres qui defilent (countUp).
 */
window.Pilot = (function () {
  const { escapeHtml, formatDeadline } = window.J;

  // Ordre des legendes : du travail fait vers ce qui demande attention.
  const STATES = [
    { key: "done", label: "Terminées" },
    { key: "doing", label: "En cours" },
    { key: "todo", label: "À faire" },
    { key: "late", label: "En retard" },
    { key: "blocked", label: "Bloquées" },
  ];
  const STATE_LABELS = {
    done: "Terminée", doing: "En cours", todo: "À faire", late: "En retard", blocked: "Bloquée",
  };
  const PHASE_LABELS = { done: "Terminée", doing: "En cours", todo: "À venir", blocked: "Bloquée" };
  const SEVERITY_LABELS = { critical: "Critique", major: "Majeure", minor: "Mineure" };
  // Registre du projet : type d'entree et statut deduit de la tache de suivi.
  const RECORD_LABELS = { decision: "Décision", scope: "Périmètre", dependency: "Dépendance" };
  const RECORD_STATUS = { acted: "Actée", open: "À régler", settled: "Réglée", noted: "Notée" };
  const SIDE_LABELS = { us: "Nous", client: "Client", both: "Conjoint" };
  const WEATHER = {
    stormy: { icon: "⛈️", label: "Orageux" },
    rainy: { icon: "🌧️", label: "Pluvieux" },
    cloudy: { icon: "☁️", label: "Nuageux" },
    fair: { icon: "🌤️", label: "Éclaircies" },
    sunny: { icon: "☀️", label: "Ensoleillé" },
  };

  function total(states) {
    return STATES.reduce((sum, state) => sum + (states[state.key] || 0), 0);
  }

  function pct(value, sum) {
    return sum ? Math.round((value / sum) * 100) : 0;
  }

  /* Anneau des statuts de taches, total au centre. Un ecart de 2 px separe
   * les segments ; chaque segment porte son infobulle. */
  function donut(states, size) {
    const somme = total(states);
    const dim = size || 150;
    const rayon = 42;
    const tour = 2 * Math.PI * rayon;
    const presents = STATES.filter((state) => states[state.key]);
    const ecart = presents.length > 1 ? 1.5 : 0;
    let decalage = 0;

    const segments = somme
      ? presents
          .map((state) => {
            const longueur = (states[state.key] / somme) * tour;
            const trait = Math.max(longueur - ecart, 0.5);
            const segment =
              '<circle class="pl-arc s-' + state.key + '" r="' + rayon + '" cx="50" cy="50"' +
                ' stroke-dasharray="' + trait.toFixed(2) + " " + (tour - trait).toFixed(2) + '"' +
                ' stroke-dashoffset="' + (-decalage).toFixed(2) + '">' +
                "<title>" + state.label + " : " + states[state.key] + " (" + pct(states[state.key], somme) + " %)</title>" +
              "</circle>";
            decalage += longueur;
            return segment;
          })
          .join("")
      : '<circle class="pl-arc pl-arc-empty" r="' + rayon + '" cx="50" cy="50"></circle>';

    return (
      '<div class="pl-donut">' +
        '<svg viewBox="0 0 100 100" width="' + dim + '" height="' + dim + '" role="img"' +
          ' aria-label="Répartition des tâches par statut">' +
          '<g transform="rotate(-90 50 50)">' + segments + "</g>" +
          '<text x="50" y="49" class="pl-donut-total">' + somme + "</text>" +
          '<text x="50" y="62" class="pl-donut-label">tâches</text>' +
        "</svg>" +
        legend(states) +
      "</div>"
    );
  }

  /* Legende des statuts. Sans `states`, libelles seuls, sur une ligne. */
  function legend(states) {
    const somme = states ? total(states) : 0;
    return (
      '<ul class="pl-legend' + (states ? "" : " is-inline") + '">' +
      STATES.map(
        (state) =>
          '<li><span class="pl-swatch s-' + state.key + '"></span>' +
            '<span class="pl-legend-label">' + state.label + "</span>" +
            (states
              ? '<span class="pl-legend-value">' + (states[state.key] || 0) +
                  " <small>" + pct(states[state.key] || 0, somme) + " %</small></span>"
              : "") +
          "</li>"
      ).join("") +
      "</ul>"
    );
  }

  /* Barre empilee des statuts : une ligne par projet. */
  function stackBar(states) {
    const somme = total(states);
    if (!somme) return '<div class="pl-stack is-empty" title="Aucune tâche"></div>';
    return (
      '<div class="pl-stack">' +
      STATES.filter((state) => states[state.key])
        .map(
          (state) =>
            '<span class="pl-stack-seg s-' + state.key + '" style="flex-grow:' + states[state.key] + '"' +
              ' title="' + state.label + " : " + states[state.key] + " (" + pct(states[state.key], somme) + ' %)"></span>'
        )
        .join("") +
      "</div>"
    );
  }

  /* Frise des etapes : une pastille par etape, coloree par statut, l'etape
   * en cours soulignee. `large` ajoute les grandes etapes au-dessus. */
  function phaseStrip(phases, currentId, large) {
    if (!phases.length) return '<span class="pj-faint">Pas d\'étapes</span>';
    const pastilles = phases
      .map((phase) => {
        const titre = phase.sequence + ". " + phase.name + " — " + PHASE_LABELS[phase.status] +
          " · " + phase.progress_pct + " %" +
          (phase.planned_date ? " · prévue le " + formatDeadline(phase.planned_date) : "");
        return (
          '<span class="pl-pip s-' + phase.status + (phase.id === currentId ? " is-current" : "") + '"' +
            ' title="' + escapeHtml(titre) + '"' + (large ? ' data-phase="' + phase.id + '"' : "") + ">" +
            (phase.status === "doing" && phase.progress_pct < 100
              ? '<span class="pl-pip-fill" style="width:' + phase.progress_pct + '%"></span>'
              : "") +
          "</span>"
        );
      })
      .join("");

    if (!large) return '<div class="pl-strip">' + pastilles + "</div>";

    // Grandes etapes : un libelle qui s'etend sur ses pastilles consecutives.
    const groupes = [];
    phases.forEach((phase) => {
      const nom = phase.stage || "";
      const dernier = groupes[groupes.length - 1];
      if (dernier && dernier.name === nom) dernier.count += 1;
      else groupes.push({ name: nom, count: 1 });
    });
    const entetes = groupes
      .map(
        (groupe) =>
          '<span class="pl-stage" style="flex-grow:' + groupe.count + '">' + escapeHtml(groupe.name) + "</span>"
      )
      .join("");
    return (
      '<div class="pl-strip-large">' +
        '<div class="pl-stages">' + entetes + "</div>" +
        '<div class="pl-strip">' + pastilles + "</div>" +
      "</div>"
    );
  }

  function weather(code, withLabel) {
    const meteo = WEATHER[code];
    if (!meteo) return withLabel ? '<span class="pl-weather is-none">Météo non renseignée</span>' : "";
    return (
      '<span class="pl-weather w-' + code + '" title="' + meteo.label + '">' + meteo.icon +
        (withLabel ? " " + meteo.label : "") + "</span>"
    );
  }

  function stateBadge(state) {
    return '<span class="pl-state s-' + state + '">' + STATE_LABELS[state] + "</span>";
  }

  function phaseBadge(status) {
    return '<span class="pl-state s-' + status + '">' + PHASE_LABELS[status] + "</span>";
  }

  function sideBadge(side) {
    if (!side || side === "us") return "";
    return '<span class="pl-side">' + SIDE_LABELS[side] + "</span>";
  }

  /* Nature d'une tache : anomalie (avec sa gravite) ou livrable client. */
  function kindBadge(task) {
    if (task.kind === "anomaly") {
      const severity = task.severity || "major";
      return '<span class="pl-sev sev-' + severity + '">Anomalie · ' +
        SEVERITY_LABELS[severity].toLowerCase() + "</span>";
    }
    if (task.kind === "deliverable") return '<span class="pl-kind">Livrable</span>';
    return "";
  }

  function severityBadge(severity) {
    return '<span class="pl-sev sev-' + severity + '">' + SEVERITY_LABELS[severity] + "</span>";
  }

  function recordBadge(entry) {
    return (
      '<span class="pl-kind">' + RECORD_LABELS[entry.kind] + "</span>" +
      '<span class="pl-record-status r-' + entry.status + '">' + RECORD_STATUS[entry.status] + "</span>"
    );
  }

  /* Delai d'un jalon : « J-3 », « aujourd'hui », « dépassé de 2 j ». */
  function daysLabel(days) {
    if (days === null || days === undefined) return "";
    if (days < 0) return "dépassé de " + -days + " j";
    if (days === 0) return "aujourd'hui";
    return "J-" + days;
  }

  /* ---------- Mouvement ---------- */

  // Plafond du decalage de la cascade : au-dela, les elements entrent ensemble.
  const STAGGER_MAX = 12;
  const COUNT_DURATION = 700;

  /* Animations reduites : reglage systeme ou choix fait dans Config. */
  function reducedMotion() {
    return (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      document.documentElement.dataset.motion === "reduced"
    );
  }

  /* Attribut style d'un remplissage de barre (.load-fill, .bar-fill) :
   * la largeur passe par scaleX, bornee entre 0 et 100 %. `extra` ajoute des
   * declarations deja echappees (ex. la couleur d'un projet). */
  function fillStyle(percent, extra) {
    const borne = Math.min(Math.max(Number(percent) || 0, 0), 100);
    return 'style="--pct:' + (borne / 100).toFixed(3) + (extra ? ";" + extra : "") + '"';
  }

  /* Apparition en cascade (.anim-in de motion.css) : decalage --i par rang,
   * plafonne a STAGGER_MAX. Accepte une NodeList, un tableau ou un HTMLCollection. */
  function stagger(nodes) {
    Array.prototype.forEach.call(nodes, (node, rang) => {
      node.classList.add("anim-in");
      node.style.setProperty("--i", Math.min(rang, STAGGER_MAX));
    });
  }

  /* Defile les indicateurs d'un bloc (.stat-value) de 0 a leur valeur, une
   * seule fois par bloc (premier affichage). Seuls les entiers, suivis ou non
   * de « % », defilent : durees, fractions et symboles restent tels quels.
   * Rien ne bouge si les animations sont reduites. */
  function countUp(container) {
    if (!container || container.dataset.counted) return;
    container.dataset.counted = "1";
    if (reducedMotion()) return;

    const cibles = [];
    container.querySelectorAll(".stat-value").forEach((node) => {
      const trouve = /^(\d+)(\s?%)?$/.exec(node.textContent.trim());
      if (!trouve || trouve[1] === "0") return;
      cibles.push({ node, value: Number(trouve[1]), suffix: trouve[2] || "" });
    });
    if (!cibles.length) return;

    let debut = null;
    function etape(instant) {
      if (debut === null) debut = instant;
      const avance = Math.min((instant - debut) / COUNT_DURATION, 1);
      const courbe = 1 - Math.pow(1 - avance, 3); // easeOutCubic
      cibles.forEach((cible) => {
        cible.node.textContent = Math.round(cible.value * courbe) + cible.suffix;
      });
      if (avance < 1) window.requestAnimationFrame(etape);
    }
    cibles.forEach((cible) => {
      cible.node.textContent = "0" + cible.suffix;
    });
    window.requestAnimationFrame(etape);
  }

  return {
    reducedMotion, fillStyle, stagger, countUp,
    STATES, STATE_LABELS, PHASE_LABELS, SIDE_LABELS, WEATHER, SEVERITY_LABELS, RECORD_LABELS, RECORD_STATUS,
    donut, legend, stackBar, phaseStrip, weather, stateBadge, phaseBadge, sideBadge, daysLabel,
    kindBadge, severityBadge, recordBadge,
  };
})();
