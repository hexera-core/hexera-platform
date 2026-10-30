# Responsibility: Accumulate what a review observed, so a verdict traces to evidence rather than assertion.
# Owns: the evidence record types, per-target obligations, and conflict detection between observations.
# Boundaries: it stores and reconciles observations; it does not decide the verdict and does not render anything.
# Collaborates with: contracts/review_evidence.py for the requirement types and agents/reviewer/ which fills the ledger.
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from meshpipeline.contracts.review_evidence import TargetKind


class EvidenceStatus(str, Enum):

    PASS = "pass"                   # noqa: S105 - a status value, not a secret
    FAIL = "fail"
    ERROR = "error"                 # the producer raised / could not compute - not a FAIL
    UNAVAILABLE = "unavailable"     # no result exists for this key
    INFO = "info"                   # a record with no pass/fail semantics (renders, diagnostics)


@dataclass(frozen=True)
class InspectionTargetRef:

    kind: TargetKind
    target_id: str


#: HOW MANY NAMED TARGETS OF ONE KIND A REVIEW CAN AFFORD TO INSPECT, from the view budget:
#:
#:      sandbox.review_session.MAX_IMAGES_PER_SESSION        64
#:      the opening view                                     -1
#:      the declared interior cuts (3 on the conjugate plate,
#:      at most 4 on a snap-grid board: results.HOT_PARTS+1)  -4
#:                                                           ---
#:      left for the reviewer                                 59
#:
#: An inspection is one image (toggle_patch), and two when the reviewer frames it first with the
#: go_to_coordinates line the prompt hands it per patch (agents/reviewer/context, PATCH VIEWS) -
#: which is what the prompt steers, and framing is NOT an inspection on its own. So a floor of k
#: costs about 2k of those 59. A floor that eats the budget leaves nothing to look twice with,
#: and a review that cannot look twice at what it found is not a review, so the floor is held to
#: roughly a third of it: 19 images, about 10 targets.
#:
#: WHAT THAT BUYS, against the entity counts actually delivered. cfMesh and snappy render the
#: body's own named patches (1 to 3 in every roster on the box); the two placed-parts engines
#: render one entity per region, and their delivered jobs are 2 regions (snappy_multiregion
#: bb5c9179) and 9 (snap-grid f8542f71); the conjugate engine is always 2. Every one of those is
#: under the floor, so every real job is held to EXACT coverage. Only the stress cases - a
#: 19-solid assembly, a generated 2,000-part board - sample, and they sample 10 where the
#: kind-only obligation they had asked for 1.
PATCH_INSPECTION_FLOOR = 10


@dataclass(frozen=True)
class TargetObligation:

    kind: TargetKind
    exact_ids: tuple[str, ...] = ()
    min_count: int = 0
    provenance: str = ""
    #: Of `exact_ids`, how many must carry a usable INSPECTION. 0 (the default) means all of
    #: them, which is what every obligation meant before this field existed.
    #:
    #: THE TWO CONSUMERS OF AN OBLIGATION COST DIFFERENT THINGS, and that is the whole reason
    #: this exists. eligibility.missing_target_obligations compares the named ids against what
    #: the session discovered: a set comparison, before the model is asked, costing nothing -
    #: and it is the check that catches an entity the mesh HAS and the review surface LOST,
    #: which is the shape job 575f76e6 came back in. eligibility._check_obligation demands an
    #: actual inspection per id, costing an image each out of a budget of 64.
    #:
    #: Naming every entity and demanding every inspection ties the cheap check to the expensive
    #: one: it is why the four flow engines were left on a kind-only obligation asking for ONE
    #: patch of any kind, because exact coverage of a 2,000-part board is not payable. Split,
    #: both can be right - every entity is named, so none can go missing unnoticed, and the
    #: inspection floor is what the budget affords (PATCH_INSPECTION_FLOOR, above).
    inspect_at_least: int = 0

    def required_count(self) -> int:
        """How many of this kind must be PRODUCED. Unchanged, and read by discovery."""
        if self.exact_ids:
            return len(self.exact_ids)
        return max(self.min_count, 1)

    def required_inspections(self) -> int:
        """How many must be INSPECTED - never more than there are, never fewer than one."""
        if not self.exact_ids:
            return max(self.min_count, 1)
        if self.inspect_at_least <= 0:
            return len(self.exact_ids)
        return max(1, min(self.inspect_at_least, len(self.exact_ids)))

    @property
    def samples(self) -> bool:
        """Whether the reviewer may choose WHICH of the named ids to inspect."""
        return self.required_inspections() < len(self.exact_ids)


