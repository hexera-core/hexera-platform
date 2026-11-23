# Responsibility: Name the STEP a repair attempt RETURNS to the customer - its own repaired export
#                 or the upload's bytes unchanged (a passthrough) - and the mesh-hints sidecar beside
#                 it, as the one record the terminal delivery and the mesh staging both read.
# Owns: `repair_attempt["returned"]`: {"path", "source", "sha256", "hints_path"}.
# Boundaries: it reads what `cad/repair/conservative.repair_step_file` already wrote and measured;
#             it writes no file, uploads nothing and decides no status. Absent - the meshability
#             pass off, or nothing on disk - it returns {} and the attempt record is what it was.
# Collaborates with: pipeline/repair_attempt.py (attaches it), application/pipeline_run.py and
#                    application/repair_report_delivery.py (deliver it), agents/builder/attempt.py
#                    and engines/gmsh/staged_hints.py (stage the hints for the mesher).
"""THE RETURNED STEP, AND ITS HINTS.

With CAD_REPAIR_MESHABILITY_PASS=live every exit of `repair_step_file` leaves a STEP at the
attempt's destination: the repair's export (`delivery_source` "repaired") or, when nothing was kept,
the upload's bytes copied unchanged ("passthrough"), plus `<step>.mesh-hints.json` when the hints
provider had anything to say. This module turns those measurements into the record two readers
need, so neither re-derives it:

  the terminal delivery hands the STEP back - labelled for what it IS: a passthrough is the
  customer's own file, never "repaired" - and the sidecar beside it;

  the mesh staging applies the sidecar's hints, but only to bytes whose sha256 the sidecar names.
  A passthrough is byte-identical to the upload, so a run that meshes the original after a refusal
  still matches.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

#: `returned["source"]` - the words `conservative.REPAIRED_SOURCE` / `PASSTHROUGH_SOURCE` record.
REPAIRED = "repaired"
PASSTHROUGH = "passthrough"
_SOURCES = (REPAIRED, PASSTHROUGH)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def returned(measurements: Mapping | None, destination: Path | str) -> dict:
    """`{"path", "source", "sha256", "hints_path"}` for the STEP the attempt left at
    `destination`, or {} when it left none worth naming. NEVER RAISES.

    CHECKED ON DISK, not inferred from the words: a passthrough whose copy failed records
    `"none: <why>"` and names nothing, and a sidecar the measurements name but the disk does not
    hold is not offered.
    """
    try:
        m = measurements if isinstance(measurements, Mapping) else {}
        source = str(m.get("delivery_source") or "")
        if source not in _SOURCES:
            return {}
        step = Path(destination)
        if not step.is_file():
            return {}
        hints = str(m.get("hints") or "")
        hints_path = hints if hints and Path(hints).is_file() else ""
        return {"path": str(step), "source": source, "sha256": _sha256(step),
                "hints_path": hints_path}
    except Exception as exc:  # noqa: BLE001 - a record that cannot be made names nothing
        logger.warning("repair_return: the returned STEP could not be named (%s)",
                       type(exc).__name__)
        return {}


def pass_live() -> bool:
    """CAD_REPAIR_MESHABILITY_PASS, failing closed: only the exact `live` (region_loop's own gate)."""
    from meshpipeline.cad.repair.region_loop import meshability_pass_on
    return meshability_pass_on()


def passthrough_without_repair(source: Path | str, destination: Path | str) -> dict:
    """A run that NEVER CALLED the repair (triage found nothing to aim at, or routed it elsewhere)
    still returns a STEP with the pass live: the upload's bytes copied unchanged to `destination`,
    labelled passthrough, with the hints provider asked about them exactly as `repair_step_file`'s
    own passthrough asks (`mesh_hints.hints_for`, no located defect - it probes the part itself and
    writes nothing when it meshes at default). Returns the `returned` record, or {}. NEVER RAISES;
    bounded by the provider's own wall (`mesh_hints.HINTS_WALL_S`, every probe in a child).
    The caller checks `pass_live()` first: off, nothing here runs.
    """
    import shutil

    try:
        src, dest = Path(source), Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("repair_return: the passthrough could not be written (%s)",
                       type(exc).__name__)
        return {}
    hints = "none"
    try:
        from meshpipeline.cad.repair import mesh_hints
        written = mesh_hints.hints_for(dest, ())
        hints = str(written) if written else "none"
    except Exception as exc:  # noqa: BLE001 - a failing provider never costs the STEP
        logger.warning("repair_return: mesh hints failed (%s)", type(exc).__name__)
        hints = f"failed: {type(exc).__name__}"
    return returned({"delivery_source": PASSTHROUGH, "hints": hints}, dest)


def of(state) -> dict:
    """The returned record a run carries, or {}."""
    attempt = (state or {}).get("repair_attempt") if hasattr(state, "get") else None
    record = attempt.get("returned") if isinstance(attempt, Mapping) else None
    return dict(record) if isinstance(record, Mapping) else {}


def hints_path(state) -> str:
    """The sidecar the mesh staging should consider for this run, or "". The staging still checks
    its sha256 against the bytes it meshes; this only says where to look."""
    return str(of(state).get("hints_path") or "")
