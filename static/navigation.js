(() => {
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("workspace-sidebar");
  if (!toggle || !sidebar) return;
  const key = toggle.dataset.preferenceKey;
  function setCollapsed(collapsed) {
    // Hidden content must also leave keyboard and screen-reader navigation.
    if (collapsed && sidebar.contains(document.activeElement)) toggle.focus();
    sidebar.hidden = collapsed;
    document.documentElement.classList.toggle("sidebar-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    const label = collapsed ? "Expand left menu" : "Minimize left menu";
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  }
  toggle.hidden = false;
  try { setCollapsed(localStorage.getItem(key) === "collapsed"); }
  catch { setCollapsed(false); }
  toggle.addEventListener("click", () => {
    const collapsed = !sidebar.hidden;
    setCollapsed(collapsed);
    try { localStorage.setItem(key, collapsed ? "collapsed" : "expanded"); }
    catch { /* Navigation remains usable when browser storage is disabled. */ }
  });
})();