# typed evidence records
# Each record is immutable, carries a stable review-local `evidence_id`, and declares whether it is
# `usable` to satisfy a requirement. `category` is a class constant so queries need no isinstance
# ladder and a new record type cannot silently miss a query.
@dataclass(frozen=True)
class HardGateEvidence:
    category: ClassVar[str] = "gate"
    evidence_id: str
    gate_key: str
    status: EvidenceStatus
    summary: str
    source: str
    usable: bool

    @property
    def passed(self) -> bool:
        return self.status is EvidenceStatus.PASS


@dataclass(frozen=True)
class MetricEvidence:
    category: ClassVar[str] = "metric"
    evidence_id: str
    metric_key: str
    value: Any                      # float | str | structured result | None
    acceptable: bool | None        # None when no threshold is declared for this metric
    status: EvidenceStatus
    summary: str
    source: str
    usable: bool
    #: True = failing this metric fails the mesh; False = ADVISORY, worth reporting and not worth
    #: refusing over. The engine already draws this line (review_types.Criterion.gating) and
    #: quality_criteria honours it when deciding production_grade. Carrying it here keeps the
    #: reviewer's judgement consistent with the engine's: without it every required metric reads as
    #: gating, and a 2.1M-cell mesh was destroyed because max_non_ortho measured 65.0156 against an
    #: advisory bar of 65.0. Defaults True so an unflagged metric keeps blocking.
    gating: bool = True

    @property
    def passed(self) -> bool:
        return self.status is EvidenceStatus.PASS


@dataclass(frozen=True)
class RenderViewEvidence:
    category: ClassVar[str] = "render_view"
    evidence_id: str
    view_id: str
    artifact_key: str               # safe provenance - a declared key, NEVER a filesystem path
    operation: str
    image_ok: bool
    usable: bool


@dataclass(frozen=True)
class TargetInspectionEvidence:
    category: ClassVar[str] = "target_inspection"
    evidence_id: str
    target: InspectionTargetRef
    operation: str
    image_ok: bool
    covered: bool                   # the session confirmed it inspected THIS target
    usable: bool


@dataclass(frozen=True)
class ValidationRecord:

    category: ClassVar[str] = "validation"
    evidence_id: str
    detail: str
    operation: str = ""
    usable: bool = False


LedgerRecord = (HardGateEvidence | MetricEvidence | RenderViewEvidence
                | TargetInspectionEvidence | ValidationRecord)


@dataclass(frozen=True)
class EvidenceConflict:

    conflict_id: str
    summary: str
    left_id: str
    right_id: str
    resolved: bool = False
    resolved_by: str = ""


