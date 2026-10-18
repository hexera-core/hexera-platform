# Responsibility: Decide what each finding of a concluded review may do: block the job, ask for a rebuild, or stand as a concern.
# Owns: the wrong-problem taxonomy, the builder's levers as a review may name them, the brief-citation
#   check, the measured vetoes, and the per-finding judgement.
# Boundaries: pure. Validity is the gates'; this only reads a review's findings, the brief and the
#   measurements the gates already took.
"""A REVIEW MAY FAIL A JOB ONLY WHEN THE MESH IS THE WRONG PROBLEM.

The deterministic gates - structure, quality bars, passages, boundaries present and typed, the trial
solve - decide whether a mesh is VALID. Over a mesh that passed them all, a review can say four
things, and this module decides which one each finding that did not pass is:

* BLOCKING - the mesh represents a different problem than the user asked for. Only one of the fixed
  WRONG_PROBLEM classes below, backed by an inspection of the mesh, quoting the confirmed fact or
  brief requirement it contradicts, and not contradicted by a measurement the gates already took.
* IMPROVE - a rebuild is asked for: the finding quotes a brief requirement, names one of the
  builder's real levers and says what to change. A blocking finding may ask for one too. Whether the
  change really produces a different case is the builder's to find out - a retry that would write
  the case already reviewed is stopped before it meshes.
* CONCERN - everything else: partial, coarser than ideal, locally rough, or a claim the review could
  not tie to the brief. Listed plainly on the delivery; never a rebuild, never a failure.
* (PASS - the axis is met; not judged here.)

Night of 2026-10-01: a literal reviewer model failed good meshes on bars of its own - "the config
has no wake region", "2.4 of 5 layers is not five" - and passed the same numbers on the next
attempt. Under these rules those findings are concerns: the mesh is delivered with them listed.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

#: THE WRONG-PROBLEM TAXONOMY - the only grounds on which a review may fail a job. Fixed and
#: general: a class names a way a mesh can stand for a different problem than the one confirmed,
#: never a part or a criterion. Each maps to the plain words the user reads.
WRONG_PROBLEM: dict[str, str] = {
    "wrong_side_meshed": "the wrong side of the body was meshed (the cells fill the solid, or the "
                         "outside instead of the flow passage)",
    "missing_geometry": "a part of the confirmed geometry is missing from the mesh",
    "wrong_scale": "the mesh is at the wrong scale for the confirmed part size and units",
    "wrong_flow_setup": "the flow setup contradicts what was confirmed (openings swapped, the flow "
                        "direction or the outer-domain margins wrong, or the body touching the "
                        "outer boundary)",
    "wrong_boundaries": "a boundary contradicts what was confirmed (an opening typed as a wall or "
                        "the reverse, a ground or symmetry plane missing, misplaced or cutting the "
                        "body)",
    "no_flow_path": "there is no flow path from the inlet to the outlet through the fluid",
    "shape_changed": "the meshed surface materially differs from the CAD shape",
    "junk_regions": "the mesh has disconnected cell regions apart from the flow region",
    "requirement_absent": "something the brief requires is entirely absent from the mesh",
}
#: What a finding names when it is not a wrong-problem claim.
NOT_WRONG_PROBLEM = "none"

#: THE BUILDER'S LEVERS, in the words a finding may name. Every engine's builder authors the
#: refinement and writes the approved boundaries; layers and the domain exist only where the
#: engine and the workflow have them (builder_levers).
LEVERS: tuple[str, ...] = ("domain", "refinement", "layers", "boundaries")
#: What a finding names when it asks for no change.
NO_LEVER = "none"

#: A quote shorter than this cannot identify a requirement ("mesh", "the wake").
MIN_QUOTE_CHARS = 10


def builder_levers(spec, purpose: str) -> tuple[str, ...]:
    """The levers this engine's builder has for this workflow, from what both DECLARE."""
    from meshpipeline.engines.purposes import topology_of
    have = {"refinement", "boundaries"}
    delivered = getattr(spec, "delivered_mesh", None)
    if delivered is not None and getattr(delivered, "prism_layers", False):
        have.add("layers")
    if topology_of(purpose or "") == "external":
        have.add("domain")       # an outer domain the builder sizes; a cavity or a solid has none
    return tuple(lever for lever in LEVERS if lever in have)


