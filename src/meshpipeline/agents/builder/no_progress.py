# Responsibility: Detect a retry ladder that has stopped making progress - the same gate
# rejecting for the same reason twice in a row - so the builder can fail truthfully instead of
# spending more attempts re-proving one failure. The combining-wye burned four attempts (and
# four Cloud Run meshes) on a byte-identical zero-faces rejection no re-plan could ever fix.
# Boundaries: pure filesystem + dict work, no model calls. Only DETERMINISTIC classifier
# signatures count - reviewer-driven retries carry no gate and are never stopped here, because
# that iteration is productive (a reviewer FAIL improved layer coverage 45.8% -> 68.8% on the
# same budget and delivered on attempt 3).
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_RECORD = ".last_failure.json"


def failure_signature(state: Mapping[str, Any]) -> dict | None:
    """The deterministic identity of the failure that caused this retry, or None.

    Gate and section come from the classifier verbatim; the summary carries the gate's own
    diagnostic text, which is deterministic for executor gates. No gate -> no signature: a
    reviewer-feedback retry must never be stopped by this detector.
    """
    cr = (state or {}).get("classifier_result") or {}
    # The classifier writes the deterministic gate under "failed_gate" (pipeline/classifier.py);
    # this reader looked up "gate" and so ALWAYS came back empty, silently returning None on
    # every failure - the no-progress stop never fired once, and identical gate rejections ran
    # the full retry ladder (wasted Cloud Run meshes each time). Read the key the writer
    # actually sets; keep "gate" as a fallback in case another path ever populates it.
    gate = cr.get("failed_gate") or cr.get("gate")
    if not gate:
        return None
    return {"gate": str(gate), "section": str(cr.get("section") or ""),
            "summary": str(cr.get("summary") or "")}


def record_failure(workspace: Path, sig: dict | None) -> None:
    """Durably note, in THIS attempt's workspace, the failure that caused it."""
    if sig is None:
        return
    try:
        tmp = Path(workspace) / (_RECORD + ".tmp")
        tmp.write_text(json.dumps(sig))
        tmp.replace(Path(workspace) / _RECORD)
    except Exception:  # noqa: BLE001 - the record is an optimization, never worth failing for
        pass


def repeats_previous(workspace: Path, sig: dict | None) -> bool:
    """True when the sibling attempt was caused by this exact failure - i.e. the previous
    attempt changed nothing about the outcome, and another attempt would re-buy the same
    rejection."""
    if sig is None:
        return False
    m = re.fullmatch(r"attempt_(\d+)", Path(workspace).name)
    if m is None or int(m.group(1)) < 2:
        return False
    prev = Path(workspace).parent / f"attempt_{int(m.group(1)) - 1}" / _RECORD
    try:
        return json.loads(prev.read_text()) == sig
    except Exception:  # noqa: BLE001 - no record, or unreadable: assume progress
        return False
