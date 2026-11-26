# Responsibility: CRS v5's ONE repair path: diagnose every defect into a class (`diagnose`), run ONE tournament per
#                 defect over its class menu (`tournament`), one full panel per round, and - if the part is still
#                 not clean and time remains - v4's AI search from search_ai's start, then from the class state.
# Owns: `CLASS_REPAIRS` (class -> its menu, rule repair first), `table_key`, `v5_table`, `repair_v5`, `V5Outcome`,
#       `remeasuring` (the local check for faults layer 1 cannot see), the wall split with the fallback, and the
#       fallback's starts (`fallback_starts`).
# Boundaries: the loop is `repair_v4.seq.repair_sequentially` (handed `diagnose` and the tournament as its
#             `repair_defect`), the classes are `diagnose`'s, the fallback is whatever search the caller hands in
#             (the eval hands `search.search` with the search_ai generators). The delivery gate and the
#             catastrophic-loss guard on the delivered part stay with the caller, exactly as for every arm.
"""Recognise, repair with a dedicated operation, verify cheap-first, keep or revert - then v4's search if needed.

The class sets a MENU (`CLASS_REPAIRS`, rule repair first) and each defect gets one tournament over it
(`tournament.Tournament`: rule seed + the menu's other repairs + parameter variants, at most six, every candidate
logged). An `unknown` defect's menu is v4's measured per-code row, so v5 never has fewer repairs than v4 had.

The fallback is what makes v5 never worse than v4's best: it is search_ai from search_ai's OWN start with search_ai's
whole wall, then from the class loop's best state with what that leaves (`fallback_starts`: the class state goes
first only where the loop made an invalid part valid), and the best of the class state and every search is kept, so
whatever the loop kept is the floor. The class loop's wall is EXTRA (CRS v5 r2): it may use `SEQ_SHARE` x the
search wall as a hard cap, and the fallback then gets the search_ai arm's whole own search wall (bounded only by the
file wall).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4 import seq as Q
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.verdict import wall
from meshpipeline.cad.repair_v4.workspace import Workspace
from meshpipeline.cad.repair_v5 import brep_faults as F
from meshpipeline.cad.repair_v5 import conformal as C
from meshpipeline.cad.repair_v5 import diagnose as D
from meshpipeline.cad.repair_v5 import events as EV
from meshpipeline.cad.repair_v5 import facet_local as FL
from meshpipeline.cad.repair_v5 import narrow as N
from meshpipeline.cad.repair_v5 import open_shell as S
from meshpipeline.cad.repair_v5 import quality as QL
from meshpipeline.cad.repair_v5 import tournament as T

Repair = Q.Repair

#: THE CLASS -> MENU MAP, keyed by `diagnose.DefectClass`. The FIRST repair is the class's RULE SEED (it always
#: enters its tournament and wins ties); the rest are the menu's other entrants. Seeded from what is MEASURED today;
#: each class's dedicated ops join its row as they land. Parameters come from the defect's measurement and cause
#: (`seq.params_for`); the tournament adds variants one step either side of them.
CLASS_REPAIRS: dict[str, tuple[Repair, ...]] = {
    # v5 step 8: what the conformal pass leaves, PAIR BY PAIR (the tournament plays a cluster per pair): the Boolean
    # chosen by volume (a duplicate dropped, a mostly-inside body fused, else the smaller cut by the larger) as the
    # rule seed; then fragment-and-drop-small; then the oracle's two body rows (conformalise and fuse tied at 5 files)
    "overlap_self_intersection": (("boolean_by_volume",), ("fragment_drop_small",), ("conformalise",), ("fuse",)),
    # sew first, then fill the free loop
    "open_shell_volume": (("sew",), ("fill_loop",)),
    "invalid_brep_tolerance": (("tighten_tolerances",), ("rebuild_edge_as_intersection",)),
    # v5 step 7: any close non-adjacent pair the facet sag can bridge. The rule seed refines BOTH faces' facets
    # until each sags under a quarter of the measured gap (split_by_proximity, aimed at the diagnosed pairs); the
    # menu is the owner's: split_angle, the shared-angle split, tweak (two bodies), defeature
    "close_faces": (("split_by_proximity",), ("split_angle",), ("split_aligned",), ("tweak",), ("defeature",)),
    # round 2: facets crossing within a face / across a shared edge / at touching faces, no measured cause: split the
    # face, refit it, merge across the edge, then defeature (the rows by measured cause are below)
    FL.CLASS: FL.GENERAL_ROW,
    # v5 step 9: imprint the intersection on both faces (the owner's rule seed); then v4's measured winners for
    # edge_through_face (defeature, conformalise) and a merge of the pair
    "surface_crossing": (("imprint_crossing",), ("defeature",), ("conformalise",), ("unify_same_domain",)),
    # round 2 (step-13: split_closed still_there and split_angle invalidated on 00610541's F316): the seam rebuilt
    # from its surface (declines unless a seam is off it) and ShapeFix_Face join the split
    "closed_periodic_face": (("split_closed",), ("rebuild_seam",), ("split_angle",), ("fix_poles",)),
    # split_by_proximity made every low_quality repair in two waves; reapproximate never applied
    "low_quality": (("split_by_proximity",), ("remove_small",)),
}
#: CODE-SPECIFIC ROWS inside a class, keyed `<class>:<code>` (or `<class>:closed`), used where the code (or the
#: cause) says the class's general row is the wrong tool. Existing ops only.
CLASS_REPAIRS.update({
    # no free loop left: the shell is closed, so the volume is made, not a loop filled
    "open_shell_volume:closed": (("sew",), ("make_volume",)),
    "open_shell_volume:non_positive_volume": (("orient_and_close",), ("sew",)),
    # v5 step 5: invalid_brep_tolerance by the detectors' fault KIND (`cause["kind"]`, brep_faults.KIND_REPAIRS):
    # tighten_tolerances / trim_unbounded / fix_poles / to_canonical as the rule seed (no `rescale`: units are
    # report-only, owner decision A, 2026-11-25)
    **{f"invalid_brep_tolerance:{kind}": row for kind, row in F.KIND_REPAIRS.items()},
    # round 2: a periodic failure on a face whose seam is off its surface: the seam rebuilt first
    "closed_periodic_face:bad_seam": (("rebuild_seam",), ("split_closed",), ("split_angle",)),
    # v5 step 7: a near-coaxial pair (shaft in bore): the SHARED-ANGLE split of both faces is the rule seed (C23:
    # it took f9501251fb30's shaft-in-bore from overlapping_facets to a mesh), segments from the gap and the sag
    "close_faces:coaxial": (("split_aligned",), ("split_by_proximity",), ("split_angle",), ("tweak",),
                            ("defeature",)),
    # v5 step 10: low_quality by the detectors' measured KIND (`quality.faults_of`): the merge is a real topology
    # edit with the deviation measured; a sliver above the feature angle is protected (merge_sliver declines)
    "low_quality:sliver": (("merge_sliver",), ("refit_cluster",), ("remove_small",), ("split_by_proximity",)),
    "low_quality:small_face": (("merge_sliver",), ("remove_small",), ("split_by_proximity",)),
    "low_quality:small_edge": (("collapse_edge",), ("remove_small",), ("split_by_proximity",)),
    "low_quality:tiny_solid": (("fuse",), ("drop_solid",)),
    "low_quality:floating_solid": (("drop_solid",),),
    # round 2 fix E (`narrow.py`): a NARROW REGION at the mesher's worst elements (two non-adjacent edges or faces
    # closer than e): merge the strip into its neighbour (deviation <= e), defeature it, collapse its short edges
    "low_quality:narrow_region": (("merge_sliver",), ("defeature",), ("collapse_edge",), ("remove_small",)),
    # v5 step 9: surface_crossing by the detectors' crossing KIND (`crossing.crossing_kind`)
    "surface_crossing:duplicate_faces": (("remove_duplicate_faces",), ("conformalise",), ("imprint_crossing",),
                                         ("defeature",)),
    "surface_crossing:true_crossing": (("imprint_crossing",), ("conformalise",), ("defeature",)),
    "surface_crossing:near_coincident": (("unify_same_domain",), ("conformalise",), ("imprint_crossing",),
                                         ("defeature",)),
    # v5 step 8 (overlap.py's detectors): a cluster holding a DUPLICATE body drops the copy first
    "overlap_self_intersection:duplicate_body": (("drop_duplicate_body",), ("boolean_by_volume",),
                                                 ("fragment_drop_small",)),
    # a self-intersecting SOLID (layer 1 names the body; the detector aims at its interference faces): rebuild it
    # from its own faces and drop the slivers, then the face ops at the located faces
    "overlap_self_intersection:self_intersection": (("fragment_drop_small",), ("rebuild_face",), ("defeature",)),
    # ... and when one of those faces' own boundary crosses itself, the face is rebuilt from that boundary first
    "overlap_self_intersection:self_intersecting_face": (("rebuild_face",), ("fragment_drop_small",),
                                                         ("defeature",)),
    # v5 step 11: open_shell_volume by the shell detectors' cause KIND (`open_shell.KIND_REPAIRS`): sew_gap at the
    # measured gap, fill_loop on the smallest unpartnered loop, make_volume, orient_and_close, split_non_manifold
    **{f"open_shell_volume:{kind}": row for kind, row in S.KIND_REPAIRS.items()},
    # round 2: facet_crossing_local by the measured CAUSE (`facet_local.KIND_REPAIRS`): split_angle on a curved face,
    # split_closed on a closed one, split_continuity at a C1 break, reapproximate a bad parameterisation, to_canonical
    # a spline that is really analytic, unify_same_domain across a near-tangent edge, imprint touching faces
    **{f"{FL.CLASS}:{kind}": row for kind, row in FL.KIND_REPAIRS.items()},
})

#: the prefix an `unknown` defect's table key carries: `unknown:<code>`. A class name and a code can be the same
#: string (`low_quality` is both), so the legacy per-code rows live under their own keys.
UNKNOWN_PREFIX = "unknown:"


def table_key(defect: Defect) -> str:
    """The key the loop's table is looked up by: `<class>:<code>` where such a row exists, `open_shell_volume:closed`
    when the cause counts no free loop, else the class; for `unknown` (or undiagnosed) `unknown:<code>`."""
    cls = defect.cls or "unknown"
    if cls == "unknown":
        return f"{UNKNOWN_PREFIX}{defect.code}"
    kind = (defect.cause or {}).get("kind")
    if kind and f"{cls}:{kind}" in CLASS_REPAIRS:
        return f"{cls}:{kind}"
    if f"{cls}:{defect.code}" in CLASS_REPAIRS:
        return f"{cls}:{defect.code}"
    if cls == "open_shell_volume" and defect.cause.get("free_loops") == 0:
        return "open_shell_volume:closed"
    if cls == "close_faces" and (defect.cause or {}).get("coaxial"):
        return "close_faces:coaxial"
    return cls


def v5_table(*, legacy: dict[str, tuple[Repair, ...]]) -> dict[str, tuple[Repair, ...]]:
    """ONE lookup table for the tournaments: the class menus, plus v4's per-code rows for `unknown` defects. Not
    capped here: the tournament caps its entrants and logs every one it leaves out."""
    return {**{f"{UNKNOWN_PREFIX}{k}": tuple(v) for k, v in legacy.items()}, **CLASS_REPAIRS}


#: the fault kinds a validity defect is judged by RE-MEASURING (`brep_faults`), because layer 1's local check cannot
#: see them: tolerances and splines are not hard codes, an unbounded part stops the checker
REMEASURED_KINDS = frozenset({"large_tolerance", "pole", "bad_spline", "unbounded", "missing_edge", "bad_seam"})
#: of those, the kinds layer 1's local check DOES see (as brep_invalid): gone only when both say so
LAYER1_SEEN_KINDS = frozenset({"missing_edge"})


def remeasuring(base: Callable[..., Any], source: str | None = None) -> Callable[..., Any]:
    """`base` (the local check), and for a VALIDITY defect of a `REMEASURED_KINDS` fault the detectors' own answer on
    the repaired state: gone = no fault of that kind left on the defect's (carried) entities, or on the part for a
    whole-part kind. Without this a repair of a fault layer 1's local check cannot see reads as "gone" whatever it
    did. A re-measurement that fails keeps `base`'s answer and says so in the detail."""
    def verify(pool: Any, ws_before: Workspace, ws_after: Workspace, defect: Defect, touched: Any = None,
               **kw: Any) -> Any:
        local = base(pool, ws_before, ws_after, defect, touched, **kw)
        kind = (defect.cause or {}).get("kind")
        import dataclasses

        from meshpipeline.cad.repair_v4.local_verify import _forward, _new_diffs

        if defect.kind == "validity" and defect.code in ROUND_TRIP_CODES and not any(
                str(b).split(" ", 1)[0] == defect.code for b in (local.detail or {}).get("before") or ()):
            return _through_round_trip(pool, ws_before, ws_after, defect, local)
        if defect.kind == "validity" and defect.cls == QL.LOW_QUALITY and kind in QL.KINDS:
            return _quality_remeasured(pool, ws_before, ws_after, defect, local)
        if defect.kind == "validity" and defect.cls == S.CLASS:
            # step 11: layer 1's local check sees solids only; a free shell, a missing solid or a gap between loose
            # faces is judged by the shell detectors on both states
            return S.remeasured(pool, ws_before, ws_after, defect, local)
        if defect.kind != "validity" or kind not in REMEASURED_KINDS:
            return local

        faults, facts = D.measure_faults(pool, ws_after, header_unit=D.header_unit_of(source))
        if facts is None:
            return dataclasses.replace(local, detail={**local.detail, "remeasured": "unavailable"})
        carried = _forward([*defect.entities, *((defect.cause or {}).get("aim") or [])],
                           _new_diffs(ws_before, ws_after))
        left = [f for f in faults if f.kind == kind and (kind in F.WHOLE_PART_KINDS or not f.entities
                                                         or set(f.entities) & carried)]
        # a missing edge IS seen by layer 1 (brep_invalid on its faces): the detector's answer must agree with it
        gone = not left and (local.gone or kind not in LAYER1_SEEN_KINDS)
        return dataclasses.replace(local, gone=gone, detail={
            **local.detail, "remeasured": [f.why for f in left] or "fault gone"})
    return verify


