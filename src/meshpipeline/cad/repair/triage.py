# Responsibility: Recommend what to do with an inspected file - as advice an operator reads, never
#                 as a decision anything acts on.
# Boundaries: a PURE FUNCTION over a stored report. It holds no session, takes no repository, calls
#             no model, and returns no state update, so there is no path by which a recommendation
#             can move a job, touch geometry, or mark anything delivered.
# Collaborates with: cad/repair/contracts.py (the vocabulary), api/v1/admin_repair.py (which shows it).
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from meshpipeline.cad.repair.contracts import DefectCode, RepairProfile, RepairStatus

# SHADOW MODE, AND WHY IT IS DELIBERATELY NOT A MODEL YET.
#
# The roadmap's posture is that AI classifies, plans and explains while deterministic tools own
# the geometry. Before a model can be allowed to recommend anything, there has to be something to
# measure it against and a corpus to learn from, and this repository has neither yet: no operator
# has made a recorded repair decision. So this is the BASELINE - rules over the defect taxonomy
# the inspection already produces. It is what a model must beat on operator agreement before it
# may take this function's place, and the operator decisions it is shown beside are the labels
# that training will use.
#
# It is also the shape the model will have to fit: a route, a confidence, the evidence behind it,
# and the right to abstain. An honest "I do not know" is worth more than a confident guess about
# somebody's geometry, so abstention is a first-class answer rather than a low score.

#: What the service can do with a file, named as routes rather than as states - the operator's
#: decision vocabulary (api/v1/admin_repair._DECISIONS) is what turns one of these into a move.
ROUTE_MESH_AS_IS = "mesh_as_is"
ROUTE_CONSERVATIVE_REPAIR = "conservative_repair"
ROUTE_MANUAL_CLEANUP = "manual_cleanup"
ROUTE_ASK_CUSTOMER = "ask_customer"
ROUTE_ABSTAIN = "abstain"

#: Defects a bounded, inspectable repair is the right answer to: things wrong with how the file was
#: WRITTEN - gaps, orderings, degeneracies - rather than with what the part IS.
_REPAIRABLE: frozenset[str] = frozenset({
    DefectCode.wire_gap.value,
    DefectCode.small_edge.value,
    DefectCode.degenerate_edge.value,
    DefectCode.curve_inconsistency.value,
    DefectCode.open_shell.value,
    DefectCode.duplicate_surface_data.value,
    # ShapeFix repairs an orientation, and the caps catch it if the fix overreaches.
    DefectCode.bad_orientation.value,
    # A tolerance outlier is usually the written trace of a gap somebody widened the tolerance to
    # hide; re-fixing it at a sane tolerance is exactly the conservative pass's job.
    DefectCode.tolerance_outlier.value,
})

#: Defects that need a person. Each one is a question about intent that no cap can answer: whether
#: a self-intersection is a modelling error or a deliberate overlap, whether a non-manifold edge
#: is three walls meeting or a mistake, whether a small face matters.
_NEEDS_JUDGEMENT: frozenset[str] = frozenset({
    DefectCode.self_intersection.value,
    DefectCode.non_manifold_surface.value,
    DefectCode.small_face.value,
})

#: Defects that mean we were handed something we cannot work with at all - the customer has to
#: send a different file, and no amount of repair substitutes for that.
_CUSTOMER_MUST_ACT: frozenset[str] = frozenset({
    DefectCode.invalid_brep.value,
})


@dataclass(frozen=True, slots=True)
class Recommendation:
    """Advice, with its evidence. Carries no authority and no side effect."""

    route: str
    confidence: float
    #: The defect codes and facts this rests on. Shown beside the advice so an operator can
    #: disagree with the REASONING rather than just the conclusion - which is also what makes a
    #: disagreement useful as a training signal.
    reasons: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)
    #: Set when the recommender declined to advise, and why. Never dressed up as a low-confidence
    #: recommendation: "I do not know" and "probably repair" are different answers.
    abstain_reason: str = ""
    #: The profile a repair route would use. Empty for every other route.
    profile: str = ""
    #: THE LOCATED FAILURE POINTS THIS ADVICE IS ABOUT: entity, region, size and position, taken
    #: verbatim from the inspection. A route without targets is a verdict on the whole part; with
    #: them, an executor has something to aim at and a person has somewhere to look. This is also
    #: the per-defect context a model would be given, which is why it is assembled here rather
    #: than left for each caller to dig out of the report.
    targets: tuple[dict, ...] = ()

    @property
    def abstained(self) -> bool:
        return self.route == ROUTE_ABSTAIN

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "confidence": round(float(self.confidence), 3),
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "abstain_reason": self.abstain_reason,
            "profile": self.profile,
            "targets": [dict(t) for t in self.targets],
            # WHO IS ADVISING, stated in the payload itself. When a model takes this over, a
            # recorded recommendation has to say which advisor produced it or the corpus cannot
            # tell the baseline's agreement from the model's.
            "advisor": "rules-v1",
            # SHADOW ALWAYS, for now. A reader that acts on this is contradicting the payload.
            "shadow": True,
        }


