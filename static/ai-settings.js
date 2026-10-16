/* Custom model ID and its provider only show when Custom is chosen. A task's form
   is also opened as a popup (modal.js), which inserts it without running its
   scripts - so forms are found when they appear, not only when the page loads. */
(() => {
  function prepare(form) {
    if (form.dataset.aiPrepared) return;
    const choice = form.querySelector("select[name$='-model_choice']");
    const provider = form.querySelector("select[name$='-provider']");
    const model = form.querySelector("input[name$='-model']");
    if (!choice || !provider || !model) return;
    form.dataset.aiPrepared = "true";
    const update = () => {
      const custom = choice.value === "custom";
      provider.closest("p").hidden = !custom;
      model.closest("p").hidden = !custom;
    };
    choice.addEventListener("change", update);
    update();
  }
  const scan = () => document.querySelectorAll(".ai-model-form").forEach(prepare);
  scan();
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
})();
