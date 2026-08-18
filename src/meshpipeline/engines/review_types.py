# Responsibility: Define the vocabulary a mesh is judged in: machine-checkable criteria and qualitative axes.
# Boundaries: types only, deliberately shared by every engine so a bar means the same thing everywhere.
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Criterion:
    key:          str    # measurement key (see measurements_from below)
    label:        str    # human-readable name
    op:           str    # "==" | "<=" | ">" | "empty"
    threshold:    object # comparison value ("empty" ignores it)
    gating:       bool   # True = failing this fails production-grade; False = advisory
    rationale:    str    # WHY this bar exists (user-facing)
    evidence_url: str    # authoritative citation (user-facing)

    def evaluate(self, measurements: dict) -> bool | None:
        if self.key not in measurements or measurements[self.key] is None:
            return None
        v = measurements[self.key]
        # a non-finite measurement (NaN / +Infinity / -Infinity) is NOT a real measurement. Left
        # to ordinary comparison it silently misjudges - `nan <= x` and `nan > x` are BOTH False, and
        # `+inf > x` is True - so a bad value could pass or fail a bar by accident. Report it as "not
        # evaluated" (None -> UNAVAILABLE in the collector) so it can never satisfy a criterion, for
        # EVERY comparison direction. int is always finite; only float carries nan/inf.
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if self.op == "empty":
            return not v
        if self.op == "==":
            return v == self.threshold
        if self.op == "<=":
            return v <= self.threshold
        if self.op == ">":
            return v > self.threshold
        raise ValueError(f"unknown criterion op: {self.op}")


@dataclass(frozen=True)
class ReviewAxis:
    name: str                              # stable dedup key across the engine∪purpose union
    validation_axis: str                   # the ValidationAxis VALUE it serves (integrity/quality/solvability/conformance)
    guidance: str                          # what to inspect + why it matters (qualitative)
    # WHAT A FAILURE OF THIS AXIS MEANS, said to the USER. `guidance` is written FOR the
    # reviewer and `name` is a dedup key - neither may be shown to a customer (a live run
    # would have pinned `surface_staircasing_adequacy` on their screen). Same contract as
    # GateSpec.proves: the concern is engine-declared, because the axis is.
    concern: str = ""
    failure_signals: tuple[str, ...] = ()  # what a failure looks like (qualitative)
    evidence: tuple[str, ...] = ()          # what to weigh: render, domain_extents, quality_metrics, groups, brief…
    evidence_url: str = ""                  # authoritative citation where available
    owner: str = ""                          # STAMPED at composition: "engine:<name>" | "purpose:<key>"
    # GROUNDING (anti-fabrication contract, enforced by test_review_rubric). A vision model cannot
    # reliably judge a fine visual property from a render - it confabulates. So a render-based axis
    # must ground its judgement: either declare typed `requires` (the authoritative successor - a
    # metric/gate/view/target obligation the ledger must satisfy), or mark itself `visual_only` for
    # a property that genuinely cannot be reduced to evidence beyond the render (the SHAPE is the
    # right geometry; acceptable cut-cell staircasing). (`anchor` - an informal manifest.quality key
    # list - was the migration bridge to `requires`; it was removed in Commit D once every engine
    # had moved to typed requirements.)
    visual_only: bool = False              # genuinely unmeasurable - judged from the render, justified in guidance
    requires: tuple = ()                   # tuple[EvidenceRequirement, ...] (avoids a contracts import cycle)


__all__ = ["Criterion", "ReviewAxis"]