def _codes(report: Mapping) -> list[str]:
    inner = report.get("report") if isinstance(report.get("report"), Mapping) else report
    defects = inner.get("defects") if isinstance(inner, Mapping) else None
    if not isinstance(defects, list):
        return []
    return [str(d.get("code")) for d in defects
            if isinstance(d, Mapping) and d.get("code")]


def _severities(report: Mapping) -> set[str]:
    inner = report.get("report") if isinstance(report.get("report"), Mapping) else report
    defects = inner.get("defects") if isinstance(inner, Mapping) else None
    if not isinstance(defects, list):
        return set()
    return {str(d.get("severity")) for d in defects
            if isinstance(d, Mapping) and d.get("severity")}


def _entities(report: Mapping) -> list[dict]:
    """The located failure points, from wherever the report carries them.

    Tolerates both shapes: a report nested under "report" (the stored RepairResult payload) and a
    bare RepairReport dict. An older report has none, which is not an error - it is the absence
    this module treats as "nothing to aim at".
    """
    for candidate in (report.get("report"), report):
        if isinstance(candidate, Mapping):
            found = candidate.get("entities")
            if isinstance(found, list):
                return [dict(e) for e in found if isinstance(e, Mapping)]
    return []


def _has_unattributed_invalid(report: Mapping) -> bool:
    """Whether the report says "invalid, and we cannot say which entity"."""
    inner = report.get("report") if isinstance(report.get("report"), Mapping) else report
    defects = inner.get("defects") if isinstance(inner, Mapping) else None
    if not isinstance(defects, list):
        return False
    for defect in defects:
        if not isinstance(defect, Mapping):
            continue
        if defect.get("code") != DefectCode.invalid_brep.value:
            continue
        details = defect.get("details")
        if isinstance(details, Mapping) and details.get("reason") == "unattributed":
            return True
    return False


#: How many targets travel with one recommendation. A part with a thousand sliver faces does not
#: need a thousand of them in a payload an operator reads or a model is prompted with - the count
#: is in the evidence, and the worst are what gets looked at.
_MAX_TARGETS = 20


def _targets(entities: list[dict]) -> tuple[dict, ...]:
    """The failure points to aim at, worst first, trimmed to a readable number."""
    rank = {"fatal": 3, "error": 2, "warning": 1, "info": 0}
    ordered = sorted(entities, key=lambda e: -rank.get(str(e.get("severity")), 0))
    out = []
    for entity in ordered[:_MAX_TARGETS]:
        out.append({
            "entity": entity.get("entity", ""),
            "code": entity.get("code", ""),
            "severity": entity.get("severity", ""),
            "region": entity.get("region", "unknown"),
            "message": entity.get("message", ""),
            "measurements": dict(entity.get("measurements") or {}),
            "location": dict(entity.get("location") or {}),
        })
    return tuple(out)


