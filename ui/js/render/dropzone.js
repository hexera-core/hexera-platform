// Responsibility: Seat the upload button in the empty stage's drop zone, and hand it back when the stage fills.
// Boundaries: moves the one existing control; it binds no upload listener and sends nothing (shell/composer.js does).

/* THE DROP ZONE. Before there is a file the stage is one invitation: drop a file, or browse for
 * one. The button is the SAME #upload-btn the composer wires (click -> the file picker, then its
 * progress words, then its removal after a good upload); it is only moved, so every listener and
 * every test that finds it by id keeps working. When the stage fills it goes back to the bar. */

const $ = (id) => document.getElementById(id);
// the composer's own words for the button after a failed upload; in the drop zone it reads as a
// browse button again
const RESET_WORDS = "Upload geometry";
const BROWSE_WORDS = "Browse files";
let watcher = null;

export function seatUploadButton() {
  const slot = document.querySelector("#empty .drop-slot"), btn = $("upload-btn");
  if (!slot || !btn) return;
  if (btn.parentElement !== slot) slot.appendChild(btn);
  if (btn.textContent.trim() === RESET_WORDS) btn.textContent = BROWSE_WORDS;
  if (!watcher && typeof MutationObserver === "function") {
    watcher = new MutationObserver(() => {
      if (btn.closest("#empty") && btn.textContent.trim() === RESET_WORDS) btn.textContent = BROWSE_WORDS;
    });
    watcher.observe(btn, { childList: true, characterData: true, subtree: true });
  }
}

export function returnUploadButton() {
  const btn = $("upload-btn"), bar = $("upload-bar");
  if (!btn || !bar || !btn.closest("#empty")) return;
  const focused = document.activeElement === btn;
  bar.appendChild(btn);
  if (btn.textContent.trim() === BROWSE_WORDS) btn.textContent = RESET_WORDS;
  if (focused) btn.focus();
}
