import React from "react";

import {
  APPLIES,
  MAX_CHARS,
  PLACEHOLDER,
  SHELL_LABEL,
  SHELL_OPTIONS,
  type MeshingStandard,
  type ShellElements,
  isNotApplied,
  rowsFor,
  savedBy,
  shellElementsOf,
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
  /** the switch as set now (the saved value until the user flips it) */
  kind?: ShellElements;
  onKind?: (kind: ShellElements) => void;
  onChange?: (text: string) => void;
  onClear?: () => void;
  /** a plain submit handler, not a form action: a form action's state updates wait for the action to finish,
   *  so "Saving…" would never be seen while the reader reads */
  onSave?: () => void;
};

/**
 * THE MESHING SETTINGS, AS DRAWN: one box, one switch (Shell elements: Quads | Triangles), one line on where they
 * apply, Save and Clear, and what the reader
 * understood - each line exactly as a run will say it. No hooks: the page's client panel holds the state, and
 * this renders the same markup on the server, in the browser and in a test.
 */
export function MeshingStandardView({
  busy,
  draft,
  error,
  kind,
  onChange,
  onClear,
  onKind,
  onSave,
  saved,
}: MeshingStandardViewProps) {
  const said = statusLine(saved);
  const by = savedBy(saved);
  const shown = kind ?? shellElementsOf(saved);
  const changed = draft.trim() !== saved.text.trim() || shown !== shellElementsOf(saved);

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
      <div style={{ alignItems: "center", display: "flex", gap: ".8rem" }}>
        <span className="label">{SHELL_LABEL}</span>
        <div
          aria-label={SHELL_LABEL}
          role="radiogroup"
          style={{ border: "1px solid var(--border-strong)", borderRadius: "var(--r-md)", display: "inline-flex", overflow: "hidden" }}
        >
          {SHELL_OPTIONS.map((o, i) => (
            <button
              aria-checked={shown === o.key}
              data-kind={o.key}
              disabled={busy}
              key={o.key}
              onClick={onKind ? () => onKind(o.key) : undefined}
              role="radio"
              style={{
                background: shown === o.key ? "var(--surface-raised)" : "transparent",
                border: 0,
                borderLeft: i ? "1px solid var(--border-strong)" : 0,
                color: shown === o.key ? "var(--text)" : "var(--text-faint)",
                cursor: busy ? "default" : "pointer",
                font: "inherit",
                padding: ".35rem .8rem",
              }}
              title={o.title}
              type="button"
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>
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
