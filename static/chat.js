(() => {
  const form = document.getElementById("chat-composer");
  if (!form) return;
  const input = form.querySelector("textarea");
  const messages = document.getElementById("chat-messages");
  if (!new URLSearchParams(location.search).has("messages")) messages.scrollTop = messages.scrollHeight;
  let submitting = false;
  form.addEventListener("submit", (event) => {
    if (submitting) { event.preventDefault(); return; }
    submitting = true;
    document.getElementById("chat-send").disabled = true;
    document.getElementById("chat-status").hidden = false;
    form.setAttribute("aria-busy", "true");
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  window.addEventListener("pageshow", () => {
    submitting = false;
    document.getElementById("chat-send").disabled = false;
    document.getElementById("chat-status").hidden = true;
    form.removeAttribute("aria-busy");
  });
})();