class EvidenceLedger:

    def __init__(self) -> None:
        self._records: list[LedgerRecord] = []
        self._by_id: dict[str, LedgerRecord] = {}
        self._counters: dict[str, int] = {}
        self._conflicts: list[EvidenceConflict] = []

 # id minting
    def _mint(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:03d}"

    def _append(self, record: LedgerRecord) -> str:
        if record.evidence_id in self._by_id:
            raise ValueError(f"duplicate evidence id {record.evidence_id!r}")
        self._records.append(record)
        self._by_id[record.evidence_id] = record
        return record.evidence_id

 # producers (already-typed data in; the ledger interprets nothing)
    def add_gate(self, gate_key: str, status: EvidenceStatus, summary: str, source: str) -> str:
        # A gate is usable evidence whenever it produced a definite pass/fail; an error or an
        # absence is recorded but not usable, so a required gate is never "satisfied" by a
        # calculation that never ran.
        usable = status in (EvidenceStatus.PASS, EvidenceStatus.FAIL)
        return self._append(HardGateEvidence(self._mint("g"), gate_key, status, summary, source,
                                             usable))

    def add_metric(self, metric_key: str, value: Any, acceptable: bool | None,
                   status: EvidenceStatus, summary: str, source: str,
                   gating: bool = True) -> str:
        usable = status in (EvidenceStatus.PASS, EvidenceStatus.FAIL)
        return self._append(MetricEvidence(self._mint("m"), metric_key, value, acceptable, status,
                                           summary, source, usable, gating))

    def add_render_view(self, view_id: str, artifact_key: str, operation: str,
                        image_ok: bool) -> str:
        return self._append(RenderViewEvidence(self._mint("v"), view_id, artifact_key, operation,
                                               image_ok, usable=image_ok))

    def add_target_inspection(self, target: InspectionTargetRef, operation: str, image_ok: bool,
                              covered: bool) -> str:
        # Usable only when the session both COVERED the requested target and produced a real image.
        # A generic screenshot (covered=False) or a failed render (image_ok=False) records the
        # attempt but can never stand in for target-specific evidence.
        return self._append(TargetInspectionEvidence(self._mint("t"), target, operation, image_ok,
                                                     covered, usable=(image_ok and covered)))

    def add_validation(self, detail: str, operation: str = "") -> str:
        return self._append(ValidationRecord(self._mint("x"), detail, operation))

 # conflicts
    def add_conflict(self, summary: str, left_id: str, right_id: str) -> str:
        cid = self._mint("c")
        self._conflicts.append(EvidenceConflict(cid, summary, left_id, right_id))
        return cid

    def resolve_conflict(self, conflict_id: str, resolved_by: str) -> None:
        for i, c in enumerate(self._conflicts):
            if c.conflict_id == conflict_id:
                self._conflicts[i] = EvidenceConflict(
                    c.conflict_id, c.summary, c.left_id, c.right_id, resolved=True,
                    resolved_by=resolved_by)
                return
        raise KeyError(conflict_id)

    @property
    def conflicts(self) -> tuple[EvidenceConflict, ...]:
        return tuple(self._conflicts)

    def unresolved_conflicts(self) -> tuple[EvidenceConflict, ...]:
        return tuple(c for c in self._conflicts if not c.resolved)

 # lookup + queries
    def get(self, evidence_id: str) -> LedgerRecord | None:
        return self._by_id.get(evidence_id)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._by_id

    def __len__(self) -> int:
        return len(self._records)

    def _of(self, category: str) -> list:
        return [r for r in self._records if r.category == category]

    def gates(self) -> list[HardGateEvidence]:
        return self._of("gate")

    def metrics(self) -> list[MetricEvidence]:
        return self._of("metric")

    def render_views(self) -> list[RenderViewEvidence]:
        return self._of("render_view")

    def target_inspections(self) -> list[TargetInspectionEvidence]:
        return self._of("target_inspection")

    def usable_gate(self, gate_key: str) -> HardGateEvidence | None:
        for g in self.gates():
            if g.gate_key == gate_key and g.usable:
                return g
        return None

    def usable_metric(self, metric_key: str) -> MetricEvidence | None:
        for m in self.metrics():
            if m.metric_key == metric_key and m.usable:
                return m
        return None

    def usable_view(self, view_id: str) -> RenderViewEvidence | None:
        for v in self.render_views():
            if v.view_id == view_id and v.usable:
                return v
        return None

    def covered_targets(self) -> set[InspectionTargetRef]:
        return {t.target for t in self.target_inspections() if t.usable}

    def usable_inspections_of_kind(self, kind: TargetKind) -> list[TargetInspectionEvidence]:
        return [t for t in self.target_inspections() if t.usable and t.target.kind is kind]

    def usable_inspection_of_target(self, ref: InspectionTargetRef) -> TargetInspectionEvidence | None:
        for t in self.target_inspections():
            if t.usable and t.target == ref:
                return t
        return None


__all__ = [
    "EvidenceStatus",
    "InspectionTargetRef",
    "TargetObligation",
    "HardGateEvidence",
    "MetricEvidence",
    "RenderViewEvidence",
    "TargetInspectionEvidence",
    "ValidationRecord",
    "EvidenceConflict",
    "LedgerRecord",
    "EvidenceLedger",
]
