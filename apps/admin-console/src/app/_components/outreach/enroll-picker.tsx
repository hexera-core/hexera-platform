"use client";

import { useState } from "react";
import { ActionForm, SubmitButton } from "./action-form";
import { Field, Chip } from "./ui";

export interface EnrollRow {
  id: number;
  company: string;
  industry: string;
  tier: string;
  person: string;
  role: string;
  roleGroup: string;
  email: string;
  suppressed: boolean;
  placeholderName: boolean;
  verification: string | null;
  verificationScore: number | null;
  verificationReasons: string | null;
  sent: number;
  lastReply: string | null;
  step: number | null;
  /** Why this one cannot be enrolled, or null if it can. */
  blocked: string | null;
  enrolled: boolean;
  enrollmentStatus: string | null;
  /** Tickable like anyone else, but marked: auto-fill never picks YC. */
  isYc: boolean;
}

/**
 * Tick who to enroll, and say how many you want in total.
 *
 * If you tick fewer than the target, the system fills the gap from the same
 * filtered list. Picked contacts always go in first, so topping up can never
 * push out a deliberate choice; it only fills what is left. Set the target to
 * the number you ticked and the top-up does nothing.
 *
 * Contacts that cannot be enrolled are shown with the reason rather than
 * hidden. A row that silently disappears reads as a bug; a row that says
 * "no email on file" reads as information.
 */
export function EnrollPicker({
  rows,
  campaigns,
  action,
  roleGroup,
  industry,
}: {
  rows: EnrollRow[];
  campaigns: { id: number; name: string }[];
  action: (formData: FormData) => Promise<{ ok: boolean; message: string }>;
  roleGroup?: string;
  industry?: string;
}) {
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [target, setTarget] = useState(25);

  const selectable = rows.filter((r) => !r.blocked && !r.enrolled);
  const allPicked = selectable.length > 0 && picked.size === selectable.length;
  const topUp = Math.max(target - picked.size, 0);

  function toggle(id: number) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <ActionForm action={action}>
      <input type="hidden" name="role_group" value={roleGroup ?? ""} />
      <input type="hidden" name="industry" value={industry ?? ""} />

      {/* One bottom-aligned row: every cell is label + control and nothing
          else, so the baselines cannot drift apart. Widths are content-sized
          rather than stretched across the panel. */}
      <div className="flex flex-wrap items-end gap-3 mb-2">
        <div className="grow min-w-[14rem] max-w-[24rem]">
          <Field label="Campaign">
            <select name="campaign_id" className="select" required>
              {campaigns.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          </Field>
        </div>

        <div className="w-32">
          <Field label="How many">
            <input
              name="target"
              type="number"
              min={0}
              max={250}
              value={target}
              onChange={(e) => setTarget(Number(e.target.value))}
              className="input"
            />
          </Field>
        </div>

        <SubmitButton variant="primary" disabled={picked.size === 0 && target === 0}>
          Enroll {Math.max(target, picked.size)}
        </SubmitButton>
        <button
          type="button"
          className="btn"
          onClick={() =>
            setPicked(allPicked ? new Set() : new Set(selectable.map((r) => r.id)))
          }
          disabled={!selectable.length}
        >
          {allPicked ? "Clear" : `Tick all ${selectable.length}`}
        </button>
      </div>

      <p className="text-[0.75rem] text-[var(--color-muted)] m-0 mb-3">
        {picked.size} ticked
        {topUp > 0
          ? `, ${topUp} more chosen for you from this filter`
          : picked.size > target
            ? `, all ${picked.size} will be enrolled`
            : ", nothing chosen for you"}
        .
      </p>

      <div className="scroll-x">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: "2.5rem" }}></th>
              <th>Company</th>
              <th>Person</th>
              <th>Email</th>
              <th>Role</th>
              <th>Verification</th>
              <th>Sequence</th>
              <th>Sent</th>
              <th>Last reply</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const on = picked.has(row.id);
              const usable = !row.blocked && !row.enrolled;
              return (
                <tr
                  key={row.id}
                  onClick={() => usable && toggle(row.id)}
                  className={usable ? "cursor-pointer" : undefined}
                  style={{
                    background: on ? "rgba(255,79,0,0.06)" : undefined,
                    opacity: usable ? 1 : 0.5,
                  }}
                >
                  <td onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      name="contact_id"
                      value={row.id}
                      checked={on}
                      disabled={!usable}
                      onChange={() => toggle(row.id)}
                      aria-label={`Enroll ${row.person} at ${row.company}`}
                    />
                  </td>
                  <td className="max-w-[13rem]">
                    <div className="truncate" title={row.company}>
                      {row.company}
                      {row.isYc && <span className="chip chip-accent ml-1.5 align-middle">YC</span>}
                    </div>
                    <div className="text-[0.625rem] text-[var(--color-faint)] truncate">
                      {row.industry}{row.tier ? ` · ${row.tier}` : ""}
                    </div>
                  </td>
                  <td className="max-w-[11rem]">
                    <div className="truncate">{row.person}</div>
                    <div className="text-[0.625rem] text-[var(--color-faint)] truncate" title={row.role}>
                      {row.role}
                    </div>
                    {row.placeholderName && (
                      <span className="chip chip-caution mt-1" title="The sheet has a job title here, not a person's name.">
                        no name
                      </span>
                    )}
                  </td>
                  <td className="max-w-[15rem]">
                    <span className="t-mono text-[0.75rem] truncate block" title={row.email}>
                      {row.email}
                    </span>
                    {row.suppressed && <span className="chip chip-danger mt-1">suppressed</span>}
                  </td>
                  <td className="text-[0.75rem] text-[var(--color-muted)] whitespace-nowrap">
                    {row.roleGroup}
                  </td>
                  <td className="whitespace-nowrap">
                    <Chip value={row.verification ?? undefined} title={row.verificationReasons ?? undefined} />
                    {row.verificationScore !== null && (
                      <span className="t-num text-[0.625rem] text-[var(--color-faint)] ml-1.5">
                        {row.verificationScore}
                      </span>
                    )}
                  </td>
                  <td className="whitespace-nowrap">
                    {row.enrolled ? (
                      <>
                        <Chip value={row.enrollmentStatus ?? undefined} />
                        {row.step ? (
                          <span className="t-num text-[0.625rem] text-[var(--color-faint)] ml-1.5">
                            step {row.step}
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span
                        className="text-[0.75rem]"
                        style={{ color: row.blocked ? "var(--color-faint)" : "var(--color-positive)" }}
                      >
                        {row.blocked ?? "ready"}
                      </span>
                    )}
                  </td>
                  <td className="t-num">{row.sent}</td>
                  <td><Chip value={row.lastReply ?? undefined} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </ActionForm>
  );
}
