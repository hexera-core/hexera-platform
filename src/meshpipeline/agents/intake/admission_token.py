# Responsibility: Fingerprint what the user approved, so a run cannot change meaning after approval.
# Owns: the canonical approved-intent payload, its fingerprint, geometry identity, and verification at submit.
# Boundaries: it proves equality between what was approved and what is about to run.
from __future__ import annotations

import hashlib
import json
import time
import uuid

TOKEN_TTL_S = 900  # 15 minutes - long enough for gather→submit→confirm, short enough that a stale
#                    preview cannot authorize a much-later submission.

SELECTED = "selected"          # a single-engine preview of the user's chosen engine
RECOMMENDATION = "recommendation"  # read-only multi-engine exploration - never authorizes a submit


def canonical_payload(engine, purpose, input_kind, dimensionality, patches, engine_params) -> dict:
    pats = sorted(
        ({"name": (p.get("name") or "").strip(),
          "role": (p.get("role") or p.get("type") or "").strip()}
         for p in (patches or []) if isinstance(p, dict)),
        key=lambda q: (q["name"], q["role"]))
    ep = engine_params if isinstance(engine_params, dict) else {}
    return {
        "engine": (engine or "").strip().lower(),
        "purpose": (purpose or "").strip(),
        "input_kind": (input_kind or "").strip(),
        "dimensionality": (dimensionality or "").strip(),
        "patches": pats,
        "engine_params": {k: ep[k] for k in sorted(ep)},
    }


def fingerprint(canonical: dict) -> str:
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


# the COMPLETE approved intent (a strict superset of the admission canonical above)
# `canonical_payload` binds what the ADMISSION PREVIEW can decide - the engine-admissibility fields
# the preview tool actually receives. It deliberately stays that shape: the preview token is verified
# by comparing the submitted canonical to the previewed one, and `preview_selected_admission` is not
# given request_txt, so widening the preview canonical would make every submission fail token
# verification.
# The APPROVED INTENT is broader and is computed at APPROVAL, where the full submitted requirements
# exist. It binds everything the user approved that can change native execution or required delivery:
#   * MESH DETAIL PREFERENCE - four explicit fields, not one. `requested_mesh_fidelity` is the
#     user's own choice (null when they never expressed one); `effective_mesh_fidelity` is the
#     deterministic operational tier; `mesh_fidelity_source` says which of the two produced it; and
#     `fidelity_policy_version` pins the default/alias policy, so changing the default tier later
#     cannot silently re-interpret an already approved job. Recording all four is what stops a
#     system default being presented as a user selection. The canonical vocabulary is exactly
#     draft|standard|max - `high` is an input alias canonicalized to `max` before approval.
#   * GEOMETRY - a STRUCTURED identity: a required content sha256, an optional immutable revision id
#     and the byte size. The checksum is never omitted because a revision id exists: a database
#     identifier without content immutability cannot prove the bytes are the approved bytes. A local
#     absolute PATH is never part of the identity, so relocating identical bytes is not drift.
#   * REQUIRED OUTPUTS + ARTIFACT POLICY VERSION - deterministically derived from the engine by the
#     ONE artifact-policy authority, never user-selected, and version-pinned so an artifact-policy
#     change between approval and execution cannot silently alter what must be delivered.
# `approved_request_digest` is carried ALONGSIDE these as PROVENANCE, never as fidelity: it detects
# that the approved WORDING changed between approval and execution (the brief the Reviewer is judged
# against). It never substitutes for a typed field, and it never infers a preference.
# v2: single `mesh_fidelity` (draft|standard|high|max), flat geometry string.
# v3: the four fidelity fields (draft|standard|max), structured geometry, artifact_policy_version.
# v4: `port_declaration` - the internal-flow port sizes/locations/interchangeability the user
#     approved, so the binding the engines perform is provably the binding that was approved.
#     It binds here, in the approval-time canonical, NEVER in `canonical_payload` (widening the
#     preview canonical would fail every submission's token verification - see above).
# v5: the typed DOMAIN REQUEST - `requested_extents` (per-direction multiples), the
#     `reference_length_m` ruler they multiply, and `requirements_strict`. The domain gate
#     measures against these approved numbers; prose is provenance, never a measurement. A
#     pre-v5 approval executes as strict=true - an approval's blocking contract is never
#     silently relaxed by a later default.
APPROVED_INTENT_SCHEMA_VERSION = 5