def predicting(base: Callable[..., Any]) -> Callable[..., Any]:
    """`base` (the local check) with the close_faces class's FAST LOCAL CHECK in front (v5 step 7): the predictor on
    what the defect's close pairs became. Facets still crossing at the failing member -> still there, without a
    mesh; none crossing -> `base` decides (the local mesh), and the prediction rides in the detail either way. A
    prediction that could not be had leaves `base` alone."""
    def verify(pool: Any, ws_before: Workspace, ws_after: Workspace, defect: Defect, touched: Any = None,
               **kw: Any) -> Any:
        local = defect.cls == FL.CLASS and bool((defect.cause or {}).get("watch") or (defect.cause or {}).get("aim"))
        if not local and (defect.cls != "close_faces" or not (defect.cause or {}).get("face_pairs")):
            return base(pool, ws_before, ws_after, defect, touched, **kw)
        import dataclasses

        from meshpipeline.cad.repair_v4.local_verify import LocalResult

        t0 = time.monotonic()
        # round 2: facet_crossing_local's fast check is its own predictor on what the crossing faces became
        after = (FL.predicted_after(pool, ws_before, ws_after, defect) if local
                 else D.predicted_after(pool, ws_before, ws_after, defect))
        before = (defect.cause or {}).get("predicted_before")
        judged_by = defect.member if after is not None and defect.member in after else None
        crossing = (after[judged_by] if judged_by else sum(after.values())) if after is not None else None
        detail = {"predicted_before": before, "predicted_after": after}
        if crossing:
            return LocalResult(False, [], {**detail, "fast_check": f"facets still cross ({crossing} at "
                                                                   f"{judged_by or 'all members'})"},
                               round(time.monotonic() - t0, 3))
        local = base(pool, ws_before, ws_after, defect, touched, **kw)
        return dataclasses.replace(local, detail={**local.detail, **detail})
    return verify