_SPLIT = re.compile(r"\.\.\.|…")


def _norm(text: str) -> str:
    # NFKC folds y⁺ to y+ and similar; punctuation and quote styles never decide a citation
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = re.sub(r"[^\w+%.]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .")


def brief_quote_found(quote: str, brief_text: str) -> bool:
    """Is `quote` really in the brief? Word for word after folding case, spacing and punctuation;
    an ellipsis may join fragments, which must appear in order. Too short to identify a requirement
    is not a citation."""
    fragments = [_norm(f) for f in _SPLIT.split(str(quote or ""))]
    fragments = [f for f in fragments if f]
    if not fragments or sum(len(f) for f in fragments) < MIN_QUOTE_CHARS:
        return False
    if sum(len(f.split()) for f in fragments) < 2:
        return False
    haystack = _norm(brief_text)
    pos = 0
    for f in fragments:
        at = haystack.find(f, pos)
        if at < 0:
            return False
        pos = at + len(f)
    return True


def _inspected(finding, ledger) -> bool:
    """Backed by something the reviewer LOOKED at - an inspection or a view of this mesh. A gate or
    a metric never is: those are the gates' own verdicts, and the review does not re-judge them."""
    from meshpipeline.contracts.evidence_ledger import RenderViewEvidence, TargetInspectionEvidence
    if ledger is None:
        return False
    for eid in getattr(finding, "evidence_ids", ()) or ():
        rec = ledger.get(eid)
        if (isinstance(rec, (TargetInspectionEvidence, RenderViewEvidence))
                and getattr(rec, "usable", False)):
            return True
    return False


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def measured_veto(wrong_problem: str, axis_key: str, measured: dict | None) -> str:
    """A measurement the gates already took that CONTRADICTS a wrong-problem claim, in plain words,
    or "". Only checks that are certain: a claim no measurement speaks to stands on its evidence."""
    m = measured or {}
    if wrong_problem == "junk_regions":
        regions = _num(m.get("regions"))
        if regions is not None and regions <= 1:
            return "the mesh was measured as one connected region"
    if wrong_problem == "wrong_scale":
        body, ruler = _num(m.get("body_length_m")), _num(m.get("reference_length_m"))
        if body and ruler and 0.2 <= body / ruler <= 5.0:
            return (f"the measured body length ({body:.4g} m) matches the confirmed reference "
                    f"length ({ruler:.4g} m) in scale")
    if wrong_problem == "shape_changed":
        raw = m.get("surface_deviation")
        sd: dict = raw if isinstance(raw, dict) else {}
        p95 = _num(sd.get("p95_ratio"))
        beyond = _num(sd.get("frac_beyond_one_cell"))
        if p95 is not None and p95 <= 1.0 and (beyond is None or beyond <= 0.01):
            return "the measured surface deviation keeps the wall within one cell of the CAD"
    if wrong_problem == "requirement_absent" and "layer" in str(axis_key).lower():
        coverage = _num(m.get("layer_coverage_pct"))
        if coverage is not None and coverage > 1.0:
            return f"prism layers were measured on {coverage:.1f}% of the wall - present, not absent"
    return ""


@dataclass(frozen=True)
class Judgement:
    axis_key: str
    #: the WRONG_PROBLEM class this finding was accepted under, or "" - only these can fail a job
    blocking: str = ""
    #: whether it may ask the builder for a rebuild
    improve: bool = False
    #: why it stands only as a concern (or why a claimed class was not accepted), in plain words
    reason: str = ""
    #: "material" or "minor" - how prominently a concern is shown on the delivery
    severity: str = "minor"

    @property
    def concern(self) -> bool:
        return not self.blocking


