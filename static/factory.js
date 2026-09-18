// The run screen: selecting gaps, and keeping a live run up to date.
//
// Both are conveniences over controls that already work without script. The
// checkboxes are real form fields; the refresh control is a real link to this
// same page. Nothing here is the only way to do anything.
(() => {
  "use strict";

  // Select all / clear all for the gap checkboxes. The buttons are rendered
  // `hidden` and unhidden here, because without script they would be controls
  // that do nothing, and ticking eight boxes by hand is a chore rather than a
  // barrier.
  document.querySelectorAll("[data-pick-all]").forEach((bar) => {
    const boxes = () => document.querySelectorAll("." + bar.dataset.pickAll);
    if (!boxes().length) return;
    bar.hidden = false;
    bar.addEventListener("click", (event) => {
      const button = event.target.closest("[data-pick]");
      if (!button) return;
      const wanted = button.dataset.pick === "all";
      // Assigning fires no change event, and the row styling keys off :checked
      // in CSS, so there is nothing else to keep in step.
      boxes().forEach((box) => { box.checked = wanted; });
    });
  });

  // Keep a working run current.
  //
  // Deliberately not the document poll: that one stops at the first `toggle`,
  // which on this page means opening a stage to watch it is what stops it
  // updating. What must not be interrupted here is narrower and knowable -
  // text somebody has typed into the review note, and an open dialog - so
  // those are what hold it off, and it resumes when they are done.
  const live = document.querySelector("[data-run-active]");
  if (!live) return;

  const typing = () => {
    const field = document.querySelector("textarea, input:not([type=hidden])");
    return Boolean(field && field.value.trim());
  };
  const busy = () =>
    typing() || document.hidden || Boolean(document.querySelector("dialog[open]"));

  window.setInterval(() => {
    if (!busy()) window.location.reload();
  }, 5000);
})();
