/*
 * Copy-to-clipboard, available on any page.
 *
 * Two forms:
 *   <button data-copy-target="#element-id">Copy</button>   copies that element's text
 *   <button data-copy-value="literal text">Copy</button>   copies the literal
 *
 * Buttons start hidden and are revealed only where the clipboard API actually
 * works, so a browser without it (or a page served over plain http from another
 * host) shows no control that would silently do nothing. The text stays
 * selectable either way, so copying by hand is always possible.
 */
(() => {
  const buttons = document.querySelectorAll("[data-copy-target], [data-copy-value]");
  if (!buttons.length) return;
  if (!(navigator.clipboard && window.isSecureContext)) return;

  const label = (button, text) => {
    const original = button.dataset.label || button.textContent;
    button.dataset.label = original;
    button.textContent = text;
    window.setTimeout(() => { button.textContent = original; }, 1500);
  };

  buttons.forEach((button) => {
    const source = button.dataset.copyTarget
      ? document.querySelector(button.dataset.copyTarget)
      : null;
    // A target that does not exist would copy nothing; leave the button hidden.
    if (button.dataset.copyTarget && !source) return;

    button.hidden = false;
    button.addEventListener("click", async () => {
      const text = source ? source.textContent.trim() : button.dataset.copyValue;
      try {
        await navigator.clipboard.writeText(text);
        label(button, "Copied");
      } catch {
        label(button, "Copy failed");
      }
    });
  });
})();
