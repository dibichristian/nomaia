/* Fonctions partagees par toutes les pages : appels API, notifications, formats. */

window.J = (function () {
  const toastEl = document.getElementById("toast");
  let toastTimer = null;

  function toast(message, isError) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.classList.toggle("error", Boolean(isError));
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2800);
  }

  async function callApi(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || "Erreur serveur.");
    }
    // Ecriture reussie : la coquille en profite pour remettre a jour ce qui
    // en depend (anneau d'avancement du logo) sans attendre le sondage.
    const method = ((options && options.method) || "GET").toUpperCase();
    if (method !== "GET") document.dispatchEvent(new CustomEvent("nomaia:write", { detail: { url } }));
    return data;
  }

  /* Echappe un texte pour innerHTML, contenu ET attributs : sans les
   * guillemets, une suggestion du modele posee dans data-chip="..." pouvait
   * fermer l'attribut et y glisser un gestionnaire d'evenement. */
  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function formatDuration(minutes) {
    if (!minutes) return "";
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (!hours) return rest + " min";
    return rest ? hours + " h " + String(rest).padStart(2, "0") : hours + " h";
  }

  function formatDeadline(iso) {
    if (!iso) return "";
    const parts = String(iso).split("-");
    return parts[2] + "/" + parts[1] + "/" + parts[0];
  }

  function formatMoment(value) {
    if (!value) return "";
    const moment = new Date(String(value).replace(" ", "T"));
    if (Number.isNaN(moment.getTime())) return value;
    const today = new Date();
    const sameDay = moment.toDateString() === today.toDateString();
    const time = moment.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    if (sameDay) return time;
    return moment.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) + " " + time;
  }

  /* ---------- Arborescence des taches ---------- */

  /* Range des taches en arbre parent > sous-taches. Chaque noeud est une copie
   * de la tache munie de `children`, tries comme dans Odoo (position puis id).
   * Une tache dont le parent est absent de la liste reste a la racine : rien
   * n'est jamais cache. Les racines gardent l'ordre de la liste recue. */
  function nestTasks(tasks) {
    const nodes = new Map(tasks.map((task) => [task.id, { ...task, children: [] }]));
    const roots = [];
    nodes.forEach((node) => {
      const parent = node.parent_id ? nodes.get(node.parent_id) : null;
      (parent && parent !== node ? parent.children : roots).push(node);
    });
    const order = (a, b) => (a.position || 0) - (b.position || 0) || a.id - b.id;
    nodes.forEach((node) => node.children.sort(order));

    // Garde-fou : un cycle de parents rendrait ses taches inaccessibles depuis
    // les racines. Elles y sont remises plutot que perdues.
    const seen = new Set();
    const walk = (node) => {
      if (seen.has(node.id)) return;
      seen.add(node.id);
      node.children.forEach(walk);
    };
    roots.forEach(walk);
    nodes.forEach((node) => {
      if (seen.has(node.id)) return;
      // Casse le cycle en detachant la tache de son parent.
      const parent = nodes.get(node.parent_id);
      parent.children = parent.children.filter((child) => child !== node);
      roots.push(node);
      walk(node);
    });
    return roots;
  }

  /* Taches d'un arbre, la racine comprise. */
  function treeTasks(node) {
    return [node].concat(...node.children.map(treeTasks));
  }

  /* Branches repliees, memorisees dans le navigateur. Simple confort : sans
   * stockage disponible, tout reste deplie. */
  const FOLD_KEY = "nomaia.folded";
  const folded = (() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || "[]"));
    } catch (error) {
      return new Set();
    }
  })();

  function isFolded(id) {
    return folded.has(Number(id));
  }

  function toggleFold(id) {
    const key = Number(id);
    if (folded.has(key)) folded.delete(key);
    else folded.add(key);
    try {
      localStorage.setItem(FOLD_KEY, JSON.stringify([...folded]));
    } catch (error) {
      // Stockage indisponible : le repli vaut pour la page ouverte seulement.
    }
  }

  /* Bouton de repli d'une tache parente, avec ses sous-taches terminees / total. */
  function foldButton(node) {
    if (!node.children.length) return "";
    const done = node.children.filter((child) => child.status === "done").length;
    const open = !isFolded(node.id);
    return (
      '<button type="button" class="tree-fold" data-fold="' + node.id + '" aria-expanded="' + open +
        '" title="' + (open ? "Replier" : "Déplier") + ' les sous-tâches">' +
        '<span class="tree-caret">' + (open ? "▾" : "▸") + "</span>" +
        done + "/" + node.children.length + " sous-tâche" + (node.children.length > 1 ? "s" : "") +
      "</button>"
    );
  }

  /* Rend un arbre : rowFn(node, depth) produit la ligne, les sous-taches
   * suivent sauf si la branche est repliee. */
  function renderTree(roots, rowFn, depth) {
    const niveau = depth || 0;
    return roots.map((node) =>
      rowFn(node, niveau) + (isFolded(node.id) ? "" : renderTree(node.children, rowFn, niveau + 1))
    ).join("");
  }

  return {
    toast, callApi, escapeHtml, formatDuration, formatDeadline, formatMoment,
    nestTasks, treeTasks, isFolded, toggleFold, foldButton, renderTree,
  };
})();
