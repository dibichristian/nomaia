/* NOMAIA — menu principal : bulle de la page active, panneau « Plus ».
 *
 * La bulle glisse de la page quittee vers la page ouverte : au clic, l'onglet
 * de depart est retenu (sessionStorage), et au chargement suivant la bulle
 * part de lui. Sur telephone, le bouton « Plus » ouvre le panneau des autres
 * pages, et la bulle du bas s'efface pendant une saisie (clavier ouvert).
 *
 * Enveloppe dans une fonction : les constantes de premier niveau d'un script
 * classique sont partagees avec les autres scripts de la page.
 */
(function () {
  const nav = document.getElementById("main-nav");
  if (!nav) return;

  const bubble = nav.querySelector(".nav-bubble");
  const links = Array.from(nav.querySelectorAll(".nav-link"));
  const moreButton = document.getElementById("nav-more");
  const sheet = document.getElementById("nav-sheet");
  const phone = window.matchMedia("(max-width: 860px)");

  const FROM_KEY = "nomaia-nav-from";
  const TEXT_INPUTS = ["checkbox", "radio", "range", "button", "submit", "color", "file"];

  function isVisible(element) {
    return Boolean(element) && element.offsetParent !== null;
  }

  /* Onglet actif visible : sur telephone, une page du panneau « Plus » est
   * representee par le bouton « Plus ». */
  function activeLink() {
    return links.find((link) => link.classList.contains("active") && isVisible(link)) || null;
  }

  function place(target, animate) {
    if (!target) {
      bubble.style.opacity = "0";
      return;
    }
    bubble.classList.toggle("is-instant", !animate);
    bubble.style.width = target.offsetWidth + "px";
    bubble.style.height = target.offsetHeight + "px";
    bubble.style.transform = "translate(" + target.offsetLeft + "px, " + target.offsetTop + "px)";
    bubble.style.opacity = "1";
  }

  function readFrom() {
    try {
      const index = sessionStorage.getItem(FROM_KEY);
      sessionStorage.removeItem(FROM_KEY);
      return index === null ? null : links[Number(index)] || null;
    } catch (error) {
      return null;
    }
  }

  function rememberFrom() {
    const current = activeLink();
    if (!current) return;
    try {
      sessionStorage.setItem(FROM_KEY, String(links.indexOf(current)));
    } catch (error) {
      // Stockage indisponible (navigation privee) : la bulle apparait sans glisser.
    }
  }

  function start() {
    nav.classList.add("has-bubble");
    const target = activeLink();
    const from = readFrom();
    if (isVisible(from) && from !== target) {
      place(from, false);
      // Deux images : la position de depart doit etre peinte avant la transition.
      requestAnimationFrame(() => requestAnimationFrame(() => place(target, true)));
    } else {
      place(target, false);
    }
  }

  nav.addEventListener("click", (event) => {
    if (event.target.closest("a.nav-link")) rememberFrom();
  });

  window.addEventListener("resize", () => place(activeLink(), false));

  /* --- Panneau « Plus » --- */

  if (moreButton && sheet) {
    moreButton.addEventListener("click", () => sheet.showModal());
    sheet.addEventListener("click", (event) => {
      // Clic sur le fond assombri : la cible est le dialog lui-meme.
      if (event.target === sheet) sheet.close();
      if (event.target.closest("a")) rememberFrom();
    });
  }

  /* --- Saisie sur telephone : la bulle du bas laisse la place au clavier --- */

  function isTextField(element) {
    if (!element) return false;
    // Une liste deroulante ouvre un selecteur natif, pas le clavier.
    if (element.tagName === "TEXTAREA") return true;
    return element.tagName === "INPUT" && !TEXT_INPUTS.includes(element.type);
  }

  // Plusieurs pages placent le curseur dans leur champ principal au
  // chargement : sans geste de l'utilisateur, ce n'est pas une saisie.
  let userActed = false;
  ["pointerdown", "keydown"].forEach((type) => {
    document.addEventListener(type, () => { userActed = true; }, { capture: true, once: true });
  });

  document.addEventListener("focusin", (event) => {
    if (userActed && phone.matches && isTextField(event.target)) {
      document.body.classList.add("is-typing");
    }
  });

  // Toucher un champ qui a deja le curseur (pose au chargement) ouvre le
  // clavier sans nouvel evenement focusin : le toucher suffit alors.
  document.addEventListener("pointerdown", (event) => {
    if (phone.matches && isTextField(event.target)) document.body.classList.add("is-typing");
  }, true);

  document.addEventListener("focusout", () => {
    // Le focus passe d'un champ a l'autre sans quitter la saisie : on attend
    // que le nouvel element soit connu avant de rendre la bulle.
    setTimeout(() => {
      if (!isTextField(document.activeElement)) document.body.classList.remove("is-typing");
    }, 0);
  });

  start();
})();
