import React from "react";

import {
  APPLIES,
  MAX_CHARS,
  PLACEHOLDER,
  type MeshingStandard,
  isNotApplied,
  rowsFor,
  savedBy,
  statusLine,
} from "@/app/_components/meshing-standard";

type MeshingStandardViewProps = {
  /** what the box holds now (the saved words until the user types) */
  draft: string;
  /** a save is on its way: the reader can take up to a minute */
  busy: boolean;
  /** a failed save, said in plain words */
  error: string | null;
  saved: MeshingStandard;
  onChange?: (text: string) => void;
  onClear?: () => void;
  /** a plain submit handler, not a form action: a form action's state updates wait for the action to finish,
   *  so "Saving…" would never be seen while the reader reads */
  onSave?: () => void;
};

/**
 * THE MESHING SETTINGS, AS DRAWN: one box, one line on where it applies, Save and Clear, and what the reader
 * understood - each line exactly as a run will say it. No hooks: the page's client panel holds the state, and
 * this renders the same markup on the server, in the browser and in a test.
 */
export function MeshingStandardView({
  busy,
  draft,
  error,
  onChange,
  onClear,
  onSave,
  saved,
}: MeshingStandardViewProps) {
  const said = statusLine(saved);
  const by = savedBy(saved);
  const changed = draft.trim() !== saved.text.trim();

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSave?.();
      }}
      style={{ display: "grid", gap: ".9rem" }}
    >
      <label className="auth__field">
        <span className="label">Your meshing standard</span>
        <textarea
          className="auth__input"
          disabled={busy}
          maxLength={MAX_CHARS}
          name="text"
          onChange={onChange ? (event) => onChange(event.target.value) : undefined}
          placeholder={PLACEHOLDER}
          rows={rowsFor(draft)}
          spellCheck={false}
          style={{ font: "inherit", lineHeight: 1.55, padding: ".7rem .8rem", resize: "vertical" }}
          value={draft}
        />
      </label>
      <p className="page__sub">{APPLIES}</p>

      <div style={{ alignItems: "center", display: "flex", flexWrap: "wrap", gap: ".6rem" }}>
        <button className="btn btn--gold" disabled={busy || !changed} type="submit">
          {busy ? "Saving…" : "Save"}
        </button>
        <button className="btn" disabled={busy || !draft} onClick={onClear} type="button">
          Clear
        </button>
        {by && !changed ? <span className="page__sub">{by}</span> : null}
      </div>

      {error ? (
        <div className="auth__error" role="status">
          {error}
        </div>
      ) : null}

      {said || saved.lines.length ? (
        <section aria-live="polite" className="card" style={{ display: "grid", gap: ".5rem" }}>
          {saved.lines.length ? <p className="label">What we understood</p> : null}
          {said ? <p>{said}</p> : null}
          {saved.lines.length ? (
            <ul style={{ display: "grid", gap: ".3rem", listStyle: "none", margin: 0, padding: 0 }}>
              {saved.lines.map((line, i) => (
                <li
                  key={`${i}:${line}`}
                  style={isNotApplied(line) ? { color: "var(--text-muted)" } : undefined}
                >
                  {line}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}
    </form>
  );
}
