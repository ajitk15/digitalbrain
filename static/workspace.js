/* Optional panels preserve the original pages and never invoke server actions. */
(() => {
  const workspace = document.querySelector("[data-workspace]");
  const controls = [...document.querySelectorAll("[data-kb-panel]")];
  if (workspace && controls.length) {
    const mobile = window.matchMedia("(max-width: 700px)");
    workspace.classList.add("panels-ready");
    const graph = workspace.querySelector(".kb-graph");
    let selected = null;
    function show(name) {
      selected = name;
      controls.forEach((button) => {
        const panel = document.getElementById(button.getAttribute("aria-controls"));
        const open = button.dataset.kbPanel === name;
        if (!open && panel.contains(document.activeElement)) button.focus();
        panel.hidden = !open;
        button.setAttribute("aria-expanded", String(open));
      });
      graph.hidden = mobile.matches && !!name;
      workspace.classList.toggle("has-panel", !!name);
    }
    controls.forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", () => show(selected === button.dataset.kbPanel ? null : button.dataset.kbPanel));
    });
    mobile.addEventListener("change", () => show(selected));
    show(workspace.querySelector(".errorlist") ? "sources" : null);
  }
  const historyToggle = document.querySelector("[data-panel-toggle]");
  if (historyToggle) {
    const history = document.getElementById(historyToggle.dataset.panelToggle);
    const narrow = window.matchMedia("(max-width: 900px)");
    const apply = (open) => {
      if (!open && history.contains(document.activeElement)) historyToggle.focus();
      history.hidden = !open;
      historyToggle.setAttribute("aria-expanded", String(open));
    };
    historyToggle.hidden = !narrow.matches;
    apply(!narrow.matches);
    historyToggle.addEventListener("click", () => apply(history.hidden));
    narrow.addEventListener("change", () => { historyToggle.hidden = !narrow.matches; apply(!narrow.matches); });
  }
  function revealHash() {
    if (!["#upload", "#add-source"].includes(location.hash)) return;
    const upload = document.getElementById(location.hash.slice(1));
    if (upload && upload.tagName === "DETAILS") upload.open = true;
  }
  document.addEventListener("click", (event) => {
    const disclosureLink = event.target.closest("a[href='#upload'], a[href='#add-source']");
    if (disclosureLink) {
      const upload = document.getElementById(disclosureLink.hash.slice(1));
      if (upload) {
        // Expand in place. The href is the path for a browser without
        // JavaScript, where navigating to the anchor is the only way to reach
        // the panel; with script the jump and the #upload left in the address
        // bar are noise, because the panel is already on screen. Focus moves
        // to the summary instead, which also scrolls it into view - minimally,
        // and only when it was actually out of view.
        event.preventDefault();
        upload.open = true;
        const summary = upload.querySelector("summary");
        if (summary) summary.focus();
      }
    }
    document.querySelectorAll(".nav-disclosure[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = event.target.closest(".nav-disclosure[open]");
    if (menu) { menu.open = false; menu.querySelector("summary").focus(); }
  });
  window.addEventListener("hashchange", revealHash);
  revealHash();
})();
