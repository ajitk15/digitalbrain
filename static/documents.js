// Refresh conversion status without disrupting file selection, forms, confirmation
// screens or graph exploration.
//
// Documents now also appear in the Knowledge Base rail, so this poll can fire on a
// screen that holds the graph explorer. Panning and zooming raise pointer and wheel
// events, not input or change, so without the listeners below a converting document
// would reload the page out from under someone mid-exploration and discard their
// zoom, pan and expanded nodes.
if (document.querySelector("[data-document-pending]")) {
  let busy = false;
  const stop = () => { busy = true; };

  ["input", "change"].forEach((name) =>
    document.addEventListener(name, stop, { once: true })
  );

  const canvas = document.getElementById("graph-canvas");
  if (canvas) {
    ["pointerdown", "wheel", "keydown"].forEach((name) =>
      canvas.addEventListener(name, stop, { once: true, passive: true })
    );
  }
  // Opening a source chip or a details disclosure is reading, not idling.
  document.addEventListener("toggle", stop, { capture: true, once: true });

  window.setTimeout(() => {
    if (!busy && !document.hidden) window.location.reload();
  }, 5000);
}