def recommend(*, repair_status: str = "", report: Mapping | None = None,
              target_engine: str = "") -> Recommendation:
    """What to do with this file, on the evidence of its inspection. Advice only.

    THE PRECEDENCE, AND WHY IT IS THIS WAY ROUND. Before defects were localised the only thing a
    STEP inspection could say was `invalid_brep`, that code routed to the customer, and so the
    conservative repair could never be recommended for the one format it can repair. Now the
    question is not "is the part invalid" but "can we point at what is wrong":

      1. nothing to go on, or something this baseline has no rule for  -> abstain
      2. the file is unusable at all (no faces, unreadable)            -> ask the customer
      3. invalid but attributed to NO entity                           -> a person looks
      4. a defect that is a question about intent                      -> a person looks
      5. every located defect is in how the file was written            -> repair it
      6. nothing found                                                  -> mesh as it arrived

    Step 3 is the honest reading of "invalid, cause unknown": there is nothing for a repair to aim
    at, but bouncing it back to the customer having diagnosed nothing is poor service. A person
    decides whether to ask them or dig further.
    """
    payload = dict(report or {})

    if payload.get("service_failure"):
        # OUR tooling failed. Saying anything about the file on that basis would be inventing a
        # verdict, and the inspection itself already refuses to.
        return Recommendation(
            route=ROUTE_ABSTAIN, confidence=0.0,
            abstain_reason="the inspection did not complete, so there is no evidence to advise on")

    if not payload:
        return Recommendation(route=ROUTE_ABSTAIN, confidence=0.0,
                              abstain_reason="this job has no inspection report")

    status = str(repair_status or "")
    if status == RepairStatus.inconclusive.value:
        return Recommendation(route=ROUTE_ABSTAIN, confidence=0.0,
                              abstain_reason="the inspection was inconclusive")

    codes = _codes(payload)
    entities = _entities(payload)
    evidence = {"defect_codes": sorted(set(codes)), "repair_status": status,
                "target_engine": target_engine,
                "located_entities": len(entities)}

    if status == RepairStatus.clean.value and not codes:
        return Recommendation(
            route=ROUTE_MESH_AS_IS, confidence=0.9,
            reasons=("the inspection found no defects",), evidence=evidence)

    unknown = [c for c in codes
               if c not in _REPAIRABLE | _NEEDS_JUDGEMENT | _CUSTOMER_MUST_ACT]
    if unknown:
        # A defect this baseline has no rule for. Guessing would be worse than saying so.
        return Recommendation(
            route=ROUTE_ABSTAIN, confidence=0.0, evidence=evidence,
            abstain_reason=f"no rule covers {', '.join(sorted(set(unknown)))}")

    # THE FILE IS UNUSABLE AS DELIVERED. No faces, or the kernel could not read it: there is no
    # geometry to repair, and only a different file helps.
    if "fatal" in _severities(payload):
        return Recommendation(
            route=ROUTE_ASK_CUSTOMER, confidence=0.85,
            reasons=("the file cannot be read or carries no geometry to mesh",),
            evidence=evidence)

    # INVALID, AND NOT FATAL. One rule for every shape of this, because the distinction that
    # matters is whether we can POINT at the problem - not how the code happened to be written
    # down. Nothing to aim a repair at, and nothing diagnosed worth bouncing a customer over, so a
    # person looks. (Before localisation this case went straight back to the customer, which meant
    # we asked them for a new file having diagnosed nothing.)
    if any(c in _CUSTOMER_MUST_ACT for c in codes):
        _unattributed = _has_unattributed_invalid(payload)
        return Recommendation(
            route=ROUTE_MANUAL_CLEANUP, confidence=0.5,
            reasons=((("OpenCASCADE calls the part invalid but attributes it to no entity, so "
                       "there is nothing for an automatic repair to aim at"),)
                     if _unattributed else
                     ("the part's B-rep is invalid in a way this baseline cannot localise",)),
            evidence=evidence, profile=RepairProfile.manual_review.value)

    judgement = [e for e in entities if e.get("code") in _NEEDS_JUDGEMENT]
    if any(c in _NEEDS_JUDGEMENT for c in codes):
        return Recommendation(
            route=ROUTE_MANUAL_CLEANUP, confidence=0.6,
            reasons=("the defects found are questions about intent that no cap can answer",),
            evidence=evidence, profile=RepairProfile.manual_review.value,
            targets=_targets(judgement or entities))

    if codes:
        repairable = [e for e in entities if e.get("code") in _REPAIRABLE]
        return Recommendation(
            route=ROUTE_CONSERVATIVE_REPAIR, confidence=0.7,
            reasons=("every defect found is in how the file was written, not in what the part is",),
            evidence=evidence, profile=RepairProfile.conservative.value,
            # WHAT A REPAIR WOULD AIM AT. Without these a recommendation to repair is still just a
            # verdict on the whole part; with them an executor - or a person - has the entity, its
            # size and its position.
            targets=_targets(repairable))

    # A status that reports defects with none listed, or anything else this baseline cannot read.
    return Recommendation(route=ROUTE_ABSTAIN, confidence=0.0, evidence=evidence,
                          abstain_reason=f"the report is not readable as advice (status {status!r})")
