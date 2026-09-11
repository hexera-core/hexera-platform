/**
 * Role grouping.
 *
 * The sheet has 32 distinct role strings, but 194 of the 239 contacts are some
 * spelling of founder or CEO and only about a dozen are the engineers who
 * would actually run the tool. Filtering on the raw string is useless at that
 * distribution; grouping is what makes "which positions answer" a question you
 * can read off a table.
 *
 * The groups are ordered by distance from the work: the people who feel the
 * meshing problem daily are separated from the people who sign for it, because
 * those two audiences want completely different emails.
 */

export const ROLE_GROUPS = [
  "Aero / Simulation",
  "Engineering lead",
  "CTO / Chief Engineer",
  "Founder / CEO",
  "Other",
] as const;

export type RoleGroup = (typeof ROLE_GROUPS)[number];

/**
 * Order matters. A "Co-Founder & Chief Scientist" is more usefully reached as a
 * technical contact than as a founder, so the specific technical patterns are
 * tested before the broad founder ones.
 */
const RULES: [RegExp, RoleGroup][] = [
  // CAE, FEA and "analysis" belong here. At a 200-person aerospace company the
  // Head of CAE usually owns the simulation tool budget outright, so they are
  // both the person who feels the problem and the one who can act on it.
  // Leaving them out was the biggest gap in the first list.
  [
    /aerodynam|simulation|cfd|turbomachin|thermal|fluid|\bcae\b|\bfea\b|engineering analysis|analysis manager|structural analysis|computational/i,
    "Aero / Simulation",
  ],
  [/chief scientist|chief engineer|\bcto\b|chief technology/i, "CTO / Chief Engineer"],
  [
    /head of engineering|\bvp\b.*engineering|vice president.*engineering|director of engineering|engineering lead|engineering manager|head of digital engineering|president of aircraft|head of r&d|director of r&d/i,
    "Engineering lead",
  ],
  // "president" last and anchored: a Vice President of Engineering matched the
  // bare word and came back as a founder, which is how a VP ends up reading an
  // email written for the person who owns the company.
  [/founder|\bceo\b|chairman|(?<!vice )\bpresident\b/i, "Founder / CEO"],
];

export function roleGroup(role: string | null | undefined): RoleGroup {
  if (!role) return "Other";
  for (const [pattern, group] of RULES) {
    if (pattern.test(role)) return group;
  }
  return "Other";
}
