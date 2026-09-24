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

  // The stage strip in the header: jump to a stage and open it. Without script
  // the anchor still scrolls there, and the section opens with one more click.
  document.addEventListener("click", (event) => {
    const link = event.target.closest(".run-stepper a[href^='#stage-']");
    if (!link) return;
    const stage = document.getElementById(link.hash.slice(1));
    if (!stage || stage.tagName !== "DETAILS") return;
    event.preventDefault();
    stage.open = true;
    stage.dataset.liveTouched = "true";
    const summary = stage.querySelector("summary");
    stage.scrollIntoView({ block: "start", behavior: "smooth" });
    if (summary) summary.focus({ preventScroll: true });
  });

  // Keeping a working run current is the shared in-place refresh in
  // documents.js: the header and each stage carry a data-live key, and only
  // what changed is swapped. It used to reload the whole page every five
  // seconds, which closed whatever stage the reader had opened.
})();
