# Responsibility: Define exactly what may be handed to a pipeline run, and validate it before dispatch.
# Owns: the accepted and required key sets, intent-fingerprint recomputation, and construction of the run arguments.
# Boundaries: the shape of the hand-off; it launches nothing.
# Collaborates with: pipeline/data_contract.py, which declares what each field means.
from __future__ import annotations

import inspect

# Bump when the payload's MEANING changes (a new required field, a field whose interpretation
# shifts). Adding an optional field with a safe default does not require a bump.
# v1: carried `patch_contract_required` (a boolean - "at least one patch survived"). It could not
#     prove the patch SET was exactly the approved one.
# v2: carries `approved_patch_contract` (the typed, fingerprinted ApprovedPatchContract). An
#     approved-snapshot job MUST carry it; a v1 approved-snapshot payload is now AMBIGUOUS (it has
#     no typed contract to verify exactness against) and is rejected - see _check_patch_contract.
DISPATCH_SCHEMA_VERSION = 2

# Envelope keys travel with the payload but are NOT run-entry arguments.
_ENVELOPE_KEYS = frozenset({"schema_version"})

# `approved_intent_fingerprint` is a RUN-ENTRY param (like approved_patch_contract), so it is
# carried through reconstruction into the JobRequest and re-checked at graph admission - not stripped.
# It is the sha256 of the COMPLETE approved execution intent (engine, purpose, input_kind,
# dimensionality, patches, engine_params - the same canonical form the approval snapshot was
# fingerprinted on). Set once by the dispatch producer from the durable approved snapshot, and
# re-verified against the payload's own run-determining fields, so an approved run whose intent
# drifted from the approval fails closed - not just its patch subset.

# Fields a DIRECT (non-approved) dispatch legitimately omits, defaulted on read. A direct/internal
# job carries no approval provenance and no typed patch contract; an approved job carries both.
_OPTIONAL_DISPATCH_DEFAULTS: dict[str, object] = {
    "approved_snapshot_id": "",
    "approved_patch_contract": None,
}


class DispatchContractError(ValueError):
    pass


def _run_signature() -> inspect.Signature:
    # The run entry is read from the neutral dispatch_types seam (pipeline_run registers it there at
    # import) - NOT imported from pipeline_run, which would form a cycle (pipeline_run imports this
    # module). It is the registered entry, not a module attribute: rebinding `pipeline_run.run_pipeline`
    # (as tests do to observe kwargs) must never redefine the contract producers are validated against.
    from meshpipeline.application.dispatch_types import run_entry
    return inspect.signature(run_entry())


def accepted_keys() -> frozenset[str]:
    return frozenset(_run_signature().parameters)


def required_keys() -> frozenset[str]:
    return frozenset(n for n, p in _run_signature().parameters.items()
                     if p.default is inspect.Parameter.empty
                     and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD))


def validate(payload: dict, *, where: str = "dispatch") -> None:
    if not isinstance(payload, dict):
        raise DispatchContractError(f"{where}: payload must be a dict, got {type(payload).__name__}")

    # ONE accepted form. A payload with no version, or an older one, used to be read as version 0
    # and upgraded on the way through. That existed for rows written before this contract, and
    # this is a pre-release build: every persisted payload is written by `build()` below, which
    # always stamps the current version. `final_result.from_dict` refuses on the same rule.
    version = payload.get("schema_version")
    if version != DISPATCH_SCHEMA_VERSION:
        raise DispatchContractError(
            f"{where}: payload schema_version {version!r} is not {DISPATCH_SCHEMA_VERSION} - "
            "refusing to run it. Dispatch payloads are built by dispatch_contract.build(), which "
            "stamps the current version; there is no upgrade path from an earlier form.")

    fields = set(payload) - _ENVELOPE_KEYS
    unknown = sorted(fields - accepted_keys())
    if unknown:
        raise DispatchContractError(
            f"{where}: payload has key(s) the run entry does not accept: {unknown}. "
            "Add them to run_pipeline (and the contract) or stop producing them - "
            "run_pipeline takes no **kwargs on purpose.")

    # A DIRECT dispatch legitimately omits the approval-provenance fields, which is what
    # _OPTIONAL_DISPATCH_DEFAULTS names. Nothing here tolerates an older payload: an unknown key
    # and a schema_version this code does not understand are both refused above.
    missing = sorted(required_keys() - fields - set(_OPTIONAL_DISPATCH_DEFAULTS))
    if missing:
        raise DispatchContractError(f"{where}: payload is missing required key(s): {missing}")

    _check_geometry_pair(payload, where)
    _check_patch_contract(payload, where)
    _check_intent_fingerprint(payload, where)


