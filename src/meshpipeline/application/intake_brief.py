# Responsibility: Build the requirements summary the user approves, and the checks it asserts.
# Boundaries: presentation of a decided intent; it decides nothing.
from __future__ import annotations

import logging
import re
from typing import Any, Final

logger = logging.getLogger(__name__)

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


#: Text that is the system's own bookkeeping, never a sentence for the user: a parameter key, a
#: field name, an instruction to the intake model. A check's detail carrying any of it is kept off
#: the card (in the check's "internal" field and the log) - the card says only what a person reads.
_INTERNAL = re.compile(r"engine_params|intake_params|missing required param|ask the user|param '\w+'")


def user_detail(detail: str) -> tuple[str, str]:
    """(what the card shows, what stays internal) for a check's detail."""
    text = str(detail or "").strip()
    if text and _INTERNAL.search(text):
        return "", text
    return text, ""


def build_checks(brief: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, status: str, detail: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"{status!r} is not a check status")
        shown, internal = user_detail(detail)
        row = {"id": cid, "label": label, "status": status,
               "detail": shown, "stage": "intake",
               "sequence": len(checks) + 1}
        if internal:
            logger.info("intake brief: check %s detail kept off the card: %s", cid, internal[:300])
            row["internal"] = internal
        checks.append(row)

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
            if brief.get("check_mode") == "thermal":
                # a thermal model's sheet speaks in words, not in the registry's keys
                from meshpipeline.engines.registry import engine_label
                add("engine_compatible",
                    f"{engine_label(str(engine))} meshes this thermal model for a conjugate "
                    "heat-transfer solve", PASS if ok else FAIL, "" if ok else str(r.get("explanation") or ""))
            else:
                # a supported engine needs no explanation on the card (the admission's message is
                # about what is still open, which the intake settles - FEA sets its element order)
                # in words, never the registry's keys ("snappy", "external_cfd", "solid-body")
                from meshpipeline.engines.purposes import purpose_label
                from meshpipeline.engines.registry import engine_label
                add("engine_compatible",
                    f"{engine_label(str(engine))} can mesh this part for {purpose_label(str(purpose))}",
                    PASS if ok else FAIL, "" if ok else str(r.get("explanation") or ""))
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
                from meshpipeline.engines.purposes import purpose_label
                add("boundary_roles_valid",
                    f"{len(patches)} boundary assignment(s) valid for {purpose_label(str(purpose))}",
                    PASS if not bad else FAIL,
                    "" if not bad else f"roles not admitted by {purpose}: {', '.join(bad)}")
            else:
                add("boundary_roles_valid", "Boundary roles are valid for the purpose",
                    UNKNOWN, f"{purpose} is not a known purpose")
        except Exception:
            add("boundary_roles_valid", "Boundary roles are valid for the purpose",
                UNKNOWN, "the boundary vocabulary could not be resolved")
    elif purpose and _is_fea(purpose):
        # AN FEA PART declares no boundaries on purpose: supports and loads are the user's to set in
        # their own solver, on faces the deck names one by one
        add("boundary_roles_valid", "Supports and loads: set in your own solver (every face is named)", PASS)
    elif purpose and brief.get("check_mode") == "thermal":
        # A WHOLE THERMAL MODEL declares no boundaries on purpose: the air box's sides are the
        # file's, open to the room unless the file puts a fan, a vent or a wall there
        add("boundary_roles_valid", "The air box's sides are kept as the file has them", PASS)
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


def _is_fea(purpose: Any) -> bool:
    from meshpipeline.engines import fea_review
    return bool(fea_review.is_fea(purpose))


