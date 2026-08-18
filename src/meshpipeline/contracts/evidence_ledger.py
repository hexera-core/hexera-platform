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


@dataclass(frozen=True)
class TargetObligation:

    kind: TargetKind
    exact_ids: tuple[str, ...] = ()
    min_count: int = 0
    provenance: str = ""

    def required_count(self) -> int:
        if self.exact_ids:
            return len(self.exact_ids)
        return max(self.min_count, 1)


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
                   status: EvidenceStatus, summary: str, source: str) -> str:
        usable = status in (EvidenceStatus.PASS, EvidenceStatus.FAIL)
        return self._append(MetricEvidence(self._mint("m"), metric_key, value, acceptable, status,
                                           summary, source, usable))

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
