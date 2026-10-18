# Responsibility: Stop a run before the mesher starts when what was written already decides it will fail, and hand that refusal to the executor.
# Owns: the pre-flight refusal record, the far-field domain check, and the zero-face split of the post-mesh manifest gate.
# Boundaries: it reads what the driver prepared and compares it with what was approved; it authors nothing and changes no request.
# Collaborates with: engines/domain_extent_gate.py (the same verdict, reached before the mesh) and pipeline/executor.py (which reports the record).
"""Fail fast: checks whose answer is known before the mesher runs, and one way to report them.

Job ac1daa3e meshed a car for 26 minutes, twice, then failed on something that was readable from
the case before snappyHexMesh ever started. Two things live here so that cannot recur quietly:

* THE REFUSAL RECORD. A pre-flight that refuses a run writes one `PreflightRefusal` into the
  attempt's workspace - the only thing that crosses from the builder to the executor - naming
  the gate it stands in for, the cause (contracts/failure_cause.py) and the facts. The executor
  reports it as that gate with that cause, so the user reads what was wrong ("the space around the
  body is not the size you asked for: downstream is 0.5 reference lengths where you asked for 5"),
  and the retry policy skips what a retry cannot change. Any pre-flight may write it; the
  patch-contract launch check being built alongside this (engines/case_contract.py on
  fix/approved-equals-delivered) is the one meant to join it.
* THE DOMAIN CHECK. The post-mesh extent gate measures the far-field box the driver PREPARED, not
  the mesh, so its verdict is known the moment the box is planned. A box it would block is
  re-planned before a mesh is paid for.

A check refuses only what is CERTAIN to fail later. Anything uncertain passes, and the post-mesh
gates still judge it: a pre-flight that refused a mesh that would have worked would be a new
failure, not a fix.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.engines.gates import GateFeedback, refuse

logger = logging.getLogger(__name__)

#: The record a refused pre-flight leaves in the attempt workspace.
PREFLIGHT_RECORD = "preflight_refusal.json"


@dataclass(frozen=True)
class PreflightRefusal:
    gate: str                     # the gate key this stands in for (patch_contract / domain_extent)
    cause: str                    # a contracts.failure_cause.FailureCause value
    builder_text: str             # what the classifier and planner read
    facts: dict = field(default_factory=dict)   # what the user sentence is built from

    def write(self, workspace) -> None:
        try:
            (Path(workspace) / PREFLIGHT_RECORD).write_text(json.dumps({
                "gate": self.gate, "cause": self.cause, "builder_text": self.builder_text,
                "facts": self.facts}, default=str), encoding="utf-8")
        except OSError:
            logger.exception("pre-flight: could not write the refusal record - the executor will "
                             "report the missing mesh instead")

    def as_feedback(self) -> GateFeedback:
        return refuse(self.builder_text, self.cause, **self.facts)


class PreflightStop(Exception):
    """Raised between planning a pass and starting the mesher when a pre-flight refuses."""

    def __init__(self, refusal: PreflightRefusal):
        super().__init__(refusal.builder_text)
        self.refusal = refusal


def read_refusal(workspace) -> PreflightRefusal | None:
    try:
        d = json.loads((Path(workspace) / PREFLIGHT_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("gate") or not d.get("cause"):
        return None
    return PreflightRefusal(gate=str(d["gate"]), cause=str(d["cause"]),
                            builder_text=str(d.get("builder_text") or ""),
                            facts=dict(d.get("facts") or {}))


def clear_refusal(workspace) -> None:
    try:
        (Path(workspace) / PREFLIGHT_RECORD).unlink(missing_ok=True)
    except OSError:
        pass


def check_domain(*, requested, reference_length_m, strict: bool, flow_axis,
                 body_min, body_max, domain_min, domain_max,
                 grounded: bool, symmetry_faces: list | None = None) -> PreflightRefusal | None:
    """The far-field box the driver is about to mesh, judged exactly as the post-mesh domain gate
    will judge it (that gate measures this same prepared box). Refuses what that gate would
    block - a margin under half the request, or a box touching the body - and, when the approval
    is strict, a near miss too. A lenient near miss is not refused: it is delivered with the miss
    stated, so its mesh is not wasted. The faces that sit on the body by design - the floor under
    a grounded body, the symmetry planes of a half model or a slab ([{patch, axis, side}], the
    list the builder records into the manifest) - owe no margin, here as after the mesh."""
    if not isinstance(requested, dict) or not reference_length_m:
        return None
    from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents
    names = "xyz"
    try:
        box = {f"{names[i]}{s}": float(v[i]) for s, v in (("min", domain_min), ("max", domain_max))
               for i in range(3)}
        body = {f"{names[i]}{s}": float(v[i]) for s, v in (("min", body_min), ("max", body_max))
                for i in range(3)}
    except (TypeError, ValueError, IndexError):
        return None
    try:
        v = evaluate_domain_extents(requested, reference_length_m,
                                    {"geometry": {"domain_box": box, "body_box": body}},
                                    flow_axis=flow_axis, grounded=grounded,
                                    symmetry_faces=list(symmetry_faces or []))
    except Exception:  # noqa: BLE001 - a pre-flight that cannot judge refuses nothing
        logger.exception("pre-flight: domain evaluation failed - leaving it to the post-mesh gate")
        return None
    if v.status == "block" or (v.status == "miss" and strict):
        return PreflightRefusal(
            gate="domain_extent", cause=FailureCause.DOMAIN_EXTENT,
            builder_text="[PREFLIGHT] " + v.detail,
            facts={"misses": list(v.misses), "before_meshing": True})
    return None


def zero_face_feedback(text: str, empty: list, workspace) -> GateFeedback:
    """ONE symptom, several causes, told apart only by POSITIVE evidence in the delivered
    boundary - the cause that ends the retries is claimed only when it is proven.

    * A declared patch missing from the mesh while its OpenFOAM spelling IS there was written
      under another name ('car wall' approved, 'car_wall' written, job ac1daa3e): a contract
      mismatch, which no retry changes.
    * Any other zero-face patch - present with no faces, or absent with no other spelling in the
      mesh (cfMesh and createPatch can drop an empty patch altogether) - was lost by the mesher, a
      meshing problem a retry can fix. An absence alone proves nothing about naming.
    * No readable boundary at all means the mesh was not written completely: the mesher stopped,
      and nothing about the patches can be concluded from it."""
    from meshpipeline.engines.contract import foam_spelling
    from meshpipeline.engines.manifest import _patch_face_counts
    present = _patch_face_counts(Path(workspace))
    if not present:
        return refuse(text, FailureCause.ENGINE_CRASHED)
    renamed = {n: foam_spelling(n) for n in empty
               if n not in present and foam_spelling(n) != n and foam_spelling(n) in present}
    if renamed:
        return refuse(text, FailureCause.CONTRACT_MISMATCH, missing=sorted(renamed),
                      present=sorted(present), renamed=renamed)
    return refuse(text, FailureCause.PATCH_NOT_CAPTURED, patches=list(empty))


__all__ = ["PREFLIGHT_RECORD", "PreflightRefusal", "PreflightStop", "check_domain",
           "clear_refusal", "read_refusal", "zero_face_feedback"]
