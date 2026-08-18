# Responsibility: Say what new evidence a round actually added.
# Boundaries: an explicit before/after comparison.
from __future__ import annotations

from dataclasses import dataclass

from meshpipeline.contracts.evidence_ledger import EvidenceLedger


@dataclass(frozen=True)
class EvidenceSnapshot:

    usable: frozenset[str]
    unusable: frozenset[str]

    @property
    def all_ids(self) -> frozenset[str]:
        return self.usable | self.unusable

    @classmethod
    def of(cls, ledger: EvidenceLedger) -> EvidenceSnapshot:
        usable: set[str] = set()
        unusable: set[str] = set()
        for record in ledger._records:      # noqa: SLF001 - the ledger's own iteration surface
            (usable if getattr(record, "usable", False) else unusable).add(record.evidence_id)
        return cls(frozenset(usable), frozenset(unusable))


@dataclass(frozen=True)
class EvidenceDelta:

    newly_usable: frozenset[str] = frozenset()      # collected AND usable since the last point
    newly_unusable: frozenset[str] = frozenset()    # collected but NOT usable
    became_usable: frozenset[str] = frozenset()     # already present, now usable
    became_unusable: frozenset[str] = frozenset()   # already present, no longer usable
    repeated: frozenset[str] = frozenset()          # cited again; no ledger change

    @property
    def collected_usable_evidence(self) -> bool:
        return bool(self.newly_usable or self.became_usable)

    @property
    def changed(self) -> bool:
        return bool(self.newly_usable or self.newly_unusable
                    or self.became_usable or self.became_unusable)

    def counts(self) -> dict[str, int]:
        return {
            "newly_usable": len(self.newly_usable),
            "newly_unusable": len(self.newly_unusable),
            "became_usable": len(self.became_usable),
            "became_unusable": len(self.became_unusable),
            "repeated": len(self.repeated),
        }


def diff(before: EvidenceSnapshot, after: EvidenceSnapshot,
         *, cited: frozenset[str] = frozenset()) -> EvidenceDelta:
    added = after.all_ids - before.all_ids
    return EvidenceDelta(
        newly_usable=frozenset(added & after.usable),
        newly_unusable=frozenset(added & after.unusable),
        became_usable=frozenset((after.usable - before.usable) - added),
        became_unusable=frozenset((after.unusable - before.unusable) - added),
        repeated=frozenset(cited & before.all_ids))


__all__ = ["EvidenceDelta", "EvidenceSnapshot", "diff"]
