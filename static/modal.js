/*
 * Popups for short flows that used to be whole pages.
 *
 * Usage is one attribute on a real link:
 *
 *   <a data-modal href="{% url 'application-new' product.pk %}">+ Application</a>
 *
 * The href must be a page that already works on its own. This script only
 * intercepts: with JavaScript off, or if anything here fails, the link navigates
 * and the same form is filled in on its own page. That is the progressive
 * enhancement contract in CLAUDE.md, and it is why no view needed a new
 * "fragment" mode - the dialog reuses the ordinary page and lifts <main> out of
 * it, exactly as chat's fragment endpoint reuses one template for both paths.
 *
 * Submitting works the same way. The form is posted with fetch; a redirect means
 * the view accepted it, so the browser follows to wherever it pointed. A response
 * that comes back at the same URL is a re-rendered form with validation errors,
 * so it replaces the dialog's contents and the person keeps their typing.
 *
 * CSP notes (script-src 'self', style-src 'self'): no inline script, no inline
 * style, no onclick. Positioning is <dialog>'s own centring and everything else
 * is a class. connect-src 'self' is what allows the fetch.
 */
(() => {
  const dialog = document.createElement("dialog");
  dialog.className = "modal";
  dialog.id = "app-modal";
  const body = document.createElement("div");
  body.className = "modal-body";
  dialog.append(body);

  let opener = null;

  const close = () => {
    if (dialog.open) dialog.close();
  };

  dialog.addEventListener("close", () => {
    body.replaceChildren();
    // Return focus to whatever opened this, or the page loses its place.
    if (opener && opener.isConnected) opener.focus();
    opener = null;
  });

  // A click on the backdrop lands on the dialog element itself; a click on the
  // content lands on a descendant. Comparing the target tells them apart.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) close();
  });

  const fail = (url) => {
    // Never strand someone in a broken popup: fall back to the real page.
    close();
    window.location.href = url;
  };

  const wire = (url) => {
    body.querySelectorAll("form").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        // Leave a form the confirm handler in copy.js has already cancelled,
        // and leave uploads alone - they belong on their own page.
        if (event.defaultPrevented) return;
        if (form.enctype === "multipart/form-data") return;
        event.preventDefault();
        const data = new FormData(form);
        // A named submit button carries meaning here (save vs revoke, on vs
        // off); FormData does not include it, so add it back.
        const submitter = event.submitter;
        if (submitter && submitter.name) data.append(submitter.name, submitter.value);
        const action = form.getAttribute("action") || url;
        try {
          const response = await fetch(action, {
            method: (form.method || "post").toUpperCase(),
            body: data,
            headers: { "X-Requested-With": "fetch" },
            credentials: "same-origin",
            redirect: "follow",
          });
          if (!response.ok) return fail(action);
          const landed = new URL(response.url, window.location.href);
          const posted = new URL(action, window.location.href);
          if (landed.pathname !== posted.pathname) {
            // The view redirected, so it accepted the submission.
            window.location.href = landed.href;
            return;
          }
          // Same URL: a re-rendered form carrying validation errors.
          const markup = await response.text();
          render(markup, action);
        } catch {
          fail(action);
        }
      });
    });

    body.querySelectorAll("[data-modal-close]").forEach((control) => {
      control.addEventListener("click", (event) => {
        event.preventDefault();
        close();
      });
    });
  };

  const render = (markup, url) => {
    const parsed = new DOMParser().parseFromString(markup, "text/html");
    const main = parsed.querySelector("main");
    if (!main) return fail(url);
    // Page furniture that makes no sense inside a popup. The heading stays: it
    // is the dialog's title.
    main
      .querySelectorAll(".breadcrumbs, .application-menu, .tabs, .skip-link")
      .forEach((node) => node.remove());
    body.replaceChildren(...main.childNodes);
    if (!body.querySelector("[data-modal-close]")) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "button secondary";
      cancel.textContent = "Cancel";
      cancel.setAttribute("data-modal-close", "");
      const actions = body.querySelector(".actions") || body.lastElementChild;
      if (actions) actions.append(cancel);
    }
    wire(url);
    const focusable = body.querySelector(
      "input:not([type=hidden]), select, textarea, button"
    );
    if (focusable) focusable.focus();
  };

  document.addEventListener("click", async (event) => {
    const link = event.target.closest ? event.target.closest("a[data-modal]") : null;
    if (!link) return;
    // Let people open the underlying page deliberately.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    opener = link;
    const url = link.href;
    try {
      const response = await fetch(url, {
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      });
      if (!response.ok) return fail(url);
      render(await response.text(), url);
      if (!dialog.open) dialog.showModal();
    } catch {
      fail(url);
    }
  });

  document.addEventListener("DOMContentLoaded", () => document.body.append(dialog));
})();
