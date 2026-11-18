/* Badge d'etat du moteur de triage, present sur toutes les pages. */

(function () {
  const badge = document.getElementById("engine-badge");
  if (!badge) return;

  async function refresh() {
    try {
      const data = await fetch("/api/status").then((response) => response.json());
      // Le libelle et le modele viennent desormais du serveur : un
      // fournisseur ajoute cote Python apparait sans toucher au JavaScript.
      const name = data.llm.model || data.llm.label || data.llm.backend;
      const workerOk = data.worker_running;
      badge.textContent = name + (data.llm.ok && workerOk ? " ✓" : " ✕");
      badge.classList.toggle("ko", !(data.llm.ok && workerOk));
      badge.title = data.llm.ok
        ? (workerOk ? "" : "Worker arrêté — ") + (data.llm.label || "") + " · " + data.llm.detail
        : data.llm.detail;
      // Entrees a traiter : pastille sur « Boite de reception » dans le menu.
      const inbox = document.getElementById("inbox-badge");
      if (inbox) {
        const pending = Number(data.inbox_pending) || 0;
        inbox.textContent = pending;
        inbox.hidden = pending === 0;
        inbox.title = pending + " entrée" + (pending > 1 ? "s" : "") + " à traiter";
      }
    } catch (error) {
      badge.textContent = "hors ligne";
      badge.classList.add("ko");
    }
  }

  refresh();
  setInterval(refresh, 20000);
  // Rechargement a chaud (shell.js, charge apres ce fichier) : la pastille
  // suit les changements ; l'intervalle de 20 s reste un filet.
  document.addEventListener("DOMContentLoaded", () => {
    if (window.J && J.live) J.live.onChange(refresh);
  });
})();
