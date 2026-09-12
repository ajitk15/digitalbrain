document.querySelectorAll(".ai-model-form").forEach(form => {
  const choice = form.querySelector("select[name$='-model_choice']");
  const provider = form.querySelector("select[name$='-provider']");
  const model = form.querySelector("input[name$='-model']");
  const update = () => {
    const custom = choice.value === "custom";
    provider.closest("p").hidden = !custom;
    model.closest("p").hidden = !custom;
  };
  choice.addEventListener("change", update); update();
});
