"use client";

import { useSyncExternalStore } from "react";

import { formatWhen, fullWhen } from "./when";

const subscribe = () => () => {};

/** A list's date cell. The server cannot know the reader's time zone, so the first paint is in
 *  UTC and the browser redraws it in local time as soon as the page hydrates; the full stamp is
 *  the tooltip. */
export function When({ iso }: { iso: string | null }) {
  const local = useSyncExternalStore(subscribe, () => true, () => false);
  if (!iso) {
    return <span className="cell-faint">—</span>;
  }
  const zone = local ? undefined : "UTC";
  return (
    <time dateTime={iso} title={fullWhen(iso, zone)}>
      {formatWhen(iso, new Date(), zone)}
    </time>
  );
}
