/*
 * Focus mode: hides the sidebar, top bar and breadcrumb trail so the graph or the
 * conversation fills the window.
 *
 * This file is loaded in <head> WITHOUT defer, on purpose. navigation.js is
 * deferred, so the sidebar visibly paints and then collapses; the same flash here
 * would mean the entire chrome vanishing after first paint. Writing one class on
 * documentElement before the first paint is the fix, and a blocking external
 * script is the only CSP-legal way to do it - inline <script> is forbidden by
 * script-src 'self'. Nothing below <html> is touched, so this cannot race the
 * parser.
 *
 * It deliberately never touches sidebar.hidden. navigation.js owns that property,
 * so leaving focus mode restores whatever sidebar preference the user had.
 */
(() => {
  const root = document.documentElement;
  const key = root.dataset.focusKey;

  const paint = (on) => root.classList.toggle("focus-mode", on);

  // Before first paint: no flash of full chrome.
  try {
    paint(key && localStorage.getItem(key) === "on");
  } catch {
    paint(false);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("focus-toggle");
    if (!toggle) {
      // No toggle on this screen: never strand the user in a chrome-less page.
      paint(false);
      return;
    }

    const apply = (on) => {
      paint(on);
      toggle.setAttribute("aria-pressed", String(on));
      const label = on ? "Exit focus mode (Escape)" : "Enter focus mode";
      toggle.setAttribute("aria-label", label);
      toggle.title = label;
      try {
        localStorage.setItem(toggle.dataset.preferenceKey, on ? "on" : "off");
      } catch {
        /* private mode: the preference simply does not persist */
      }
    };

    toggle.hidden = false;
    apply(root.classList.contains("focus-mode"));

    toggle.addEventListener("click", () =>
      apply(!root.classList.contains("focus-mode"))
    );

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!root.classList.contains("focus-mode")) return;
      // Let an open disclosure or dialog have Escape first.
      if (event.target.closest && event.target.closest("details[open] > summary, dialog[open]")) {
        return;
      }
      apply(false);
      toggle.focus();
    });
  });
})();
