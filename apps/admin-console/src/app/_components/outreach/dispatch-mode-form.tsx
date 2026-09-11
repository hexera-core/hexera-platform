"use client";

import { useState } from "react";
import { ActionForm, SubmitButton } from "./action-form";
import { Field } from "./ui";

/**
 * The mode picker.
 *
 * The start time only exists in automatic mode, so it is only on screen in
 * automatic mode. Leaving a greyed-out "09:00" sitting there while manual is
 * selected invites the reading that something is still going to happen at 9.
 */
export function DispatchModeForm({
  mode,
  time,
  action,
}: {
  mode: "manual" | "automatic";
  time: string;
  action: (formData: FormData) => Promise<{ ok: boolean; message: string }>;
}) {
  const [selected, setSelected] = useState(mode);

  return (
    <ActionForm action={action} className="flex flex-col gap-3">
      <Field label="How sending starts">
        <select
          name="mode"
          value={selected}
          onChange={(e) => setSelected(e.target.value as "manual" | "automatic")}
          className="select"
        >
          <option value="manual">Manual, I pick and press send</option>
          <option value="automatic">Automatic, on a schedule</option>
        </select>
      </Field>

      {selected === "automatic" ? (
        <Field
          label="Start time"
          hint="24-hour clock, in the campaign's timezone. Each day's sending begins here."
        >
          <input name="time" defaultValue={time} placeholder="09:00" className="input" />
        </Field>
      ) : (
        <p className="text-[0.6875rem] text-[var(--color-faint)] leading-snug m-0">
          No timers. Everything enrolled is ready the moment it is enrolled, and stays
          ready until you send it.
        </p>
      )}

      <div><SubmitButton>Save</SubmitButton></div>
    </ActionForm>
  );
}
