/* NOMAIA — pieces jointes : composant reutilisable (lot 7).
 *
 *   JAttachments.mount(conteneur, "task" | "project" | "minutes" | "note", id)
 *
 * Liste les fichiers de l'element, permet d'en joindre (bouton ou glisser-
 * deposer sur le conteneur) et d'en retirer. Images et PDF s'ouvrent dans un
 * nouvel onglet ; les autres types se telechargent (decide par le serveur).
 */
(function () {
  const { callApi, escapeHtml, toast } = window.J;

  function size(bytes) {
    if (bytes < 1024) return bytes + " o";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " Ko";
    return (bytes / 1024 / 1024).toFixed(1).replace(".", ",") + " Mo";
  }

  function glyph(type) {
    if (type.startsWith("image/")) return "IMG";
    if (type === "application/pdf") return "PDF";
    if (/sheet|excel|csv/.test(type)) return "XLS";
    if (/word|document/.test(type)) return "DOC";
    return "FIC";
  }

  function mount(container, target, id) {
    if (!container || !id) return null;
    container.classList.add("att");
    container.innerHTML =
      '<ul class="att-list" aria-live="polite"></ul>' +
      '<label class="btn-ghost att-add">Joindre un fichier<input type="file" multiple hidden></label>' +
      '<p class="att-hint">ou glissez-déposez ici · 10 Mo au plus par fichier</p>';
    const list = container.querySelector(".att-list");
    const input = container.querySelector("input[type=file]");

    async function refresh() {
      try {
        const { attachments } = await callApi("/api/attachments?target=" + target + "&id=" + id);
        list.innerHTML = attachments.map((item, index) =>
          '<li class="att-item anim-in" style="--i:' + Math.min(index, 12) + '">' +
            '<span class="att-glyph" aria-hidden="true">' + glyph(item.content_type) + "</span>" +
            '<a class="att-name" href="/api/attachments/' + item.id + '/file" target="_blank" rel="noopener">' +
              escapeHtml(item.filename) + "</a>" +
            '<span class="att-size">' + size(item.size) + "</span>" +
            '<button type="button" class="icon-btn danger" data-att-delete="' + item.id + '" aria-label="Retirer ' +
              escapeHtml(item.filename) + '">✕</button>' +
          "</li>").join("");
      } catch (error) {
        list.innerHTML = '<li class="att-error">' + escapeHtml(error.message) + "</li>";
      }
    }

    async function upload(files) {
      for (const file of files) {
        const form = new FormData();
        form.append("target", target);
        form.append("id", id);
        form.append("file", file);
        try {
          await callApi("/api/attachments", { method: "POST", body: form });
        } catch (error) {
          toast(file.name + " : " + error.message, true);
        }
      }
      await refresh();
    }

    input.addEventListener("change", () => {
      upload([...input.files]);
      input.value = "";
    });

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-att-delete]");
      if (!button) return;
      try {
        await callApi("/api/attachments/" + button.dataset.attDelete, { method: "DELETE" });
        await refresh();
      } catch (error) {
        toast(error.message, true);
      }
    });

    ["dragenter", "dragover"].forEach((type) => container.addEventListener(type, (event) => {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      container.classList.add("is-dropping");
    }));
    ["dragleave", "drop"].forEach((type) => container.addEventListener(type, () => container.classList.remove("is-dropping")));
    container.addEventListener("drop", (event) => {
      if (!event.dataTransfer?.files?.length) return;
      event.preventDefault();
      upload([...event.dataTransfer.files]);
    });

    refresh();
    return { refresh };
  }

  window.JAttachments = { mount };
})();
