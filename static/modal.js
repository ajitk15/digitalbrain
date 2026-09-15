/* Short forms reuse their ordinary URL and POST contract. */
(() => {
  const dialog = document.createElement("dialog");
  dialog.className = "modal";
  dialog.id = "app-modal";
  const body = document.createElement("div");
  body.className = "modal-body";
  dialog.append(body);
  document.body.append(dialog);
  let opener = null;
  let request = null;
  let generation = 0;
  let submitting = false;
  function close() { if (!submitting) dialog.close(); }
  dialog.addEventListener("cancel", (event) => { if (submitting) event.preventDefault(); });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  dialog.addEventListener("close", () => {
    generation += 1;
    request?.abort();
    body.replaceChildren();
    if (opener?.isConnected) opener.focus();
    opener = null;
  });
  function focusContent() {
    const target = body.querySelector(".errorlist") ||
      (body.querySelector("button.danger") ? body.querySelector("[data-modal-close]") :
        body.querySelector("input:not([type=hidden]), select, textarea")) || body.querySelector("h1");
    if (target) { if (!target.matches("input, select, textarea, button, a")) target.tabIndex = -1; target.focus(); }
  }
  function status(message) {
    let notice = body.querySelector(".modal-status");
    if (!notice) {
      notice = document.createElement("p");
      notice.className = "notice notice-error modal-status";
      notice.setAttribute("role", "alert");
      notice.tabIndex = -1;
      body.prepend(notice);
    }
    notice.textContent = message;
    notice.focus();
  }
  function render(markup, url) {
    const parsed = new DOMParser().parseFromString(markup, "text/html");
    const main = parsed.querySelector("main");
    if (!main) throw new Error("No form page");
    main.querySelectorAll(".breadcrumbs, .application-menu, .tabs, .skip-link").forEach((node) => node.remove());
    body.replaceChildren(...main.childNodes);
    const heading = body.querySelector("h1, h2");
    if (heading) {
      heading.id = "app-modal-title";
      dialog.setAttribute("aria-labelledby", heading.id);
      dialog.removeAttribute("aria-label");
    } else {
      dialog.removeAttribute("aria-labelledby");
      dialog.setAttribute("aria-label", "Edit details");
    }
    if (!body.querySelector("[data-modal-close]")) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "button secondary";
      cancel.textContent = "Cancel";
      cancel.dataset.modalClose = "";
      (body.querySelector(".actions") || body).append(cancel);
    }
    body.querySelectorAll("[data-modal-close]").forEach((control) => {
      control.addEventListener("click", (event) => { event.preventDefault(); close(); });
    });
    body.querySelectorAll("form").forEach((form) => {
      if (!form.getAttribute("action")) form.action = url;
      form.addEventListener("submit", async (event) => {
        if (event.defaultPrevented || form.enctype === "multipart/form-data" || form.method.toLowerCase() !== "post") return;
        event.preventDefault();
        if (submitting || form.dataset.outcomeUnknown) return;
        const data = new FormData(form);
        if (event.submitter?.name) data.append(event.submitter.name, event.submitter.value);
        const buttons = [...form.querySelectorAll("button[type=submit], button:not([type]), input[type=submit]")];
        const enabledButtons = buttons.filter((button) => !button.disabled);
        enabledButtons.forEach((button) => { button.disabled = true; });
        submitting = true;
        form.setAttribute("aria-busy", "true");
        let uncertain = false;
        try {
          const response = await fetch(form.action, { method: "POST", body: data,
            headers: { "X-Requested-With": "fetch" }, credentials: "same-origin", redirect: "follow", signal: AbortSignal.timeout(30000) });
          if (!response.ok) {
            uncertain = response.status >= 500;
            status(uncertain ? "The server could not confirm the result. Close this window and check the list before trying again." :
              "This request could not be saved. Your entries are still here. Close this window if you need to sign in again.");
            return;
          }
          const landed = new URL(response.url);
          if (landed.pathname !== new URL(form.action).pathname) { window.location.assign(landed.href); return; }
          render(await response.text(), form.action);
          focusContent();
        } catch {
          uncertain = true;
          status("The connection was interrupted and the result is unknown. Close this window and check the list before trying again.");
        } finally {
          submitting = false;
          form.removeAttribute("aria-busy");
          if (uncertain) form.dataset.outcomeUnknown = "true";
          else enabledButtons.forEach((button) => { button.disabled = false; });
        }
      });
    });
  }
  document.addEventListener("click", async (event) => {
    const link = event.target.closest("a[data-modal]");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (submitting) return;
    request?.abort();
    request = new AbortController();
    const ticket = ++generation;
    opener = link;
    const title = document.createElement("h1");
    title.id = "app-modal-title";
    title.textContent = "Loading…";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button secondary";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", close);
    body.replaceChildren(title, cancel);
    dialog.setAttribute("aria-labelledby", title.id);
    if (!dialog.open) dialog.showModal();
    cancel.focus();
    try {
      const response = await fetch(link.href, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin", signal: request.signal });
      if (!response.ok) throw new Error("Page unavailable");
      // Expired sessions belong on the real sign-in page, not inside a popup.
      if (new URL(response.url).pathname !== new URL(link.href).pathname) {
        if (ticket === generation) window.location.assign(response.url);
        return;
      }
      const markup = await response.text();
      if (ticket !== generation || !dialog.open) return;
      render(markup, link.href);
      focusContent();
    } catch (error) {
      if (ticket !== generation || error.name === "AbortError") return;
      close();
      window.location.assign(link.href);
    }
  });
})();
