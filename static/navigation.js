(() => {
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("workspace-sidebar");
  const close = document.getElementById("sidebar-close");
  const backdrop = document.getElementById("sidebar-backdrop");
  if (!toggle || !sidebar || !close || !backdrop) return;
  const shell = document.querySelector(".shell");
  const mobile = window.matchMedia("(max-width: 700px)");
  let collapsed = false;
  let drawerOpen = false;
  try { collapsed = localStorage.getItem(toggle.dataset.preferenceKey) === "collapsed"; }
  catch { /* Navigation works when storage is unavailable. */ }
  function apply() {
    const hidden = mobile.matches ? !drawerOpen : collapsed;
    if (hidden && sidebar.contains(document.activeElement)) toggle.focus();
    sidebar.hidden = hidden;
    close.hidden = !mobile.matches;
    backdrop.hidden = !(mobile.matches && drawerOpen);
    shell.inert = mobile.matches && drawerOpen;
    document.documentElement.classList.toggle("sidebar-collapsed", hidden);
    document.documentElement.classList.toggle("drawer-open", mobile.matches && drawerOpen);
    toggle.setAttribute("aria-expanded", String(!hidden));
    const label = hidden ? "Open navigation" : "Close navigation";
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    if (mobile.matches && drawerOpen) close.focus();
  }
  toggle.hidden = false;
  toggle.addEventListener("click", () => {
    if (mobile.matches) drawerOpen = !drawerOpen;
    else {
      collapsed = !collapsed;
      try { localStorage.setItem(toggle.dataset.preferenceKey, collapsed ? "collapsed" : "expanded"); }
      catch { /* Persistence is optional. */ }
    }
    apply();
  });
  function dismiss() {
    drawerOpen = false;
    shell.inert = false;
    apply();
    toggle.focus();
  }
  close.addEventListener("click", dismiss);
  backdrop.addEventListener("click", dismiss);
  document.addEventListener("keydown", (event) => {
    if (!mobile.matches || !drawerOpen || document.querySelector("dialog[open]")) return;
    if (event.key === "Escape") { event.preventDefault(); dismiss(); }
    if (event.key !== "Tab") return;
    const controls = [...sidebar.querySelectorAll("a, button, summary")]
      .filter((element) => element.getClientRects().length && !element.hidden);
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  mobile.addEventListener("change", () => { drawerOpen = false; apply(); });
  apply();
})();
