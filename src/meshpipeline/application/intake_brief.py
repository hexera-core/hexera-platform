# Responsibility: Build the requirements summary the user approves, and the checks it asserts.
# Boundaries: presentation of a decided intent; it decides nothing.
from __future__ import annotations

from typing import Any, Final

# The check vocabulary. Closed on purpose: a check is a statement the
# application can stand behind, not a free-form label.
PASS: Final = "pass"        # noqa: S105 - a check verdict, not a credential
FAIL: Final = "fail"
UNKNOWN: Final = "unknown"          # not evaluable from what was finalized
STATUSES: Final[frozenset[str]] = frozenset({PASS, FAIL, UNKNOWN})

# Session attributes that are authorization or accountability internals. Named
# here so the exclusion is a declaration, not an accident of which fields the
# builder below happens to read.
NEVER_EXPOSED: Final[tuple[str, ...]] = (
    "intake_gate",          # admission preview token + confirmed engine selection
    "llm_metadata",         # per-call provider accounting
    "messages",             # the conversation log; the UI already has its own turns
    "owner_id",
    "intake_events",        # canonical accountability records
)


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, (list, dict)):
        return value or None
    return value


def _patches(session: Any) -> list[dict[str, str]] | None:
    raw = getattr(session, "intake_patches", None) or []
    out: list[dict[str, str]] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        role = str(p.get("type") or p.get("role") or "").strip()
        if name:
            out.append({"name": name, "role": role})
    return out or None


def build_checks(brief: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, status: str, detail: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"{status!r} is not a check status")
        checks.append({"id": cid, "label": label, "status": status,
                       "detail": detail, "stage": "intake",
                       "sequence": len(checks) + 1})

    purpose = brief.get("purpose")
    engine = brief.get("engine")
    input_kind = brief.get("input_kind")
    dimensionality = brief.get("dimensionality")
    patches = brief.get("boundary_assignments") or []

    # 1. the requirement text and the reviewer's criteria both exist
    has_req = bool(brief.get("requirements_text"))
    has_brief = bool(brief.get("review_brief_text"))
    add("requirements_captured",
        "Requirements captured and acceptance criteria written",
        PASS if (has_req and has_brief) else FAIL,
        "" if (has_req and has_brief) else
        "the submission is missing its requirement summary or its reviewer criteria")

    # 2. engine x purpose x input admissibility - the real compatibility gate
    if engine and purpose and input_kind:
        try:
            from meshpipeline.agents.intake.validation import check_engine_compatibility
            r = check_engine_compatibility(str(engine), str(purpose), str(input_kind))
            ok = r.get("result") == "supported"
            add("engine_compatible",
                f"Engine {engine} supports {purpose} from {input_kind}",
                PASS if ok else FAIL, str(r.get("explanation") or ""))
        except Exception:
            # A gate that cannot run is not a gate that passed.
            add("engine_compatible", "Engine, purpose and input are compatible",
                UNKNOWN, "the compatibility gate could not be evaluated")
    else:
        add("engine_compatible", "Engine, purpose and input are compatible",
            UNKNOWN, "engine, purpose or input kind was never settled")

    # 3. boundary roles are the ones this purpose admits
    if purpose and patches:
        try:
            from meshpipeline.engines.purposes import PURPOSES, purpose_keys
            if purpose in set(purpose_keys()):
                allowed = set(PURPOSES[purpose].boundary_roles)
                bad = sorted({p["role"] for p in patches if p.get("role") not in allowed})
                add("boundary_roles_valid",
                    f"{len(patches)} boundary assignment(s) valid for {purpose}",
                    PASS if not bad else FAIL,
                    "" if not bad else f"roles not admitted by {purpose}: {', '.join(bad)}")
            else:
                add("boundary_roles_valid", "Boundary roles are valid for the purpose",
                    UNKNOWN, f"{purpose} is not a known purpose")
        except Exception:
            add("boundary_roles_valid", "Boundary roles are valid for the purpose",
                UNKNOWN, "the boundary vocabulary could not be resolved")
    elif purpose:
        # A purpose that needs no patches is not a failure; say so rather than pass.
        add("boundary_roles_valid", "Boundary roles are valid for the purpose",
            UNKNOWN, "no boundary assignments were declared")

    # 4. dimensionality is one the system models
    if dimensionality:
        try:
            from meshpipeline.pipeline.enums import Dimensionality
            ok = dimensionality in {d.value for d in Dimensionality}
            add("dimensionality_known", f"Dimensionality {dimensionality} is supported",
                PASS if ok else FAIL,
                "" if ok else f"{dimensionality} is not a modelled dimensionality")
        except Exception:
            add("dimensionality_known", "Dimensionality is supported", UNKNOWN, "")

    # 5. names are unique - the viewer maps a selection back by name
    names = [p["name"] for p in patches]
    if names:
        dupes = sorted({n for n in names if names.count(n) > 1})
        add("boundary_names_unique", "Boundary names are unique",
            PASS if not dupes else FAIL,
            "" if not dupes else f"duplicated: {', '.join(dupes)}")

    # 6. the brief was structurally admitted. This reports WHETHER authorization
    #    exists, never the token that carries it.
    add("submission_authorized", "Submission authorized for dispatch",
        PASS if brief.get("submitted") else UNKNOWN,
        "" if brief.get("submitted") else "the brief has not been submitted yet")

    return checks


def build_brief(session: Any) -> dict[str, Any] | None:
    if session is None or not _clean(getattr(session, "request_txt", None)):
        return None

    brief: dict[str, Any] = {
        "purpose":              _clean(getattr(session, "purpose", None)),
        "engine":               _clean(getattr(session, "mesh_engine", None)),
        "input_kind":           _clean(getattr(session, "input_kind", None)),
        "dimensionality":       _clean(getattr(session, "dimensionality", None)),
        "mesh_fidelity":        _clean(getattr(session, "requested_mesh_fidelity", None)),
        "label":                _clean(getattr(session, "domain", None)),
        "boundary_assignments": _patches(session),
        "engine_params":        _clean(getattr(session, "engine_params", None)),
        "requirements_text":    _clean(getattr(session, "request_txt", None)),
        "review_brief_text":    _clean(getattr(session, "review_brief_txt", None)),
        "submitted":            bool(getattr(session, "intake_submitted", False)),
    }
    # absent stays absent: a field Intake never settled is not a blank row
    brief = {k: v for k, v in brief.items() if v is not None}
    brief["submitted"] = bool(getattr(session, "intake_submitted", False))
    brief["checks"] = build_checks(brief)
    _add_display_labels(brief)
    return brief


def _add_display_labels(brief: dict[str, Any]) -> None:
    from meshpipeline.engines.purposes import input_kind_label, purpose_label
    from meshpipeline.engines.registry import engine_label

    for key, to_label in (("purpose", purpose_label), ("engine", engine_label),
                          ("input_kind", input_kind_label)):
        raw = brief.get(key)
        if raw:
            brief[f"{key}_label"] = to_label(raw)

    # The tier always applies, so the brief states it even when defaulted rather than omitting the
    # row. Derived from the declared authority, not a second copy of the default rule.
    from meshpipeline.pipeline.enums import (
        DEFAULT_MESH_FIDELITY,
        MeshFidelitySource,
        fidelity_value_text,
    )
    requested = brief.get("mesh_fidelity")
    brief["mesh_fidelity_label"] = fidelity_value_text(
        effective=requested or DEFAULT_MESH_FIDELITY,
        source=MeshFidelitySource.USER if requested else MeshFidelitySource.DEFAULT)
