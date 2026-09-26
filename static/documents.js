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

async function refreshLive() {
  let fresh;
  try {
    const response = await fetch(window.location.href, {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
      redirect: "error",
    });
    if (!response.ok) return false;
    fresh = new DOMParser().parseFromString(await response.text(), "text/html");
  } catch {
    return false;
  }
  const incoming = liveKeys(fresh);
  liveKeys(document).forEach((node, key) => {
    const replacement = incoming.get(key);
    // A row the reader is working in (a focused Retry, a half-written review
    // note, gaps they have unticked) waits until they are done with it.
    if (!replacement || node.contains(document.activeElement) || dirty(node)) return;
    const incoming = document.importNode(replacement, true);
    keepDisclosures(node, incoming);
    if (incoming.outerHTML !== node.outerHTML) node.replaceWith(incoming);
  });
  return fresh.querySelector("[data-live-pending]") !== null;
}

if (document.querySelector("[data-live-pending]")) {
  const tick = async () => {
    // An open dialog (a gap's evidence, say) is reading; wait until it closes.
    const paused = document.hidden || document.querySelector("dialog[open]");
    const more = paused ? true : await refreshLive();
    if (more) window.setTimeout(tick, LIVE_INTERVAL);
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