def moving(base: Callable[..., Any]) -> Callable[..., Any]:
    """`base` (the local check), and for a low_quality MESHER defect that the member still fails on: gone when the
    worst elements left the defect's region with a better min SICN (`narrow.moved`, round 2 fix E) - the defect at
    this place is fixed, the failure left is another defect. Said as `moved` in the detail, never as a pass."""
    def verify(pool: Any, ws_before: Workspace, ws_after: Workspace, defect: Defect, touched: Any = None,
               **kw: Any) -> Any:
        local = base(pool, ws_before, ws_after, defect, touched, **kw)
        if local.gone or local.new_hard or defect.cls != QL.LOW_QUALITY:
            return local
        why = N.moved(defect, local.detail or {})
        if why is None:
            return local
        import dataclasses

        return dataclasses.replace(local, gone=True, detail={**local.detail, "moved": why, "remeasured": why})
    return verify


#: layer-1 codes the BREP-level local check can be BLIND to: layer 1 judges the STEP re-export read back, and an
#: invalid face can exist only after that round trip (measured on jig, v5 step 5: on 532be4434c61 and 78cbab541afb
#: tighten_tolerances read "gone" locally on a brep_invalid the BREP never had, and the full check still failed).
ROUND_TRIP_CODES = frozenset({"brep_invalid"})


