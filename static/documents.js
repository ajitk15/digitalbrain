// Keep in-flight work current without a manual refresh.
//
// Two mechanisms, for two kinds of page.
//
// Sources and Code Factory runs ([data-live]): each row, card or run stage
// that is still moving is refreshed in place. The page is fetched again and only elements whose
// data-live key matches are swapped, so an open intake panel, a half-typed
// link, an expanded origin or a graph mid-pan is left exactly as it was. The
// markup comes from the server's own templates; nothing is built here. Polling
// stops once the fetched page has nothing left in flight.
//
// Everything else ([data-document-pending]): the old behaviour - one reload
// after a pause, skipped entirely once the reader starts doing something. The
// graph explorer raises pointer and wheel events, not input or change, so it is
// listened to separately; a reload mid-exploration would discard zoom and pan.
const LIVE_INTERVAL = 2500;

// Anything the reader has changed and not yet sent: typed text, a ticked box.
function dirty(node) {
  return Array.from(node.querySelectorAll("input, textarea, select")).some((field) =>
    field.type === "checkbox" || field.type === "radio"
      ? field.checked !== field.defaultChecked
      : field.tagName === "SELECT"
        ? Array.from(field.options).some((option) => option.selected !== option.defaultSelected)
        : field.value !== field.defaultValue
  );
}

// A disclosure the reader opened or closed keeps that state across a swap; one
// they never touched follows the server, which opens whichever stage is current.
document.addEventListener("click", (event) => {
  const summary = event.target.closest("summary");
  if (summary && summary.parentElement.tagName === "DETAILS") {
    summary.parentElement.dataset.liveTouched = "true";
  }
});

function keepDisclosures(node, replacement) {
  const before = [node, ...node.querySelectorAll("details")];
  const after = [replacement, ...replacement.querySelectorAll("details")];
  if (before.length !== after.length) return;
  before.forEach((old, index) => {
    if (old.tagName === "DETAILS" && old.dataset.liveTouched) {
      after[index].open = old.open;
      after[index].dataset.liveTouched = "true";
    }
  });
}

function liveKeys(root) {
  return new Map(
    Array.from(root.querySelectorAll("[data-live]"), (node) => [node.dataset.live, node])
  );
}

// What one refresh found: "pending" (keep going), "done", or "failed" (the
// page could not be fetched - a dropped connection, a restarting server, or a
// session that expired and redirected to sign-in).
async function refreshLive() {
  let fresh;
  try {
    const response = await fetch(window.location.href, {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
      redirect: "error",
    });
    if (!response.ok) return "failed";
    fresh = new DOMParser().parseFromString(await response.text(), "text/html");
  } catch {
    return "failed";
  }
  const incoming = liveKeys(fresh);
  let waiting = false;
  liveKeys(document).forEach((node, key) => {
    const replacement = incoming.get(key);
    if (!replacement) return;
    // A row the reader is working in (a focused Retry, a half-written review
    // note, gaps they have unticked) waits until they are done with it - and
    // polling waits with it, or a run that finished meanwhile would leave the
    // row showing its old state for good.
    if (node.contains(document.activeElement) || dirty(node)) {
      if (replacement.outerHTML !== node.outerHTML) waiting = true;
      return;
    }
    const imported = document.importNode(replacement, true);
    keepDisclosures(node, imported);
    if (imported.outerHTML !== node.outerHTML) node.replaceWith(imported);
  });
  localTimes(document);
  return fresh.querySelector("[data-live-pending]") !== null || waiting ? "pending" : "done";
}

// Said once, in one place, while updates cannot be fetched; gone when they can.
function liveStatus(text) {
  let note = document.getElementById("live-status");
  if (!text) {
    if (note) note.remove();
    return;
  }
  if (!note) {
    note = document.createElement("p");
    note.id = "live-status";
    note.className = "live-status";
    note.setAttribute("role", "status");
    document.body.append(note);
  }
  note.textContent = text;
}

// Failures in a row before giving up and saying so. With backoff doubling from
// the normal interval to a 30 second ceiling, that is about two minutes.
const LIVE_ATTEMPTS = 8;

if (document.querySelector("[data-live-pending]")) {
  let failures = 0;
  let lastGood = new Date();
  const tick = async () => {
    // An open dialog (a gap's evidence, say) is reading; wait until it closes.
    const paused = document.hidden || document.querySelector("dialog[open]");
    const result = paused ? "pending" : await refreshLive();
    if (result === "failed") {
      failures += 1;
      const since = lastGood.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      if (failures >= LIVE_ATTEMPTS) {
        liveStatus(
          `Updates stopped: this page could not reach the server (last updated ${since}). ` +
            "The work carries on. Reload the page to see where it is."
        );
        return;
      }
      liveStatus(`Connection interrupted - retrying. Last updated ${since}.`);
      window.setTimeout(tick, Math.min(30000, LIVE_INTERVAL * 2 ** failures));
      return;
    }
    if (!paused) {
      failures = 0;
      lastGood = new Date();
      liveStatus("");
    }
    if (result === "pending") window.setTimeout(tick, LIVE_INTERVAL);
  };
  window.setTimeout(tick, LIVE_INTERVAL);
}

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

// A folder upload's subfolders. Django keeps only a file's own name, so each
// file's path inside the folder travels beside it, in the order the files are
// sent. Without this the folder still uploads; every file keeps its bare name.
document.addEventListener("change", (event) => {
  const input = event.target.closest("input[data-folder-input]");
  if (!input || !input.form) return;
  const paths = input.form.querySelector("input[name=paths]");
  if (!paths) return;
  paths.value = JSON.stringify(
    Array.from(input.files, (file) => file.webkitRelativePath || file.name)
  );
});

// Times are rendered in UTC so the page reads the same everywhere and without
// script. Here they are shown in the reader's own time, with the UTC original
// kept as the tooltip. Nothing changes where the reader is on UTC already.
function localTimes(root) {
  root.querySelectorAll("time[data-local]:not([data-localised])").forEach((node) => {
    const when = new Date(node.getAttribute("datetime"));
    if (Number.isNaN(when.getTime()) || when.getTimezoneOffset() === 0) return;
    node.title = node.textContent.trim();
    node.textContent = when.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
    node.dataset.localised = "true";
  });
}
localTimes(document);

// A first-visit note, dismissed once and remembered in this browser. Without
// script, or where storage is refused, it simply stays.
document.querySelectorAll("[data-first-visit]").forEach((note) => {
  const key = `digital-brain.first-visit.${note.dataset.firstVisit}`;
  try {
    if (localStorage.getItem(key)) {
      note.remove();
      return;
    }
  } catch {
    return;
  }
  const button = note.querySelector("[data-dismiss]");
  if (!button) return;
  button.hidden = false;
  button.addEventListener("click", () => {
    try {
      localStorage.setItem(key, "dismissed");
    } catch {
      /* storage refused: the note goes for this page view only */
    }
    note.remove();
  });
});