def _check_geometry_pair(payload: dict, where: str) -> None:
    source = payload.get("geometry_source")
    interpretation = payload.get("geometry_interpretation")
    if source and not interpretation:
        raise DispatchContractError(
            f"{where}: payload carries geometry_source but no geometry_interpretation - the "
            "physical scale of those bytes is unknown, so this run cannot be meshed. A geometry "
            "source and its interpretation are one approved fact and must be dispatched together.")
    if interpretation and not source:
        raise DispatchContractError(
            f"{where}: payload carries geometry_interpretation but no geometry_source - an "
            "interpretation describes specific bytes and is meaningless without them.")
    if not source:
        return

    # PRESENT is not the same as WELL-FORMED. Both refs parse their own payloads and refuse an
    # unsupported unit, an unsanctioned scale factor or a missing field - so parsing here rejects
    # a half-written snapshot at the producer instead of at materialisation, and the pairing below
    # can be checked at all.
    from meshpipeline.contracts.geometry_source import (
        GeometryInterpretationRef,
        GeometrySourceError,
    )
    try:
        source_ref = source_ref_of(payload)
        interpretation_ref = GeometryInterpretationRef.from_payload(interpretation)
    except GeometrySourceError as exc:
        raise DispatchContractError(f"{where}: {exc}") from exc

    # The two must describe the SAME bytes. Materialisation verifies this against the durable
    # rows; doing it here as well means a producer that paired the wrong two snapshots is caught
    # where it can still be identified, rather than as a data-integrity fault much later.
    if interpretation_ref.geometry_source_id != source_ref.source_id:
        raise DispatchContractError(
            f"{where}: the geometry interpretation describes source "
            f"{interpretation_ref.geometry_source_id} but the payload carries source "
            f"{source_ref.source_id} - meshing these bytes at another file's scale.")


def recompute_intent_fingerprint(payload: dict) -> str:
    from meshpipeline.agents.intake.admission_token import approved_intent_fingerprint
    return approved_intent_fingerprint(
        engine=payload.get("mesh_engine", ""), purpose=payload.get("purpose", ""),
        input_kind=payload.get("input_kind", ""), dimensionality=payload.get("dimensionality", ""),
        patches=payload.get("intake_patches") or [], engine_params=payload.get("engine_params") or {},
        # The four fidelity fields are recomputed from the payload AS CARRIED - never re-resolved
        # from prose, and the consistency rules reject a trio the resolver could not have produced.
        requested_mesh_fidelity=payload.get("requested_mesh_fidelity"),
        effective_mesh_fidelity=payload.get("effective_mesh_fidelity"),
        mesh_fidelity_source=payload.get("mesh_fidelity_source"),
        fidelity_policy_version=payload.get("fidelity_policy_version"),
        request_txt=payload.get("request_txt", ""),
        source_ref=source_ref_of(payload))


def source_ref_of(payload: dict):
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    snapshot = (payload or {}).get("geometry_source")
    if not snapshot:
        return None
    return GeometrySourceRef.from_payload(snapshot)


def _check_intent_fingerprint(payload: dict, where: str) -> None:
    stored = (payload.get("approved_intent_fingerprint") or "").strip() or None
    approved_snapshot_id = (payload.get("approved_snapshot_id") or "").strip()

    if stored is None:
        if approved_snapshot_id:
            raise DispatchContractError(
                f"{where}: this run comes from an approved snapshot ({approved_snapshot_id}) but "
                "carries no approved_intent_fingerprint - its complete approved intent cannot be "
                "verified. Refusing to run.")
        return                                          # genuine direct/internal dispatch
    recomputed = recompute_intent_fingerprint(payload)
    if recomputed != stored:
        raise DispatchContractError(
            f"{where}: the run's approved intent does not match its fingerprint - engine/purpose/"
            "input_kind/dimensionality/patches/engine_params/mesh_fidelity/geometry were altered "
            "between approval and execution. Refusing to run.")


def _check_patch_contract(payload: dict, where: str) -> None:
    from meshpipeline.application.approved_patch_contract import (
        ApprovedPatchContract,
        PatchContractError,
    )

    raw_contract = payload.get("approved_patch_contract")
    exec_patches = payload.get("intake_patches") or []
    approved_snapshot_id = (payload.get("approved_snapshot_id") or "").strip()

    if raw_contract is not None:
        try:
            contract = ApprovedPatchContract.from_dict(raw_contract)
            contract.assert_satisfied_by(exec_patches, where=where)
        except PatchContractError as exc:
            raise DispatchContractError(f"{where}: {exc}") from exc
        return

    # No typed contract present.
    if approved_snapshot_id:
        # An approved-snapshot job MUST carry a typed contract, or its exact patch set is unverifiable.
        raise DispatchContractError(
            f"{where}: this run comes from an approved snapshot ({approved_snapshot_id}) but carries "
            "no typed approved_patch_contract - its exact boundary set cannot be verified. Refusing "
            "to run (an approved job must ship its fingerprinted patch contract).")

    # Genuine direct/internal dispatch (no approved snapshot): the explicit no-contract path.


def build(**fields) -> dict:
    payload = {"schema_version": DISPATCH_SCHEMA_VERSION, **fields}
    validate(payload, where="dispatch build")
    return payload


def to_run_kwargs(payload: dict, *, where: str = "reconstruction") -> dict:
    validate(payload, where=where)
    kwargs = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}
    for key, default in _OPTIONAL_DISPATCH_DEFAULTS.items():
        if key not in kwargs and key in accepted_keys():
            kwargs[key] = default
    return kwargs
