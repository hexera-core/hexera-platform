import Link from "next/link";
import React, { type ComponentProps } from "react";

import { isWorkbenchHref } from "./workbench-href";

/** A console link. Into the workbench it is a plain <a> - a full page load, the only way
 *  main.js starts (see isWorkbenchHref) - and everywhere else a normal client-side Link. */
export function NavLink({ href, ...rest }: ComponentProps<"a"> & { href: string }) {
  if (isWorkbenchHref(href)) {
    return <a href={href} {...rest} />;
  }
  return <Link href={href} {...rest} />;
}
