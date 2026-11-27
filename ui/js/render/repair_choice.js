// Responsibility: The geometry check's "what should we do with your CAD?" choice - Repair + mesh, Repair CAD only,
//                 Mesh only - and the run's "accept repairs without asking" box: drawn in every CAD form, read on confirm.
// Owns: the choice's markup and how a form's answer becomes the confirm body's `repair_goal` / `repair_auto_accept`.
// Boundaries: words come from core/repair_words.js; the forms (render/geometry_form.js, render/fea_form.js) place it
//             and their readers add what it reads to the body they already send. It decides no repair.

import { esc } from "../core/format.js";
import { CHOICES, choiceKey } from "../core/repair_words.js";

let _n = 0;

/** What the check proposal says about the choice: the goal it carries (the intake may have set it from the chat -
 *  "just fix my STEP"), whether the chat set it, and the run's auto-accept default (the organisation's setting). */
export function choiceOf(p) {
  const q = p || {};
  return {
    goal: choiceKey(q.repair_goal),
    fromChat: !!q.repair_goal && (q.repair_goal_source === "chat" || q.repair_goal_source === "intake"),
    autoAccept: !!q.repair_auto_accept,
    // the organisation's permission (Settings, Meshing): without it the box stays unavailable, and says why
    autoAcceptAllowed: q.repair_auto_accept_allowed !== false,
  };
}

/** The choice as HTML: three radios, each with one short line of help, and the auto-accept box. */
export function repairChoiceHtml(p) {
  const c = choiceOf(p), id = `rc-choice-${++_n}`;
  const opts = CHOICES.map((o) => {
    const oid = `${id}-${o.key}`;
    return `<label class="rc-opt" for="${oid}"><input type="radio" id="${oid}" name="${id}" value="${esc(o.key)}"`
      + `${o.key === c.goal ? " checked" : ""} aria-describedby="${oid}-help">`
      + `<span class="rc-opt-t"><b>${esc(o.label)}</b><span class="rc-opt-h" id="${oid}-help">${esc(o.help)}</span></span></label>`;
  }).join("");
  const said = c.fromChat ? `<p class="rc-said">Set from your message. Change it here if you like.</p>` : "";
  const off = c.goal === "off";
  return `<fieldset class="rc-choice" data-start="${esc(c.goal)}">
      <legend class="rc-k">What should we do with your CAD?</legend>
      ${said}
      <div class="rc-opts">${opts}</div>
      <label class="rc-auto"${c.autoAcceptAllowed ? "" : ' data-locked="1"'}>
        <input type="checkbox" class="rc-auto-box" aria-describedby="${id}-auto-help"${c.autoAccept && c.autoAcceptAllowed ? " checked" : ""}`
        + `${off || !c.autoAcceptAllowed ? " disabled" : ""}>
        <span>Accept repairs without asking (skip review)
          <span class="rc-opt-h" id="${id}-auto-help">${c.autoAcceptAllowed
            ? "Larger changes, like closing a gap, are normally shown to you before they are kept."
            : "Your organisation shows larger repairs for review. An admin can allow skipping it in Settings, Meshing."}</span></span>
      </label>
    </fieldset>`;
}

/** What the form says: `{repair_goal, repair_auto_accept}`, or {} when the form has no choice on it. */
export function readRepairChoice(root) {
  const box = root && root.querySelector ? root.querySelector(".rc-choice") : null;
  if (!box) return {};
  const on = box.querySelector('input[type="radio"]:checked');
  const goal = choiceKey(on ? on.value : box.dataset.start);
  const auto = box.querySelector(".rc-auto-box");
  return { repair_goal: goal, repair_auto_accept: goal !== "off" && !!(auto && auto.checked && !auto.disabled) };
}

/* "Mesh only" repairs nothing, so there is nothing to accept: the box follows the choice, in every form at once. */
if (typeof document !== "undefined") {
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (!t || !t.matches || !t.matches('.rc-choice input[type="radio"]')) return;
    const box = t.closest(".rc-choice").querySelector(".rc-auto-box");
    if (box && !box.closest(".rc-auto").dataset.locked) box.disabled = t.value === "off";
  });
}
