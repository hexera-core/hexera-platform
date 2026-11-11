import assert from "node:assert/strict";
import test from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  APPLIES,
  MAX_CHARS,
  PLACEHOLDER,
  SCOPE,
  type MeshingStandard,
  isNotApplied,
  saveFailureMessage,
  savedBy,
  statusLine,
} from "./meshing-standard";
import { MeshingStandardView } from "./meshing-standard-view";

const CRITERIA = "Global Mesh Size = 10 mm\nAspect Ratio < 4.0 (1% between 4.0 and 6.0 is acceptable)\nNastran deck";

function standard(over: Partial<MeshingStandard> = {}): MeshingStandard {
  return {
    text: CRITERIA,
    lines: [
      "Global element size 10 mm.",
      "Aspect ratio under 4 (up to 1% of elements between 4 and 6).",
      'Not applied: "Nastran deck" - no mesh setting of ours matches it yet.',
    ],
    status: "read",
    updated_by: "engineer@example.com",
    updated_at: "2026-11-11T09:30:00+00:00",
    ...over,
  };
}

const EMPTY: MeshingStandard = { text: "", lines: [], status: "empty", updated_by: null, updated_at: null };

function render(saved: MeshingStandard, over: { draft?: string; busy?: boolean; error?: string | null } = {}) {
  return renderToStaticMarkup(
    createElement(MeshingStandardView, {
      busy: over.busy ?? false,
      draft: over.draft ?? saved.text,
      error: over.error ?? null,
      saved,
    }),
  );
}

// ------------------------------------------------------------------ the words ------------------------------------
test("the page says whose standard it is and where it applies, in one line each", () => {
  assert.equal(SCOPE, "One standard for your whole team, shared by everyone in this organisation.");
  assert.equal(APPLIES, "Applied to every run. What you write in a run's notes wins for that run.");
  assert.match(PLACEHOLDER, /^Global Mesh Size = 10 mm\n/);
  assert.ok(PLACEHOLDER.split("\n").length <= 4, "the example stays short");
});

test("an unread standard is saved and said so; a read one lets its lines speak", () => {
  assert.equal(
    statusLine(standard({ status: "unread", lines: [] })),
    "Saved. We couldn't read it just now, so each run reads it when it starts.",
  );
  assert.equal(statusLine(standard()), null);
  assert.equal(statusLine(EMPTY), null);
  assert.equal(statusLine(standard({ lines: [] })), "Saved. Nothing in it is a mesh setting we can apply.");
});

test("who saved it and when, in plain words", () => {
  assert.equal(savedBy(standard()), "Saved 11 Nov 2026 by engineer@example.com.");
  assert.equal(savedBy(standard({ updated_by: null })), "Saved 11 Nov 2026.");
  assert.equal(savedBy(EMPTY), null);
  assert.equal(savedBy(standard({ updated_at: "not a date" })), null);
});

test("a failed save says what happened without inventing a cause", () => {
  assert.equal(saveFailureMessage(403), "Only a console session can change the standard.");
  assert.equal(saveFailureMessage(404), "Your account has no organisation to hold a standard.");
  assert.equal(saveFailureMessage(422), "That is longer than 8,000 characters. Shorten it and save again.");
  assert.equal(saveFailureMessage(500), "Could not save just now. Try again.");
  assert.equal(saveFailureMessage(0), "Could not save just now. Try again.");
  assert.equal(MAX_CHARS, 8000);
});

test("a not-applied line is told apart from a line that applies", () => {
  assert.equal(isNotApplied('Not applied: "Nastran deck" - no mesh setting of ours matches it yet.'), true);
  assert.equal(isNotApplied("Global element size 10 mm."), false);
});

// ------------------------------------------------------------------ the page, rendered ---------------------------
test("a read standard shows the box, the line, Save and Clear, and every line exactly as a run says it", () => {
  const html = render(standard());
  assert.match(html, /<span class="label">Your meshing standard<\/span>/);
  assert.match(html, /<textarea[^>]*name="text"[^>]*>Global Mesh Size = 10 mm/);
  assert.match(html, /maxLength="8000"/);
  assert.match(html, /Applied to every run\. What you write in a run&#x27;s notes wins for that run\./);
  assert.match(html, />What we understood</);
  assert.match(html, /<li>Global element size 10 mm\.<\/li>/);
  assert.match(html, /<li>Aspect ratio under 4 \(up to 1% of elements between 4 and 6\)\.<\/li>/);
  assert.match(html, /<li style="color:var\(--text-muted\)">Not applied: &quot;Nastran deck&quot; - no mesh setting/);
  assert.match(html, /Saved 11 Nov 2026 by engineer@example\.com\./);
  // nothing else: two buttons, no per-field controls
  assert.equal((html.match(/<button/g) ?? []).length, 2);
  assert.match(html, />Save<\/button>/);
  assert.match(html, />Clear<\/button>/);
  assert.doesNotMatch(html, /<input|<select/);
});

test("Save waits for a change, and says it is saving while the reader reads", () => {
  assert.match(render(standard()), /<button class="btn btn--gold" disabled="" type="submit">Save<\/button>/);
  const edited = render(standard(), { draft: `${CRITERIA}\nJacobian > 0.6` });
  assert.match(edited, /<button class="btn btn--gold" type="submit">Save<\/button>/);
  assert.doesNotMatch(edited, /Saved 11 Nov 2026/, "an unsaved edit is not shown as saved");
  const busy = render(standard(), { busy: true, draft: `${CRITERIA}\nJacobian > 0.6` });
  assert.match(busy, />Saving…<\/button>/);
  assert.match(busy, /<textarea[^>]*disabled=""/);
});

test("an empty standard is just the box with its example", () => {
  const html = render(EMPTY);
  assert.match(html, /placeholder="Global Mesh Size = 10 mm\nAspect Ratio &lt; 4\.0/);
  assert.doesNotMatch(html, /What we understood/);
  assert.match(html, />Clear<\/button>/);
  assert.match(html, /<button class="btn" disabled="" type="button">Clear<\/button>/);
});

test("an unread standard says it was saved and will be read at run time", () => {
  const html = render(standard({ status: "unread", lines: [] }));
  assert.match(html, /What we understood/);
  assert.match(html, /Saved\. We couldn&#x27;t read it just now, so each run reads it when it starts\./);
  assert.doesNotMatch(html, /<ul/);
});

test("a failed save is shown where the user is looking", () => {
  const html = render(standard(), { error: saveFailureMessage(500) });
  assert.match(html, /<div class="auth__error" role="status">Could not save just now\. Try again\.<\/div>/);
});
