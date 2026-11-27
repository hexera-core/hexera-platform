import React from "react";

import {
  AUTO_HELP,
  AUTO_LABEL,
  REPAIR_INTRO,
  type RepairSettings,
  scopeLine,
} from "@/app/_components/repair-settings";

type RepairSettingsViewProps = {
  saved: RepairSettings;
  /** the switch as set now (the saved value until the user flips it) */
  on: boolean;
  busy: boolean;
  error: string | null;
  onToggle?: (on: boolean) => void;
};

/**
 * THE CAD REPAIR SETTING, AS DRAWN: one switch and the line that says who may change it. No hooks: the page's client
 * panel holds the state, and this renders the same markup on the server, in the browser and in a test.
 */
export function RepairSettingsView({ busy, error, on, onToggle, saved }: RepairSettingsViewProps) {
  const locked = saved.can_change === false;
  return (
    <div className="repair-settings">
      <p className="page__sub">{REPAIR_INTRO}</p>
      <label className="repair-settings__switch" htmlFor="repair-auto-accept">
        <input
          aria-describedby="repair-auto-accept-help"
          checked={on}
          disabled={locked || busy}
          id="repair-auto-accept"
          onChange={(event) => onToggle?.(event.target.checked)}
          type="checkbox"
        />
        <span>
          <b>{AUTO_LABEL}</b>
          <span className="repair-settings__help" id="repair-auto-accept-help">
            {AUTO_HELP}
          </span>
        </span>
      </label>
      <p className="page__sub" role="status">
        {busy ? "Saving…" : scopeLine({ ...saved, auto_accept_repairs: on })}
      </p>
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