#: Version of the geometry-identity structure, so a later change to how geometry is identified is
#: detectable rather than silently altering an approved binding.
GEOMETRY_IDENTITY_SCHEMA_VERSION = 1


def approved_request_digest(request_txt) -> str:
    import unicodedata
    raw = str(request_txt or "")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    norm = " ".join(unicodedata.normalize("NFC", raw).split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest() if norm else ""


def geometry_identity(source_ref, *, revision_id: str | None = None,
                      interpretation_ref=None) -> dict:
    ident = {"identity_schema_version": GEOMETRY_IDENTITY_SCHEMA_VERSION,
             "source_id": "", "sha256": "", "size_bytes": 0, "suffix_hint": "",
             "revision_id": str(revision_id or "").strip()}
    if source_ref is not None:
        ident.update(source_ref.identity())
    if interpretation_ref is not None:
        # Physical meaning is part of what was approved: the same bytes read as millimetres and
        # as metres are different approvals, because they mesh to different physical objects.
        ident.update(interpretation_ref.identity())
    return ident


def required_output_classes(engine) -> list[str]:
    from meshpipeline.application.artifact_policy import required_output_classes as _req
    return _req(engine)


def approved_intent_canonical(*, engine, purpose, input_kind, dimensionality, patches,
                              engine_params, request_txt, source_ref=None,
                              interpretation_ref=None,
                              requested_mesh_fidelity=None,
                              effective_mesh_fidelity=None,
                              mesh_fidelity_source=None,
                              fidelity_policy_version=None,
                              geometry: dict | None = None,
                              geometry_revision_id: str | None = None,
                              requested_extents: dict | None = None,
                              reference_length_m: float | None = None,
                              requirements_strict: bool = False) -> dict:
    from meshpipeline.application.artifact_policy import ARTIFACT_POLICY_VERSION
    from meshpipeline.pipeline.enums import (
        FIDELITY_POLICY_VERSION,
        assert_fidelity_consistent,
        resolve_mesh_fidelity,
    )
    base = canonical_payload(engine, purpose, input_kind, dimensionality, patches, engine_params)

    if effective_mesh_fidelity is None or mesh_fidelity_source is None:
        req, eff, src = resolve_mesh_fidelity(requested_mesh_fidelity)
        req_v = None if req is None else req.value
        eff_v, src_v = eff.value, src.value
    else:
        from meshpipeline.pipeline.enums import canonical_mesh_fidelity
        _r = canonical_mesh_fidelity(requested_mesh_fidelity)
        req_v = None if _r is None else _r.value
        eff_v = str(effective_mesh_fidelity).strip().lower()
        src_v = str(mesh_fidelity_source).strip().lower()
    pol_v = str(fidelity_policy_version or FIDELITY_POLICY_VERSION)
    assert_fidelity_consistent(requested=req_v, effective=eff_v, source=src_v, policy_version=pol_v)

    return {
        "schema_version": APPROVED_INTENT_SCHEMA_VERSION,
        **base,
        "requested_mesh_fidelity": req_v,
        "effective_mesh_fidelity": eff_v,
        "mesh_fidelity_source": src_v,
        "fidelity_policy_version": pol_v,
        "required_outputs": required_output_classes(base["engine"]),
        "artifact_policy_version": ARTIFACT_POLICY_VERSION,
        "approved_request_digest": approved_request_digest(request_txt),
        "requested_extents": (
            {k: float(requested_extents[k]) for k in sorted(requested_extents)
             if requested_extents[k] is not None}
            if isinstance(requested_extents, dict) and any(
                x is not None for x in requested_extents.values()) else None),
        "reference_length_m": (None if reference_length_m is None
                               else float(reference_length_m)),
        "requirements_strict": bool(requirements_strict),
        "port_declaration": sorted(
            ({"name": (p.get("name") or "").strip(),
              "role": (p.get("role") or p.get("type") or "").strip(),
              "diameter_mm": p.get("diameter_mm"), "area_mm2": p.get("area_mm2"),
              "width_mm": p.get("width_mm"), "height_mm": p.get("height_mm"),
              "near_mm": list(p["near_mm"]) if isinstance(p.get("near_mm"), (list, tuple))
              else None,
              "interchangeable_with": sorted(str(x).strip()
                                             for x in (p.get("interchangeable_with") or []))}
             for p in (patches or []) if isinstance(p, dict)
             and (p.get("role") or p.get("type") or "").strip() in ("inlet", "outlet")),
            key=lambda q: q["name"]),
        "geometry": geometry if geometry is not None else geometry_identity(
            source_ref, revision_id=geometry_revision_id,
            interpretation_ref=interpretation_ref),
    }


def approved_intent_fingerprint(**kw) -> str:
    return fingerprint(approved_intent_canonical(**kw))



def verify_approved_intent(stored_canonical: dict, stored_fingerprint: str) -> bool:
    v = (stored_canonical or {}).get("schema_version")
    if v != APPROVED_INTENT_SCHEMA_VERSION:
        return False
    return fingerprint(stored_canonical) == stored_fingerprint


def execution_fidelity_for(stored_canonical: dict) -> str:
    from meshpipeline.pipeline.enums import DEFAULT_MESH_FIDELITY, canonical_mesh_fidelity
    c = stored_canonical or {}
    eff = c.get("effective_mesh_fidelity")
    if eff:
        return (canonical_mesh_fidelity(eff) or DEFAULT_MESH_FIDELITY).value
    return DEFAULT_MESH_FIDELITY.value


def revision_of(messages) -> str:
    users = [str(m.get("content", "")) for m in (messages or [])
             if isinstance(m, dict) and m.get("role") == "user"]
    return hashlib.sha256("\x00".join(users).encode()).hexdigest()[:16]


def issue(*, session_id: str, owner_id: str, revision: str, canonical: dict,
          verdict: str, mode: str, selection_id: str) -> dict:
    return {
        "token": uuid.uuid4().hex,
        "session": session_id, "owner": owner_id, "revision": revision,
        "selection_id": selection_id,
        "fingerprint": fingerprint(canonical), "canonical": canonical,
        "verdict": verdict, "mode": mode, "expires_at": time.time() + TOKEN_TTL_S,
    }


def _selection_binding(pending: dict, selection: dict | None) -> tuple[bool, str]:
    from meshpipeline.agents.intake import engine_selection as es

    if es.state_of(selection) != es.CONFIRMED or not selection:
        return False, ("the engine selection backing that preview is no longer confirmed - the user "
                       "must select and confirm an engine again")
    if pending.get("selection_id") != selection.get("id"):
        return False, ("the engine selection changed after that preview - re-run "
                       "preview_selected_admission for the confirmed engine")
    if (pending.get("canonical") or {}).get("engine") != selection.get("engine"):
        return False, "that preview is for a different engine than the confirmed selection"
    return True, ""


def verify_for_submit(pending: dict | None, token: str, *, session_id: str, owner_id: str,
                      revision: str, submitted_canonical: dict,
                      selection: dict | None) -> tuple[bool, str]:
    if not pending or not token:
        return False, ("no admission preview authorizes this - call preview_selected_admission with "
                       "the FULL declared payload (including every patch) and submit its exact result")
    if pending.get("token") != token:
        return False, "that preview token does not match the current preview"
    if pending.get("session") != session_id or pending.get("owner") != owner_id:
        return False, "that preview token belongs to a different session or owner"
    if time.time() > pending.get("expires_at", 0):
        return False, "the preview expired - re-run preview_admission"
    if pending.get("mode") != SELECTED:
        return False, "that token did not come from a confirmed-engine admission preview"
    if pending.get("verdict") != "supported":
        return False, "the previewed requirements were not admissible - resolve that first"
    if pending.get("revision") != revision:
        return False, ("the requirements changed after the preview - re-run "
                       "preview_selected_admission on the current message")
    if pending.get("fingerprint") != fingerprint(submitted_canonical):
        return False, ("the submission differs from what was previewed - re-run "
                       "preview_selected_admission on the EXACT payload you are submitting "
                       "(patches included)")
    return _selection_binding(pending, selection)


def verify_for_confirm(pending: dict | None, token: str, *,
                       selection: dict | None) -> tuple[bool, str]:
    if not pending or not token:
        return False, "no approved snapshot to confirm - submit the requirements first"
    if pending.get("token") != token:
        return False, "that snapshot is stale (the requirements changed) - re-confirm the current one"
    if pending.get("verdict") != "supported" or pending.get("mode") != SELECTED:
        return False, "the current snapshot is not an admissible, submitted selection"
    return _selection_binding(pending, selection)


#: The ASK shown with a submitted brief. The requirements themselves are stated by the
#: structured brief beside it, from the same persisted session and with the checks the
#: application ran - restating them here too would let two renderings of one decision disagree.
CONFIRM_REQUIREMENTS_ASK = "Please confirm the requirements above before I mesh anything."