def judge(finding, *, brief_text: str, levers: tuple[str, ...], ledger,
          measured: dict | None = None) -> Judgement | None:
    """BLOCKING, IMPROVE and CONCERN for one finding that did not pass; None for one that passed."""
    if getattr(finding, "passed", True):
        return None
    key = str(getattr(finding, "axis_key", ""))
    quote = str(getattr(finding, "brief_requirement", "") or "").strip()
    cited = brief_quote_found(quote, brief_text)
    looked = _inspected(finding, ledger)
    severity = ("material" if str(getattr(finding, "severity", "") or "").lower() == "material"
                else "minor")

    claim = str(getattr(finding, "wrong_problem", "") or "").strip().lower()
    blocking, reason = "", ""
    if claim and claim != NOT_WRONG_PROBLEM:
        if claim not in WRONG_PROBLEM:
            reason = "it names no recognised way the mesh could be the wrong problem"
        elif not quote:
            reason = "it cites no confirmed fact or brief requirement it contradicts"
        elif not cited:
            reason = "the fact it quotes is not in your brief or the confirmed setup"
        elif not looked:
            reason = "it is not backed by an inspection of the mesh"
        else:
            reason = measured_veto(claim, key, measured)
            if not reason:
                blocking = claim
        if not blocking:
            severity = "material"    # a wrong-problem claim that could not be confirmed is shown prominently

    lever = str(getattr(finding, "builder_change", "") or "").strip().lower()
    improve = bool(cited and looked and lever in levers
                   and str(getattr(finding, "change_request", "") or "").strip())
    if not blocking and not reason:
        reason = ("" if improve else
                  "it cites no requirement from your brief" if not quote else
                  "the requirement it quotes is not in your brief" if not cited else
                  "it names no change the builder can make" if lever not in levers else
                  "it does not say what to change"
                  if not str(getattr(finding, "change_request", "") or "").strip() else
                  "it is not backed by an inspection of the mesh")
    return Judgement(key, blocking=blocking, improve=improve, reason=reason, severity=severity)


def judge_all(findings, *, brief_text: str, levers: tuple[str, ...], ledger,
              measured: dict | None = None) -> dict[str, Judgement]:
    """{axis_key: Judgement} for every finding that did not pass."""
    out: dict[str, Judgement] = {}
    for f in findings or ():
        j = judge(f, brief_text=brief_text, levers=levers, ledger=ledger, measured=measured)
        if j is not None:
            out[j.axis_key] = j
    return out


def measurements_from(manifest: Mapping | None, state: Mapping | None) -> dict:
    """The measurements the vetoes read, from the manifest the gates wrote and the approved facts."""
    def _mapping(value) -> Mapping:
        return value if isinstance(value, Mapping) else {}

    mf, st = _mapping(manifest), _mapping(state)
    q = _mapping(mf.get("quality"))
    out: dict = {"regions": q.get("regions"), "layer_coverage_pct": q.get("layer_coverage_pct"),
                 "surface_deviation": q.get("surface_deviation"),
                 "reference_length_m": st.get("reference_length_m")}
    box = _mapping(mf.get("geometry")).get("body_box")
    if isinstance(box, dict):
        try:
            out["body_length_m"] = max(float(box["xmax"]) - float(box["xmin"]),
                                       float(box["ymax"]) - float(box["ymin"]),
                                       float(box["zmax"]) - float(box["zmin"]))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def asks_for_rebuild(axis_findings) -> bool:
    """Whether a recorded review asks for a rebuild. A record written before findings were judged
    (an in-flight job across a deploy) carries no `improve` key and keeps the old meaning: any
    finding that did not pass asked for one."""
    rows = [f for f in (axis_findings or []) if isinstance(f, dict)]
    if not any("improve" in f for f in rows):
        # unjudged: a FAIL with no recorded findings, or any finding that did not pass, rebuilds
        return not rows or any(f.get("passed") is False for f in rows)
    return any(f.get("improve") is True for f in rows)


def blocking_findings(axis_findings) -> list[dict]:
    """The recorded findings that make the mesh the wrong problem - the only ones that fail a job."""
    return [f for f in (axis_findings or [])
            if isinstance(f, dict) and f.get("passed") is False
            and str(f.get("blocking") or "") in WRONG_PROBLEM]


__all__ = ["LEVERS", "MIN_QUOTE_CHARS", "NOT_WRONG_PROBLEM", "NO_LEVER", "WRONG_PROBLEM",
           "Judgement", "asks_for_rebuild", "blocking_findings", "brief_quote_found",
           "builder_levers", "judge", "judge_all", "measured_veto", "measurements_from"]
