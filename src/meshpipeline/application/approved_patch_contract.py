# Responsibility: Hold a run to the boundary set the user approved, and refuse it if that set changed.
# Owns: patch normalisation, the fingerprint, and the admission re-check performed before execution.
# Boundaries: it compares an approved intent with a recomputed one.
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

PATCH_CONTRACT_SCHEMA_VERSION = 1


class PatchContractError(ValueError):
    pass


def normalize_patches(raw: object) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise PatchContractError(f"patch set must be a list, got {type(raw).__name__}")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PatchContractError(f"patch entry must be a dict, got {type(entry).__name__}")
        name = (entry.get("name") or "").strip()
        role = (entry.get("role") or entry.get("type") or "").strip()
        if not name:
            raise PatchContractError("a patch entry has no name - malformed contract")
        if name in seen:
            raise PatchContractError(f"duplicate patch name {name!r} - a boundary is declared twice")
        seen.add(name)
        out.append((name, role))
    return tuple(sorted(out, key=lambda p: (p[0], p[1])))


def compute_fingerprint(required: bool, patches: tuple[tuple[str, str], ...]) -> str:
    payload = {"required": bool(required),
               "patches": [[n, r] for (n, r) in patches]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ApprovedPatchContract:
    schema_version: int
    required: bool
    patches: tuple[tuple[str, str], ...]
    fingerprint: str

    @staticmethod
    def build(raw_patches: object, *, required: bool | None = None) -> ApprovedPatchContract:
        patches = normalize_patches(raw_patches)
        req = bool(patches) if required is None else bool(required)
        if req and not patches:
            raise PatchContractError("a required patch contract must declare at least one patch")
        return ApprovedPatchContract(
            schema_version=PATCH_CONTRACT_SCHEMA_VERSION, required=req, patches=patches,
            fingerprint=compute_fingerprint(req, patches))

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "required": self.required,
                "patches": [{"name": n, "role": r} for (n, r) in self.patches],
                "fingerprint": self.fingerprint}

    @staticmethod
    def from_dict(d: object) -> ApprovedPatchContract:
        if not isinstance(d, dict):
            raise PatchContractError(f"contract must be a dict, got {type(d).__name__}")
        v = d.get("schema_version")
        if v != PATCH_CONTRACT_SCHEMA_VERSION:
            raise PatchContractError(
                f"approved_patch_contract schema_version {v!r} is not supported by this code "
                f"({PATCH_CONTRACT_SCHEMA_VERSION}) - refusing to run it")
        required = bool(d.get("required", False))
        patches = normalize_patches(d.get("patches"))
        if required and not patches:
            raise PatchContractError("contract marked required but declares no patches")
        recomputed = compute_fingerprint(required, patches)
        stored = d.get("fingerprint")
        if stored != recomputed:
            raise PatchContractError(
                "approved_patch_contract fingerprint does not match its patches - the contract was "
                "altered between approval and execution (tampered patches or a stale fingerprint)")
        return ApprovedPatchContract(schema_version=PATCH_CONTRACT_SCHEMA_VERSION, required=required,
                                     patches=patches, fingerprint=recomputed)

    def matches(self, execution_patches: object) -> bool:
        try:
            return normalize_patches(execution_patches) == self.patches
        except PatchContractError:
            return False

    def assert_satisfied_by(self, execution_patches: object, *, where: str = "admission") -> None:
        exec_norm = normalize_patches(execution_patches)  # malformed exec patches raise here
        if not self.required:
            if exec_norm:
                raise PatchContractError(
                    f"{where}: approved snapshot declared NO patch contract, but the run carries "
                    f"{len(exec_norm)} patch(es) - the execution set does not match the approval")
            return
        if exec_norm != self.patches:
            approved = {n for (n, _) in self.patches}
            got = {n for (n, _) in exec_norm}
            missing = sorted(approved - got)
            extra = sorted(got - approved)
            role_changed = sorted(
                n for (n, r) in exec_norm
                if n in approved and (n, r) not in self.patches)
            raise PatchContractError(
                f"{where}: the execution patch set does not match the approved contract exactly "
                f"(missing={missing}, added={extra}, role_changed={role_changed}). The declared "
                "boundaries were altered between approval and execution - refusing to run.")