def _through_round_trip(pool: Any, ws_before: Workspace, ws_after: Workspace, defect: Defect, local: Any) -> Any:
    """`local` re-judged by layer 1 itself (the STEP round trip) on the repaired state: gone = no finding of the
    defect's code left on its carried entities (any, when it carries none). Used only when the local check did not
    see the code before the repair either, i.e. could not see this defect at all."""
    import dataclasses

    from meshpipeline.cad.repair_v4.local_verify import _forward, _new_diffs
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.verdict.valid import check_valid

    report = check_valid(pool, step_for(pool, ws_after.brep_path), original_units=None)
    carried = _forward(defect.entities, _new_diffs(ws_before, ws_after))
    left = [f for f in report.hard if f.code == defect.code
            and (not carried or not f.entities or set(Q.layer1_ids(ws_after, list(f.entities))[0]) & carried)]
    return dataclasses.replace(local, gone=not left, detail={
        **local.detail, "remeasured": "layer 1 through the STEP round trip (the local check could not see it): "
                                      + (f"{len(left)} {defect.code} left" if left else "gone")})


def _quality_remeasured(pool: Any, ws_before: Workspace, ws_after: Workspace, defect: Defect, local: Any) -> Any:
    """`local` re-judged by the low_quality detector (v5 step 10) for a defect made from a layer-1 SOFT finding,
    which layer 1's local check cannot see (soft codes are not in it): gone = the aimed entity carried to nothing,
    or nothing it carried to (scoped to the defect's carried faces) is still under `e` as the same kind."""
    import dataclasses

    from meshpipeline.cad.repair_v4.local_verify import _forward, _new_diffs

    diffs = _new_diffs(ws_before, ws_after)
    aim = list((defect.cause or {}).get("aim") or defect.entities)
    carried = _forward(aim, diffs) & set(ws_after.ids)
    if not carried:
        return dataclasses.replace(local, gone=True, detail={**local.detail, "remeasured": "the aimed entity is gone"})
    h = QL.h_of(defect)
    near = [*defect.entities, *((defect.cause or {}).get("faces") or [])]
    faces = sorted({ws_after.indices[s] for s in (_forward(near, diffs) | carried) & set(ws_after.ids)
                    if s[:1] == "F" and s in ws_after.indices})
    facts = QL.measure(pool, ws_after, h_mm=h, faces=faces or None) if h else None
    if facts is None:
        return dataclasses.replace(local, detail={**local.detail, "remeasured": "unavailable"})
    from meshpipeline.cad.repair_v4.sites import _reverse

    left = QL.still_there(QL.faults_of(facts, _reverse(ws_after), h_mm=h or 0.0), defect, carried)
    return dataclasses.replace(local, gone=not left, detail={
        **local.detail, "remeasured": [f.why for f in left] or "no small entity left there"})


