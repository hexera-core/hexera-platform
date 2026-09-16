"use client";

import { useState } from "react";
import { ActionForm, SubmitButton } from "./action-form";

export interface Sendable {
  enrollmentId: number;
  company: string;
  person: string;
  email: string;
  step: number;
  role: string;
}

/**
 * Tick who you want, press send.
 *
 * Nothing is ticked when the page loads, and there is no "select all that then
 * sends" shortcut: the button always names the exact number it is about to
 * mail, because "Send" with an invisible count is how you email 200 people by
 * accident.
 */
export function SendPicker({
  items,
  action,
  live,
}: {
  items: Sendable[];
  action: (formData: FormData) => Promise<{ ok: boolean; message: string }>;
  live: boolean;
}) {
  const [picked, setPicked] = useState<Set<number>>(new Set());

  function toggle(id: number) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const allPicked = items.length > 0 && picked.size === items.length;

  return (
    <ActionForm action={action}>
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <SubmitButton name="intent" value="send" variant="primary" disabled={picked.size === 0}>
          {picked.size === 0
            ? "Tick someone to send"
            : live
              ? `Send to ${picked.size}`
              : `Rehearse ${picked.size}`}
        </SubmitButton>

        {/* Same form, different button. The pressed button puts its own
            name and value into the FormData, so the action knows which of
            the two you meant without a hidden field to get out of sync. */}
        <SubmitButton name="intent" value="unenroll" variant="danger" disabled={picked.size === 0}>
          {picked.size === 0 ? "Unenroll" : `Unenroll ${picked.size}`}
        </SubmitButton>

        <button
          type="button"
          className="btn"
          onClick={() => setPicked(allPicked ? new Set() : new Set(items.map((i) => i.enrollmentId)))}
          disabled={items.length === 0}
        >
          {allPicked ? "Clear" : `Tick all ${items.length}`}
        </button>

        <span className="text-[0.75rem] text-[var(--color-muted)]">
          {picked.size} of {items.length} selected
        </span>
      </div>

      {picked.size > 0 && !live && (
        <p className="text-[0.75rem] text-[var(--color-muted)] m-0 mb-3">
          Sending is switched off, so this will render the messages and deliver nothing.
        </p>
      )}

      <div className="scroll-x">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: "2.5rem" }}></th>
              <th>Company</th>
              <th>Person</th>
              <th>Role</th>
              <th>Email</th>
              <th>Step</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const on = picked.has(item.enrollmentId);
              return (
                <tr
                  key={item.enrollmentId}
                  onClick={() => toggle(item.enrollmentId)}
                  className="cursor-pointer"
                  style={on ? { background: "rgba(255,79,0,0.06)" } : undefined}
                >
                  <td onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      name="enrollment_id"
                      value={item.enrollmentId}
                      checked={on}
                      onChange={() => toggle(item.enrollmentId)}
                      aria-label={`Send to ${item.person} at ${item.company}`}
                    />
                  </td>
                  <td className="max-w-[14rem] truncate">{item.company}</td>
                  <td className="max-w-[11rem] truncate">{item.person}</td>
                  <td className="max-w-[12rem] truncate text-[0.75rem] text-[var(--color-muted)]">
                    {item.role}
                  </td>
                  <td className="t-mono text-[0.75rem] max-w-[16rem] truncate">{item.email}</td>
                  <td className="t-num">{item.step}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </ActionForm>
  );
}