# #
# RUN-BOUNDARY ADMISSION - does this run still match what the user approved?
# Extracted from application/pipeline_run._run_async. Two gates and one rejection lived inline as
# ~78 lines: the exact-equality patch-contract check, the COMPLETE approved-intent fingerprint
# re-check, and the durable refusal that follows either.
# They run BEFORE any expensive work - before make_pipeline_state, the graph, the builder, the
# executor or any native process. The dispatch contract already verified this at reconstruction;
# this is the second, run-boundary gate that produces a truthful terminal DB status if a payload
# ever slips past it. The engine gate remains defence in depth: it proves the delivered MESH
# realised the contract, not merely that the REQUEST did.
# A rejection here is an INTERNAL consistency failure - the user approved a valid, boundary-declaring
# config and our pipeline dropped it. It is blameless to the user, and never a native-execution or
# user-input failure.
# #


def check_admission(req) -> str | None:
    if req.approved_patch_contract is not None:
        try:
            contract = ApprovedPatchContract.from_dict(req.approved_patch_contract)
            contract.assert_satisfied_by(req.intake_patches or [], where="graph admission")
        except PatchContractError as exc:
            return str(exc)

    # the COMPLETE approved-intent re-check. The run's own run-determining fields must still
    # canonicalize to the fingerprint carried from the approval snapshot - an altered
    # engine/purpose/input_kind/dimensionality/patch-set/engine_params is refused before any work.
    if req.approved_intent_fingerprint:
        from meshpipeline.application.dispatch_contract import recompute_intent_fingerprint

        payload = {
            "mesh_engine": req.mesh_engine, "purpose": req.purpose,
            "input_kind": req.input_kind, "dimensionality": req.dimensionality,
            "intake_patches": req.intake_patches or [], "engine_params": req.engine_params or {},
            # the approved FIDELITY (request_txt) and the approved GEOMETRY are part of the
            # complete intent. Geometry is re-hashed FROM THE FILE, so substituting different bytes
            # behind the same path is refused - a path check alone would not catch it.
            "requested_mesh_fidelity": req.requested_mesh_fidelity,
            "effective_mesh_fidelity": req.effective_mesh_fidelity,
            "mesh_fidelity_source": req.mesh_fidelity_source,
            "fidelity_policy_version": req.fidelity_policy_version,
            "request_txt": req.request_txt or "",
            # the typed domain request is intent v5 - omitting it here refused every correctly
            # approved run that captured extents (found live on the heat-sink replay, run 8)
            "requested_extents": getattr(req, "requested_extents", None),
            "reference_length_m": getattr(req, "reference_length_m", None),
            "flow_axis": getattr(req, "flow_axis", None),
            "requirements_strict": bool(getattr(req, "requirements_strict", False)),
            "geometry_source": (req.geometry_source.to_payload() if req.geometry_source else None),
            "geometry_interpretation": (req.geometry_interpretation.to_payload()
                                        if req.geometry_interpretation else None),
        }
        if recompute_intent_fingerprint(payload) != req.approved_intent_fingerprint:
            return ("graph admission: the run's approved intent (engine/purpose/input_kind/"
                    "dimensionality/patches/engine_params/mesh_fidelity/geometry) does not match "
                    "its approved fingerprint - it was altered between approval and execution.")
    return None


async def refuse_admission(session_factory, reason: str, *, job_id: str, snapshot_id: str,
                           job_repo, jlog, publish) -> dict:
    from meshpipeline.errors import (
        FailureClass,
        failed_reason_for,
        record_dead_letter,
        user_message_for,
    )
    from meshpipeline.persistence.job_state import TransitionResult
    from meshpipeline.persistence.models import FailedReason, JobStatus

    jlog.error("Patch-contract admission REJECT - %s (job_id=%s)", reason, job_id)
    try:
        async with session_factory() as db:
            if await job_repo.transition(db, uuid.UUID(job_id), JobStatus.failed) == TransitionResult.applied:
                row = await job_repo.get_internal(db, uuid.UUID(job_id))
                if row:
                    try:
                        row.failed_reason = FailedReason(failed_reason_for(FailureClass.INTERNAL))
                    except (ValueError, AttributeError):
                        row.failed_reason = FailedReason.unhandled
            await db.commit()
    except Exception as exc:                       # noqa: BLE001 - the refusal must still record
        jlog.warning("patch-contract admission: could not mark job failed: %s", exc)
    record_dead_letter(job_id, FailureClass.INTERNAL, "intake",
                       f"approved snapshot {snapshot_id}: {reason}")
    try:
        publish(user_message_for(FailureClass.INTERNAL))
    except Exception:                              # noqa: BLE001
        pass
    return {"job_id": job_id, "status": "failed", "reason": "patch_contract_mismatch"}