#: the class loop's wall as a share of the search wall, ON TOP of it (CRS v5 r2: the fallback keeps the whole search
#: wall, so v5 never gives search_ai less time than its own arm has)
SEQ_SHARE = 0.5
# --- CRS v5 r2: PHASE SHARES of the class loop's wall (`wall_s * seq_share`), per round, so every class gets a turn.
# Measured on the step-13 wave (4a263b160, 95 dev rows; 45 files left 222 defects never tried): of those 45 files'
# wall, the conformal pass took 13%, diagnosis (sites + detectors) 13%, tournaments 16%; on 14 files the pass alone
# and on 3 the diagnosis alone (9ceb64b12b9c: 1219 s) used the loop's whole share before any defect was tried.
#: the conformal pass's measurements and steps (its own full check, the keep rule, runs after)
CONFORMAL_SHARE = 0.25
#: locating and diagnosing the round's defects: past this share the remaining kernel measurements are not started
#: (each degrades its cause, as a failed measurement always did) so the defects still get their tournaments. r2h: a
#: defect whose measurement was cut keeps its code's class (`diagnose.by_code_when_cut`), never `unknown`
DIAGNOSIS_SHARE = 0.25
# --- CRS v5 r2h: every phase in its own bounded share (owner decision 2026-11-26). Step-14 wave (df39e5e29, 145 v5
# files, 160,823 s): fallback 50.0%, staged healer 14.6%, tournaments 11.8%, diagnosis 5.0%, conformal 4.9%, input
# verdict 4.6%, final gates 3.6%, heal confirm 2.6%, round panels 1.8%; the loss guard alone 17% (26,646 s).
#: the FULL checks inside the class loop (the conformal pass's keep rule, the round-end panels) are counted in their
#: own share of the loop's wall, ON TOP of it: up to this share, their seconds move the tournaments' deadline later,
#: so a full check never shrinks a defect's slice (fix A's conformal check pushed a round past the loop's wall)
PANEL_SHARE = 0.25
#: the staged healer's share of the (size-scaled) FILE wall; stages past it are skipped and recorded (`stages`)
HEAL_SHARE = 0.15
#: the delivery's reserve: this times the input verdict's own seconds (the same full panel on a similar part) is kept
#: on the file wall after the class loop (and the fallback) for the delivered part's full panel
DELIVER_RESERVE_FACTOR = 1.25


