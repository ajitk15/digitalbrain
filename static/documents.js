// Refresh conversion status without disrupting file selection, forms or confirmation screens.
if (document.querySelector("[data-document-pending]")) {
  let changed = false;
  document.addEventListener("input", () => { changed = true; }, {once: true});
  document.addEventListener("change", () => { changed = true; }, {once: true});
  window.setTimeout(() => {
    if (!changed && !document.hidden) window.location.reload();
  }, 5000);
}
