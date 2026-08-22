# Responsibility: Collect the measured evidence that needs no model to establish.
# Boundaries: measurement read from the manifest and gates.
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus


def _is_finite_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    return False


# Gate-outcome vocabulary the executor's report speaks, mapped to ledger status.
_GATE_STATUS = {
    "pass": EvidenceStatus.PASS,
    "fail": EvidenceStatus.FAIL,
    "error": EvidenceStatus.ERROR,
    "unavailable": EvidenceStatus.UNAVAILABLE,
}


def collect_deterministic_evidence(
    plan,
    ledger: EvidenceLedger,
    *,
    measurements: Mapping[str, Any],
    criteria: Mapping[str, Any],
    gate_status: Mapping[str, str],
) -> None:
    for key in sorted(plan.required_gate_keys):
        status = _GATE_STATUS.get(gate_status.get(key, "unavailable"), EvidenceStatus.UNAVAILABLE)
        summary = {
            EvidenceStatus.PASS: "passed",
            EvidenceStatus.FAIL: "failed",
            EvidenceStatus.ERROR: "could not be evaluated",
            EvidenceStatus.UNAVAILABLE: "no result recorded",
        }[status]
        ledger.add_gate(key, status, f"executor gate '{key}' {summary}", source="executor")

    for key in sorted(plan.required_metric_keys):
        crit = criteria.get(key)
        present = key in measurements and measurements.get(key) is not None
        value = measurements.get(key)
        if crit is not None:
            verdict = crit.evaluate(measurements)      # True | False | None(missing)
            if verdict is None:
                status, acceptable = EvidenceStatus.UNAVAILABLE, None
            else:
                status = EvidenceStatus.PASS if verdict else EvidenceStatus.FAIL
                acceptable = bool(verdict)
            label = getattr(crit, "label", key)
            summary = f"{label}: measured {value!r}" if present else f"{label}: not measured"
        elif present and not _is_finite_number(value):
            # a present but NON-FINITE value (NaN / ±Infinity) is not a real measurement - never
            # record it as a usable pass, even without a declared threshold.
            status, acceptable, summary = (EvidenceStatus.UNAVAILABLE, None,
                                           f"{key}={value!r} is non-finite - not a usable measurement")
        elif present:
            # A required metric with a value but no declared threshold: present and usable, but with
            # no acceptable/unacceptable judgement to make.
            status, acceptable, summary = EvidenceStatus.PASS, None, f"{key}={value!r} (no threshold)"
        else:
            status, acceptable, summary = EvidenceStatus.UNAVAILABLE, None, f"{key}: not measured"
        # The criterion knows whether it is gating or advisory; pass that through rather
        # than letting the reviewer treat every required metric as a blocker.
        ledger.add_metric(key, value, acceptable, status, summary, source="criteria",
                          gating=bool(getattr(crit, "gating", True)) if crit is not None
                          else True)


__all__ = ["collect_deterministic_evidence"]