def build_brief(session: Any, *, part_matches: dict | None = None) -> dict[str, Any] | None:
    """The request card. `part_matches`: the parts checklist (application/part_matches.view, read off the event
    loop by the caller): "Reading your notes…" while the notes are tied to the parts, then the rows
    "words → parts → size" and the phrases it could not place; left out when the notes name no part."""
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
    # WHY THE BOUNDARIES ARE WHAT THEY ARE, when the uploaded file declares them and the brief
    # still gives them the file's roles (the geometry declaration's reason, read back here)
    from meshpipeline.application.geometry_confirmation import roles_reason_of
    brief["boundaries_reason"] = _clean(roles_reason_of(getattr(session, "messages", None) or [],
                                                        brief.get("boundary_assignments") or []))
    # A WHOLE THERMAL MODEL'S JOB SHEET: the job in an electronics-cooling engineer's words, from
    # the file's own numbers and the detail level - what the user approves
    view = _thermal_view(session)
    if view is not None:
        from meshpipeline.application.thermal_wording import job_sheet
        brief["check_mode"] = "thermal"
        brief["job_sheet"] = _clean(job_sheet(
            view, level=brief.get("mesh_fidelity") or _declared_detail(session),
            engine=brief.get("engine") or "snapgrid"))
    if _is_fea(brief.get("purpose")):
        # an FEA mesh has no detail level (one rules-based plan, PLAN-fea-14): no row for it
        brief["mesh_fidelity"] = None
        # SHSTAGE-5: a part settled as a shell, or as shell + solid, says so on the card (a solid is the default)
        from meshpipeline.application.shell_check import card_elements, card_engine_settings, card_mesh_as
        _declared = _fea_declaration(getattr(session, "messages", None) or [])
        brief["mesh_as"] = _clean(card_mesh_as(_declared))
        # ...and its engine settings true to it: the plates are linear (3-node triangles when SHSTAGE-8 settled
        # them); the element order is the solid's beside them
        if brief["mesh_as"] and brief.get("engine_params"):
            brief["engine_params_label"] = _clean(card_engine_settings(brief["mesh_as"], brief["engine_params"],
                                                                       elements=card_elements(_declared)))
    # THE CELL-LIMIT ANSWER IN WORDS on the card, never the parameter's own ("narrow gaps: within_limit")
    if brief.get("engine_params") and not brief.get("engine_params_label"):
        brief["engine_params_label"] = _clean(_params_words(brief["engine_params"]))
    # absent stays absent: a field Intake never settled is not a blank row
    brief = {k: v for k, v in brief.items() if v is not None}
    brief["submitted"] = bool(getattr(session, "intake_submitted", False))
    # THE USER'S OWN NOTES, as typed (contracts/user_notes): listed on the card under the plan, with
    # the box that adds one. Words only - never where they were captured or when.
    from meshpipeline.contracts.user_notes import clean as _clean_notes
    brief["user_notes"] = [{"id": n["id"], "text": n["text"], "source": n["source"]}
                           for n in _clean_notes(getattr(session, "user_notes", None))]
    # NOTE-4 BEFORE APPROVAL: which parts the notes mean, for the user to see (and fix) before Approve. Every row's
    # quote is the user's own words (cad/part_words reads the notes only), so the notes listed are theirs alone -
    # never a sentence of the intake's summary shown as a note
    if part_matches:
        brief["part_matches"] = part_matches
    brief["checks"] = build_checks(brief)
    _add_display_labels(brief)
    return brief


def _params_words(params: Any) -> str | None:
    """The engine settings as the card says them, when one of them has words of its own (the cell-limit
    answer); None leaves the console's own reading of them."""
    from meshpipeline.engines.gap_sizing import FULL_RULE, NARROW_GAPS_PARAM, WITHIN_LIMIT
    if not isinstance(params, dict) or NARROW_GAPS_PARAM not in params:
        return None
    said = {WITHIN_LIMIT: "Narrow gaps: kept within the cell limit", FULL_RULE: "Narrow gaps: full detail"}
    out = []
    for k, v in params.items():
        if k == NARROW_GAPS_PARAM:
            out.append(said.get(str(v), f"Narrow gaps: {v}"))
        else:
            out.append(f"{str(k).replace('_', ' ')}: {v}")
    return ", ".join(out)


def _fea_declaration(messages: list) -> str:
    """The conversation's latest geometry declaration, "" when there is none."""
    from meshpipeline.application.geometry_confirmation import _is_declaration
    for m in reversed(list(messages)):
        if isinstance(m, dict) and _is_declaration(m):
            return str(m.get("content") or "")
    return ""


def _thermal_view(session: Any) -> dict | None:
    """The thermal view the geometry check read from the file (application/thermal_view), when
    the conversation's declaration is a whole thermal model's; None otherwise, or when the check
    cannot be read - the brief is then the generic one."""
    from meshpipeline.application.geometry_confirmation import thermal_declared

    if not thermal_declared(getattr(session, "messages", None) or []):
        return None
    try:
        from meshpipeline.application.geometry_check import read_check

        view = (read_check(str(getattr(session, "id", ""))) or {}).get("thermal")
    except Exception:  # noqa: BLE001 - a brief without its job sheet is still the brief
        return None
    return view if isinstance(view, dict) and view else None


def _declared_detail(session: Any) -> str:
    """The detail level the user picked on the thermal model's panel, as the declaration says it
    ("mesh_fidelity standard"); "" when it says none."""
    import re

    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK

    for m in reversed(list(getattr(session, "messages", None) or [])):
        if not isinstance(m, dict):
            continue
        text = str(m.get("content") or "")
        if m.get("role") == "assistant" and text.startswith(CONFIRMED_MARK):
            found = re.search(r"\(mesh_fidelity (draft|standard|max)\)", text)
            return found.group(1) if found else ""
    return ""


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
