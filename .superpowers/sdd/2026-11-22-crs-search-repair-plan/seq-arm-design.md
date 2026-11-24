# search_seq — sequential defect repair (owner direction 2026-11-24)

Owner's objection: the search arms evaluate hundreds of candidates per step ("coordinated bashing"). `search_seq`
repairs defects ONE AT A TIME, verifies each LOCALLY, and runs the full 4-mesher check only at the end.
Linear in the number of defects. A new arm; `search_det` and `search_ai` stay for comparison.

## Where it starts

Same start as the other search arms: the staged healer's output, confirmed by its full verdict (already paid,
shared by the single-flight input cache). Same per-file wall, same op budget cap, same delivery gate.

## The loop (at most 3 rounds)

1. DIAGNOSE (once per round). `find_sites(...)` on the current state's FULL verdict gives the defect list: each
   site has a kind, its entities (faces / bodies by stable id) and measurements (clash depth, gap, sliver width,
   the mesher's located faces). Round 1 reuses the confirm_staged verdict, so diagnosis costs no extra panel.
2. ORDER. Validity first, in this order: inside-out (non_positive_volume), body_overlap, self_intersection,
   open_shell / free edges, other BRepCheck codes. Then mesher failures by kind: overlapping_facets,
   edge_through_face / plc_intersection, hxt_3d_failed, no_volume, low_quality, timeout. Ties: by site id
   (deterministic).
3. PICK ONE REPAIR per defect, from the RULE TABLE: defect kind -> ordered list of repairs, seeded from Lane D's
   oracle winners (`lane-d21/pass1/winners_by_defect.jsonl`, `final/table.jsonl`), ranked by DISTINCT FILES.
   A "repair" may be a fixed 2-op recipe (e.g. aimed defeature -> run_stage bodies) because the oracle's wins are
   mostly 2-step chains that do nothing after step 1 alone.
   - Exactly one top entry -> use it.
   - None, or a tie at the top -> ONE AI call, constrained to the table's candidates for that kind (or the op
     registry's ops that apply to the kind when the table is empty), returns one choice + rationale.
   - PARAMETERS come from the defect's measurement, not a ladder: remove_small width = measured sliver width x
     1.1; fuse/cut fuzzy = measured gap/clash; defeature faces = the site's located faces; etc. One value.
4. APPLY on the site's entities only. The existing guards apply unchanged: catastrophic loss (B22), degenerate
   Boolean (C15), inside-out on the BREP (C15).
5. VERIFY LOCALLY (Lane C's `verify_local`):
   - validity on the TOUCHED solids only (the op's modified/generated bodies, plus any body whose box meets them,
     for overlap pairs): is THIS defect's code gone on those entities, and no NEW hard code on them;
   - for a mesher defect: mesh the touched solid(s) alone with the member that failed (doomed cap applies).
   Pass -> keep, next defect. Fail -> next alternative from the table (at most 2 more), else REVERT and record
   `skipped: <defect> <reason>`.
6. After all validity defects: OPTIONAL one full check (on if any validity defect was fixed; it tells round 2 the
   true mesher state). At the end of the round: ONE full 4-mesher check. Clean -> deliver. New or remaining
   failures -> next round with a fresh diagnosis. Stop at 3 rounds, or a round that keeps nothing.
7. DELIVER through the same delivery gate (never worse than the input; catastrophic-loss refusal reported).

## Entity tracking (the hard part; Lane C)

Ops rename faces. Each defect in the ordered list is RE-LOCATED before its repair: through the op's history
(modified/generated) where the kernel gives it, else by geometric location (the defect's points / box). A defect
that can no longer be located is re-checked locally: gone -> recorded `resolved_by_earlier_repair`; else skipped.

## Known risks, stated up front

- A local pass is not a whole-part pass (body interactions, shared facets). The end-of-round full check catches it;
  that is what the rounds are for.
- Greedy order cannot find a fix whose first step alone looks worse; the 2-op recipes in the table cover the
  oracle-measured cases.
- Fix-one-break-another: bounded by 3 rounds and "a round that keeps nothing stops".

## What is measured (fast loop, fresh cache, same walls)

Per file and arm: clean, wall s, FULL checks per file, local checks per file, defects found / fixed / skipped by
kind, AI calls and $. search_seq vs search_det vs search_ai on the same files.

## Ownership

- Lane B: the arm, the loop, ordering, the rule table + parameter derivation, the AI pick, row fields.
- Lane C: `verify_local(ws_before, ws_after, defect, touched) -> LocalResult(gone, new_hard, detail, seconds)`,
  the touched-entity set, re-location across ops, the region mesh.
- Interface first: Lane C publishes the `verify_local` signature + a stub within the hour so Lane B builds against
  it. Tiny targeted tests only.