#: one phase's pool: calls clipped to the phase's end, none started after it (moved to `seq` in CRS v5 r2h, where
#: `apply_repair` bounds a whole repair with it too)
DeadlinePool = Q.DeadlinePool
def fit_loop_wall(loop_wall: float, file_left: float | None, reserve_s: float | None) -> tuple[float, str]:
    """(the class loop's wall, why it was shortened or ""). r2h: the delivery (its full panel) keeps `reserve_s` of
    the FILE wall: the loop plus its full checks' share (`PANEL_SHARE`) must end in time for it. On the r2h VM run
    af9fc48e7581 reached `deliver` with 110 s of its 900 s left and lost the file to a panel that could not start.
    Pure."""
    if not reserve_s or file_left is None:
        return loop_wall, ""
    room = max(0.0, (file_left - reserve_s) / (1.0 + PANEL_SHARE))
    if room >= loop_wall:
        return loop_wall, ""
    return room, (f"class loop: {room:.0f} s, not {loop_wall:.0f} s - the file wall keeps {reserve_s:.0f} s for the "
                  f"delivery's full panel")


#: a fallback with less than this left is not started (a search that cannot finish one expansion measures nothing)
MIN_FALLBACK_S = 60.0

#: (start state, its full verdict, wall seconds left) -> a `search.SearchResult`-shaped result
Fallback = Callable[[Workspace, Any, float], Any]
#: the AI's description of the `unknown` defects: the diagnosed defects -> the same, `ai` filled on the unknowns
#: (`describe_ai.describe_unknown` bound to the file's ledger and route). It never changes a class.
AIDescribe = Callable[[list[Defect]], list[Defect]]


@dataclass
class V5Outcome:
    seq: Q.SeqOutcome
    #: the fallback's result, or None when it did not run
    fallback: Any
    #: why the fallback did or did not run, for the row
    fallback_why: str
    best: Workspace
    verdict: Any
    #: every tournament candidate of the run: applied or why not (`tournament.Match`)
    matches: T.MatchLog = field(default_factory=T.MatchLog)
    #: what the detectors reported per diagnosis (the units verdict whatever it is, the faults found)
    notes: list[str] = field(default_factory=list)
    #: the fallback's best beat the class loop's state (`fallback` is then the result it came from)
    fallback_improved: bool = False

    @property
    def clean(self) -> bool:
        return bool(getattr(self.verdict, "clean", False))

    @property
    def stopped_on_wall(self) -> bool:
        """The class loop stopped on the FILE wall (a panel or local mesh it could not start); arms reads it."""
        return self.seq.stopped_on_wall

    def lines(self) -> list[str]:
        """The readable trace: defect -> cause -> tournament -> every candidate, every round, the match counts, then
        the fallback."""
        return [*self.notes, *self.seq.lines(), f"matches: {self.matches.counts()}", f"fallback: {self.fallback_why}"]


