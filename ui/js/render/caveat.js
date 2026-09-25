// Responsibility: Put a warning where it cannot be read past - a popup that interrupts, and a card that stays.
// Owns: the popup stack and the rule that dismissing a popup never destroys the warning.
// Boundaries: it renders what it is given; it decides nothing about which text is a warning.

/* A WARNING THAT SCROLLS PAST IS A WARNING NOBODY READ.
 *
 * `plan.summary_for_user` is the geometry agent's own sentence to the customer - on a real job
 * it read "the outlet o6 has only 1.2 D of run - extend it or accept the mesh and not quote the
 * o6 pressure drop". It now rides on the confirmation turn, which is the last turn before
 * compute is spent, and it arrived as one more grey paragraph in a wall of grey paragraphs.
 *
 * Two surfaces, and the pairing is the design:
 *   the POPUP interrupts, because this is the turn where a caveat is still worth something;
 *   the CARD stays in the transcript, because a popup can be dismissed with a stray click and a
 *   warning that can be destroyed by a stray click is worse than one that was never shown.
 * Dismissing the popup therefore removes the popup ONLY. The card is already in the conversation
 * and the popup's own button scrolls back to it.
 */
import { esc, mdBlock } from "../core/format.js";

const TITLE = "What the geometry agent found";

/** The card that stays in the transcript. `mdBlock` because the agent writes the customer
 *  bullets and bold, and it is the same renderer the rest of the conversation goes through. */
export function buildCaveat(text) {
  const el = document.createElement("div");
  el.className = "cav";
  el.innerHTML = `<div class="cav-h"><span class="cav-mark" aria-hidden="true">!</span>`
    + `<span class="cav-t">${esc(TITLE)}</span>`
    + `<span class="cav-when">before you say go</span></div>`
    + `<div class="cav-b">${mdBlock(text)}</div>`;
  return el;
}

function stack() {
  let host = document.getElementById("pop-stack");
  if (!host) {
    host = document.createElement("div");
    host.id = "pop-stack";
    // aria-live rather than role="alertdialog": it must be ANNOUNCED without stealing the
    // caret, because the customer is mid-sentence in the composer when this arrives.
    host.setAttribute("role", "region");
    host.setAttribute("aria-live", "assertive");
    host.setAttribute("aria-label", "Warnings");
    document.body.appendChild(host);
  }
  return host;
}

/** Raise the popup for a caveat already mounted at `anchor`.
 *
 *  It does not auto-dismiss. Everything else in this page that pops up is transient status -
 *  offline, a retry - and goes away on a timer; a caveat about the mesh that is about to be
 *  built is the one thing that must wait to be read. */
export function popCaveat(text, anchor) {
  const pop = document.createElement("div");
  pop.className = "pop pop-warn";
  // The preview is one flattened line and is NOT rendered as markdown - a two-line popup with a
  // half-formatted list in it is harder to read than plain words. The emphasis marks come off
  // rather than being shown raw, so the preview does not read as "**o6**".
  const short = String(text)
    .replace(/\*\*(.+?)\*\*/g, "$1").replace(/`(.+?)`/g, "$1")
    .replace(/^\s*[-*]\s+/gm, "· ")
    .replace(/\s+/g, " ").trim();
  pop.innerHTML = `<div class="pop-h"><span class="pop-mark" aria-hidden="true">!</span>`
    + `<span class="pop-t">${esc(TITLE)}</span>`
    + `<button type="button" class="pop-x" aria-label="Dismiss this warning">×</button></div>`
    + `<div class="pop-b">${esc(short.length > 220 ? short.slice(0, 220) + "…" : short)}</div>`
    + `<div class="pop-f"><button type="button" class="pop-go">Read it in full</button>`
    + `<span class="pop-keep">stays in the conversation</span></div>`;
  pop.querySelector(".pop-x").onclick = () => pop.remove();
  pop.querySelector(".pop-go").onclick = () => {
    if (anchor && anchor.scrollIntoView) anchor.scrollIntoView({ block: "center" });
    if (anchor) {
      anchor.classList.add("cav-flash");
      setTimeout(() => anchor.classList.remove("cav-flash"), 1400);
    }
    pop.remove();
  };
  stack().appendChild(pop);
  return pop;
}
