/*
 * Progressive enhancement only.
 *
 * Every control on this page is a real <form method="post"> that works with
 * JavaScript disabled and lands on the synchronous server path. This file only
 * intercepts those forms to make them feel immediate. Nothing here is ever the
 * only way to do something.
 *
 * Model output is never rendered as markup by this file. Streaming text is
 * inserted with textContent; when a stream ends the finished message is replaced
 * by a server-rendered fragment produced by the trusted chat_text filter, so the
 * escaping rules live in exactly one place.
 */
(() => {
  const form = document.getElementById("chat-composer");
  if (!form) return;

  const input = form.querySelector("textarea");
  const messages = document.getElementById("chat-messages");
  const sendButton = document.getElementById("chat-send");
  const status = document.getElementById("chat-status");
  const csrf = form.querySelector("[name=csrfmiddlewaretoken]");
  const modeSelect = document.getElementById("chat-mode");

  const canStream =
    "EventSource" in window && modeSelect && modeSelect.value !== "search";

  if (!new URLSearchParams(location.search).has("messages")) {
    messages.scrollTop = messages.scrollHeight;
  }

  const scrollDown = () => { messages.scrollTop = messages.scrollHeight; };

  let submitting = false;
  let active = null;

  const setBusy = (busy) => {
    submitting = busy;
    sendButton.disabled = busy && !active;
    status.hidden = !busy;
    if (busy) form.setAttribute("aria-busy", "true");
    else form.removeAttribute("aria-busy");
  };

  const bubble = (role, text) => {
    const article = document.createElement("article");
    article.className = `chat-message chat-${role}`;
    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = role === "user" ? "You" : "Digital Brain";
    const body = document.createElement("div");
    body.className = "message-body";
    body.textContent = text || "";
    article.append(meta, body);
    const empty = messages.querySelector(".chat-empty");
    if (empty) empty.remove();
    messages.append(article);
    scrollDown();
    return { article, body, meta };
  };

  const showStop = (on) => {
    sendButton.textContent = on ? "Stop" : "Send";
    sendButton.dataset.state = on ? "stop" : "send";
    sendButton.disabled = false;
  };

  const finish = async (payload, assistant, text) => {
    if (active) { active.close(); active = null; }
    showStop(false);
    setBusy(false);
    try {
      const response = await fetch(payload.fragment, {
        headers: { "X-Requested-With": "fetch" },
      });
      if (response.ok) {
        const holder = document.createElement("div");
        // Server-rendered, already escaped by the chat_text filter.
        holder.innerHTML = await response.text();
        const rendered = holder.firstElementChild;
        if (rendered) {
          assistant.article.replaceWith(rendered);
          wireMessage(rendered);
        }
      }
    } catch {
      assistant.body.textContent = text;
    }
    assistant.article.removeAttribute("data-status");
    scrollDown();
  };

  const stream = (payload) => {
    const assistant = bubble("assistant", "");
    assistant.article.dataset.status = "streaming";
    let text = "";
    let tools = null;
    showStop(true);

    const source = new EventSource(payload.stream);
    active = source;

    source.addEventListener("delta", (event) => {
      try {
        text += JSON.parse(event.data).t || "";
      } catch { return; }
      assistant.body.textContent = text;
      scrollDown();
    });

    source.addEventListener("tool", (event) => {
      let info;
      try { info = JSON.parse(event.data); } catch { return; }
      if (!tools) {
        tools = document.createElement("p");
        tools.className = "chat-tool";
        assistant.article.insertBefore(tools, assistant.body);
      }
      tools.textContent = `Searching sources: ${info.detail || info.name}`;
      scrollDown();
    });

    source.addEventListener("title", (event) => {
      let name;
      try { name = JSON.parse(event.data).title; } catch { return; }
      if (!name) return;
      const heading = document.querySelector(".conversation-title h2");
      if (heading) heading.textContent = name;
      const field = document.getElementById("rename-title");
      if (field) field.value = name;
      const current = document.querySelector(".conversation-link[aria-current]");
      if (current) {
        const label = current.querySelector("strong");
        if (label) label.textContent = name;
      }
    });

    source.addEventListener("error", (event) => {
      let message = "The answer could not be completed.";
      try { message = JSON.parse(event.data).message || message; } catch { /* transport */ }
      assistant.body.textContent = text || message;
      finish(payload, assistant, text || message);
    });

    source.addEventListener("done", () => {
      if (tools) tools.remove();
      finish(payload, assistant, text);
    });

    // Transport-level failure, distinct from a server "error" frame.
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) finish(payload, assistant, text);
    };
  };

  const stop = async () => {
    if (!active || !active.stopUrl) return;
    try {
      await fetch(active.stopUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrf.value, "X-Requested-With": "fetch" },
      });
    } catch { /* the stream will end on its own */ }
  };

  form.addEventListener("submit", async (event) => {
    if (sendButton.dataset.state === "stop") {
      event.preventDefault();
      stop();
      return;
    }
    if (submitting) { event.preventDefault(); return; }
    if (!canStream) { setBusy(true); return; }

    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    setBusy(true);
    bubble("user", question);
    input.value = "";

    let payload;
    try {
      const response = await fetch(form.action || location.pathname, {
        method: "POST",
        headers: {
          "X-CSRFToken": csrf.value,
          "X-Digital-Brain-Stream": "1",
          "X-Requested-With": "fetch",
        },
        body: formData(question),
      });
      if (!response.ok) throw new Error("start failed");
      payload = await response.json();
    } catch {
      // Fall back to the plain form post rather than losing the question.
      input.value = question;
      setBusy(false);
      form.submit();
      return;
    }
    const hidden = form.querySelector("[name=conversation]");
    if (!hidden) {
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = "conversation";
      field.value = payload.conversation;
      form.append(field);
    }
    stream(payload);
    active.stopUrl = payload.stop;
  });

  function formData(question) {
    const data = new FormData();
    data.append("csrfmiddlewaretoken", csrf.value);
    data.append("question", question);
    const hidden = form.querySelector("[name=conversation]");
    if (hidden) data.append("conversation", hidden.value);
    if (modeSelect) data.append("mode", modeSelect.value);
    return data;
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  // Restores the composer when the page is reached through bfcache.
  window.addEventListener("pageshow", () => {
    if (active) { active.close(); active = null; }
    showStop(false);
    setBusy(false);
  });

  // The rename form ships visible so it works without JavaScript. Only once JS is
  // running do we collapse it behind a toggle.
  const renameForm = document.getElementById("rename-form");
  const renameToggle = document.querySelector("[data-rename-toggle]");
  if (renameForm && renameToggle) {
    renameForm.classList.add("collapsed");
    renameToggle.hidden = false;
    renameToggle.addEventListener("click", () => {
      renameForm.classList.toggle("collapsed");
      if (!renameForm.classList.contains("collapsed")) {
        const field = renameForm.querySelector("input[type=text]");
        field.focus();
        field.select();
      }
    });
  }

  document.querySelectorAll("form[data-confirm]").forEach((item) => {
    item.addEventListener("submit", (event) => {
      if (!window.confirm(item.dataset.confirm)) event.preventDefault();
    });
  });

  function wireMessage(root) {
    if (!(navigator.clipboard && window.isSecureContext)) return;
    root.querySelectorAll("[data-copy]").forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", async () => {
        const body = button.closest(".chat-message").querySelector(".message-body");
        try {
          await navigator.clipboard.writeText(body.textContent.trim());
          const original = button.textContent;
          button.textContent = "Copied";
          window.setTimeout(() => { button.textContent = original; }, 1500);
        } catch {
          button.textContent = "Copy failed";
        }
      });
    });
  }

  messages.querySelectorAll(".chat-message").forEach(wireMessage);
})();