def repair_v5(
    pool: Any,
    start: Workspace,
    original: str,
    *,
    judge: Any,
    sites_of: Callable[[Any, Workspace, Any], list],
    h_mm: float,
    wall_s: float,
    fallback: Fallback | None = None,
    ai_describe: AIDescribe | None = None,
    diagnose: Q.Diagnose | None = None,
    start_verdict: Any = None,
    run_stage: Any = None,
    clock: Callable[[], float] = time.monotonic,
    trace: Any = None,
    seq_share: float = SEQ_SHARE,
    verify: Any = None,
    guard: T.Guard = T.loss_guard,
    sources: Any = T.DEFAULT_SOURCES,
    budget: Any = T.budget_for,
    conformal: bool = True,
    reserve_s: float | None = None,
    **seq_kwargs: Any,
) -> V5Outcome:
    """`diagnose` defaults to `diagnose.diagnose` at `h_mm`; tests hand in their own (no kernel), and fakes for
    `verify` (default `local_verify.verify_local`), `guard` (default `tournament.loss_guard`) and `budget`.
    `wall_s` is the file's wall as the caller sized it (already scaled for part size): it is not rescaled here.
    `conformal` runs the conformal assembly pass (`conformal.ConformalPass`, step 6) at the top of every round.
    `reserve_s` (r2h): the seconds the FILE wall keeps after the class loop for the delivery's full panel; the loop
    (with its full checks' share) is shortened to fit when the file wall is the tighter bound."""
    t0 = clock()
    loop_wall = wall_s * seq_share
    notes: list[str] = []
    loop_wall, why = fit_loop_wall(loop_wall, wall.remaining_s(), reserve_s)
    if why:
        notes.append(why)
    panel_s = PANEL_SHARE * loop_wall          # the full checks' own share, on top of the loop's wall (r2h)
    diag_window: dict[str, Any] = {"until": None, "spent": {}}
    pair_cache: D.PairCache = {}             # per-site close-pair facts, reused across rounds (CRS v5 r2)

    def timed_sites(pool: Any, ws: Workspace, verdict: Any) -> list:
        # the round's diagnosis window opens with locating its sites (CRS v5 r2: `DIAGNOSIS_SHARE`)
        diag_window["until"] = min(clock() + DIAGNOSIS_SHARE * loop_wall, t0 + loop_wall + panel_s)
        diag_window["spent"] = {}
        return sites_of(DeadlinePool(pool, diag_window["until"], clock, diag_window["spent"]), ws, verdict)

    table = v5_table(legacy=Q.RULE_TABLE)
    stage = run_stage if run_stage is not None else Q.stage_runner()
    matches = T.MatchLog()
    local_check = predicting(remeasuring(moving(verify if verify is not None else Q.verify_local), original))
    runner = T.Tournament(menu_of=lambda d: table.get(table_key(d), ()), verify=local_check, guard=guard,
                          run_stage=stage, log=matches, sources=tuple(sources), budget=budget, clock=clock,
                          deadline=t0 + loop_wall, trace=trace, panel_credit_s=panel_s,
                          **{k: seq_kwargs[k] for k in ("op_timeout_s", "local_timeout_s") if k in seq_kwargs})

    def diagnosed(pool: Any, ws: Workspace, verdict: Any, sites: list, defects: list[Defect]) -> list[Defect]:
        def report(**facts: Any) -> None:
            units = facts.get("units") or {}
            line = (f"units: {units.get('verdict')} ({units.get('why')}); faults: "
                    f"{', '.join(f['kind'] for f in facts.get('faults') or []) or 'none'}"
                    + ("" if facts.get("facts_measured", True) else " [facts unmeasured]"))
            if not notes or notes[-1] != line:
                notes.append(line)
            if trace is not None:
                trace.record("v5_brep_facts", **facts)

        until = diag_window["until"] if diag_window["until"] is not None else t0 + loop_wall
        timed = DeadlinePool(pool, until, clock, diag_window["spent"])
        out = (diagnose(pool, ws, verdict, sites, defects) if diagnose is not None
               else D.diagnose(timed, ws, verdict, sites, defects, h_mm=h_mm, source_step=original, report=report,
                            pair_cache=pair_cache))
        spent = {k: round(v, 1) for k, v in sorted(diag_window["spent"].items(), key=lambda kv: -kv[1])}
        skipped = {k: v for k, v in spent.items() if k.startswith("skipped:")}
        if skipped:
            notes.append(f"diagnosis: its share of the wall was spent; measurements not started: {skipped}")
            # r2h: a defect left `unknown` keeps the class its code names - time never makes a defect unknown
            out = D.by_code_when_cut(out, "the diagnosis share of the wall cut its measurement short")
        if trace is not None:
            trace.record("v5_phase", phase="diagnosis", kernel_s=spent)
        return ai_describe(out) if ai_describe is not None else out

    if start_verdict is None and fallback is not None:
        # the fallback may start from `start` (CRS v5 r2 fix I), so its full verdict is kept: judged here, once,
        # exactly as the loop would judge it, and handed to the loop
        start_verdict = judge.full(pool, start, original)
        judged_start = True
    else:
        judged_start = False
    pre_round = (C.ConformalPass(judge=judge, original=original, log=matches, guard=guard, clock=clock,
                                 deadline=t0 + loop_wall, trace=trace, budget_s=CONFORMAL_SHARE * loop_wall,
                                 **{k: seq_kwargs[k] for k in ("op_timeout_s",) if k in seq_kwargs})
                 if conformal else None)
    outcome = Q.repair_sequentially(
        pool, start, original, judge=judge, sites_of=timed_sites, run_stage=stage, verify=local_check,
        start_verdict=start_verdict, wall_s=loop_wall, clock=clock, trace=trace,
        diagnose=diagnosed, repair_defect=runner, pre_round=pre_round, panel_credit_s=panel_s, **seq_kwargs)
    best, verdict = outcome.best, outcome.verdict
    if judged_start:
        outcome.counters.full_checks += 1
    if trace is not None:
        trace.record("v5_phase", phase="class_loop", seconds=dict(outcome.counters.phase_s),
                     full_s=round(outcome.counters.full_s, 1), loop_wall_s=round(loop_wall, 1))
    # CRS v5 r2 (coordinator, from the step-13 wave): the fallback gets the search_ai arm's OWN search wall, whatever
    # the class loop spent - the loop's share is EXTRA, on top of it, never taken out of it. At 4a263b160 the fallback
    # got wall_s minus the loop's time: on 4 of the 5 files v5 lost to search_ai the loop kept NOTHING and left the
    # fallback 286-527 s of search_ai's 600 s. Only the FILE wall still bounds it.
    left = wall_s
    file_left = wall.remaining_s()       # the FILE wall (step 4), when the harness set one: the tighter bound wins
    if file_left is not None:
        left = min(left, file_left - (reserve_s or 0.0))     # r2h: and the delivery's reserve stays untouched
    improved = False
    if bool(getattr(verdict, "clean", False)):
        why, result = "not run: the class repairs made the part clean", None
    elif fallback is None:
        why, result = "not run: no fallback search configured", None
    elif left < MIN_FALLBACK_S:
        why, result = f"not run: {left:.0f} s of wall left (< {MIN_FALLBACK_S:g} s)", None
    else:
        EV.enter("fallback", wall_left_s=round(left, 1), **EV.NO_DEFECT)
        result, best, verdict, improved, why = _run_fallback(fallback, judge, start, start_verdict, best, verdict,
                                                             left, clock)
    EV.emit("fallback_end", ran=result is not None, reason=why)
    if trace is not None:
        trace.record("v5_fallback", line=why)
    return V5Outcome(seq=outcome, fallback=result, fallback_why=why, best=best, verdict=verdict, matches=matches,
                     notes=notes, fallback_improved=improved)


def fallback_starts(judge: Any, start: Workspace, start_verdict: Any, best: Workspace,
                    verdict: Any) -> list[tuple[str, Workspace, Any]]:
    """The fallback's starts, in order, as (name, state, its full verdict).

    MEASURED (CRS v5 r2 fix I, the round-2 wave df39e5e29): v5 lost both files it lost to search_ai (0daa964f7139,
    7ab61ea7d641) because its fallback searched from the CLASS-REPAIRED state for its whole wall and ended unclean,
    while search_ai, from the START state, cleaned them in 32 s and 274 s. On both the class state was no more VALID
    than the start (0daa964f7139 had MORE panel passes, 3 against 1, and still lost), and of the 30 fallbacks run
    from a class-repaired state none was clean where search_ai was not. So: search_ai's OWN start first - the fallback
    is then search_ai, whole wall included - and the class-repaired state second with what that search leaves;
    unless the class loop made a part VALID that was not (score key 0), where it goes first, as before."""
    if best is start or start_verdict is None:
        return [("class-repaired" if best is not start else "start", best, verdict)]
    ordered = [("start", start, start_verdict), ("class-repaired", best, verdict)]
    return ordered[::-1] if _valid_key(judge, verdict) > _valid_key(judge, start_verdict) else ordered


def _valid_key(judge: Any, verdict: Any) -> int:
    s = _score(judge, verdict)
    return int(s[0]) if s else -1


def _score(judge: Any, verdict: Any) -> tuple | None:
    try:
        return tuple(judge.score(verdict))
    except Exception:  # noqa: BLE001 - an inconclusive verdict ranks below every measured one
        return None


def _rank(judge: Any, verdict: Any) -> tuple:
    s = _score(judge, verdict)
    return (bool(getattr(verdict, "clean", False)), s is not None, s or ())


def _run_fallback(fallback: Fallback, judge: Any, start: Workspace, start_verdict: Any, best: Workspace,
                  verdict: Any, left: float, clock: Callable[[], float]) -> tuple[Any, Workspace, Any, bool, str]:
    """(the result kept, best state, its verdict, whether the fallback improved on the class loop, the row's line).
    The first start gets the whole `left`; the second only what the first left (and the file wall allows), and only
    when the first did not end clean. The best of the class loop's state and every search's best is kept (ties: the
    class loop's)."""
    starts = fallback_starts(judge, start, start_verdict, best, verdict)
    runs: list[tuple[str, float, Any]] = []
    budget = left
    for k, (name, ws0, v0) in enumerate(starts):
        if k:
            file_left = wall.remaining_s()
            budget = budget if file_left is None else min(budget, file_left)
            if budget < MIN_FALLBACK_S:
                break
        t = clock()
        runs.append((name, budget, fallback(ws0, v0, budget)))
        budget -= clock() - t
        if bool(getattr(runs[-1][2], "clean", False)):
            break
    kept, improved = runs[0][2], False
    for _name, _s, r in runs:
        if bool(getattr(r, "changed", False)) and _rank(judge, r.best_verdict) > _rank(judge, verdict):
            kept, best, verdict, improved = r, r.best, r.best_verdict, True
    lines = [f"search_ai from the {name} state with {s:.0f} s: "
             f"{'clean' if getattr(r, 'clean', False) else 'not clean'} ({getattr(r, 'reason', '?')})"
             for name, s, r in runs]
    if len(starts) > len(runs) and not bool(getattr(runs[-1][2], "clean", False)):
        lines.append(f"the {starts[len(runs)][0]} start not run: {max(budget, 0.0):.0f} s left")
    why = "; ".join(lines) + ("; improved on the class loop" if improved else "; no better than the class loop")
    return kept, best, verdict, improved, why
