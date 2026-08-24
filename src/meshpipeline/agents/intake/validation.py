# Responsibility: Validate a submitted set of requirements before it can become a run.
# Owns: the admission preview, semantic-loss detection, engine-compatibility checks and submission normalisation.
# Boundaries: it refuses an unusable submission rather than repairing it.
from __future__ import annotations

from dataclasses import dataclass

from meshpipeline.agents.intake import vocabulary as _vocab
from meshpipeline.engines.purposes import INPUT_KINDS as _INPUT_KINDS
from meshpipeline.engines.purposes import PURPOSES as _PURPOSES
from meshpipeline.engines.purposes import is_compatible as _is_compatible
from meshpipeline.engines.purposes import purpose_keys as _purpose_keys
from meshpipeline.pipeline.enums import (
    Dimensionality,
    MeshFidelity,
)

_DIMENSIONALITY_VALUES = [d.value for d in Dimensionality]
#: What a SUBMISSION may carry: the three canonical tiers.
_MESH_FIDELITY_INPUT_VALUES = [f.value for f in MeshFidelity]
_PURPOSE_KEYS = list(_purpose_keys())


def _all_boundary_roles() -> list[str]:
    roles: list[str] = []
    for p in _PURPOSES.values():
        roles.extend(r for r in p.boundary_roles if r not in roles)
    return roles


_ALL_BOUNDARY_ROLES = _all_boundary_roles()


# preview_admission verdicts.
ADMIT_SUPPORTED = "supported"
ADMIT_IMPOSSIBLE = "impossible"
ADMIT_INCOMPLETE = "incomplete"
ADMIT_MALFORMED = "malformed"

# The admission codes that are HARD physical impossibilities (no gathering fixes them; the user
# must revise a declared field). engine_param_invalid and the missing_* patch-structure codes are
# "incomplete" - the user simply has not declared enough yet - not impossible.
_HARD_IMPOSSIBLE_CODES = frozenset({
    "purpose_incompatible", "input_kind_incompatible", "dimensionality_unsupported",
    "symmetry_unsupported", "multiple_wall_patches_unsupported", "geometry_unsuitable",
})

# The two lines closing every impossible message: they preserve user intent, name NO alternative
# engine, and gate recommendations behind an explicit request.
_PRESERVED_LINE = "Nothing was changed - every value you declared is preserved exactly."
_REVISE_QUESTION = (
    "Which would you like to revise: the engine, the purpose, the input kind, the dimensionality, "
    "or the patches? Ask me to list the compatible engines and I will."
)


def _normalise_patches(patches) -> list[dict]:
    out = []
    for p in (patches or []):
        if isinstance(p, dict):
            out.append({"name": (p.get("name") or "").strip(),
                        "role": (p.get("role") or p.get("type") or "").strip()})
    return out


def _impossible_message(hard: list) -> str:
    # One finding per line, and the assurance and the question each on their own. Joining every
    # rejection with a space produced one run-on block in which two independent constraints - a
    # geometry mismatch and a patch-structure limit, say - were indistinguishable, so a reader
    # could not tell how many decisions they were being asked to make. The chat renderer turns
    # "- " into a list, so N findings arrive as N things.
    findings = [r.message.strip() for r in hard if (r.message or "").strip()]
    body = findings[0] if len(findings) == 1 else "\n".join(f"- {f}" for f in findings)
    return f"{body}\n\n{_PRESERVED_LINE}\n\n{_REVISE_QUESTION}"


def preview_admission(engine: str, purpose: str, input_kind: str, dimensionality: str | None = None,
                      patches=None, engine_params=None, geometry_facts=None) -> dict:
    # ONE boundary between the keys the system routes on and the words a person reads. Every
    # message below is written with keys, so translating here means no verdict can reach a user in
    # the system's own vocabulary. The declared values keep their keys - callers route on them.
    result = _admission(engine, purpose, input_kind, dimensionality, patches, engine_params,
                        geometry_facts)
    for field in ("safe_user_message", "capability_reason"):
        if result.get(field):
            result[field] = _vocab.humanize(result[field])
    return result


def _admission(engine: str, purpose: str, input_kind: str, dimensionality: str | None = None,
               patches=None, engine_params=None, geometry_facts=None) -> dict:
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    from meshpipeline.engines.registry import engine_names, get_spec

    _eng = (engine or "").strip().lower()
    _pur = (purpose or "").strip()
    _ik = (input_kind or "").strip()
    _dim = (dimensionality or "").strip() or None
    _patches = _normalise_patches(patches)
    _eparams = engine_params if isinstance(engine_params, dict) else {}
    declared = {"engine": _eng, "purpose": _pur, "input_kind": _ik, "dimensionality": _dim,
                "patches": _patches, "engine_params": _eparams}

    missing = [n for n, v in (("engine", _eng), ("purpose", _pur), ("input_kind", _ik)) if not v]
    if missing:
        return {"verdict": ADMIT_INCOMPLETE, "selected_engine": _eng or None,
                "missing_fields": missing, "preserved_declared_values": declared,
                "safe_user_message": f"To check this I still need: {', '.join(missing)}. "
                                     "Which is it? I will not guess a value."}

    bad = []
    if _eng not in engine_names():
        bad.append("engine")
    if _pur not in _PURPOSE_KEYS:
        bad.append("purpose")
    if _ik not in _INPUT_KINDS:
        bad.append("input_kind")
    if _dim and _dim not in _DIMENSIONALITY_VALUES:
        bad.append("dimensionality")
    if bad:
        return {"verdict": ADMIT_MALFORMED, "selected_engine": _eng,
                "conflicting_field_names": bad, "preserved_declared_values": declared,
                "safe_user_message": f"That is not a recognised value for: {', '.join(bad)}."}

    ev = AdmissionEvidence(
        engine=_eng, purpose=_pur, input_kind=_ik, dimensionality=_dim,
        patches=tuple(PatchSummary(name=p["name"], type=p["role"]) for p in _patches),
        engine_params=_eparams, surface_analysis=geometry_facts)
    rejections = get_spec(_eng).admit(ev)
    hard = [r for r in rejections if r.code in _HARD_IMPOSSIBLE_CODES]
    if hard:
        r0 = hard[0]
        field_of = {"purpose_incompatible": ["purpose", "engine"],
                    "input_kind_incompatible": ["input_kind", "engine"],
                    "dimensionality_unsupported": ["dimensionality", "engine"],
                    "symmetry_unsupported": ["patches", "engine"],
                    "multiple_wall_patches_unsupported": ["patches", "engine"],
                    "geometry_unsuitable": ["geometry", "engine"]}
        return {"verdict": ADMIT_IMPOSSIBLE, "blocking_rule_code": r0.code,
                "blocking_rule_codes": sorted({r.code for r in hard}),
                "selected_engine": _eng, "conflicting_field_names": field_of.get(r0.code, ["engine"]),
                "preserved_declared_values": declared,
                # The engine-authored WHY on its own (no revise coda) - this is what an engine
                # COMPARISON quotes; the coda only belongs on a selected-engine refusal.
                "capability_reason": " ".join(r.message for r in hard).strip(),
                "safe_user_message": _impossible_message(hard)}
    # capability + structure OK; anything left (params / missing patches) is still-to-gather.
    gather = [r for r in rejections if r.code in ("engine_param_invalid",) or r.field == "patches"]
    if gather:
        return {"verdict": ADMIT_INCOMPLETE, "selected_engine": _eng,
                "missing_fields": sorted({r.field or "patches" for r in gather}),
                "preserved_declared_values": declared,
                "safe_user_message": " ".join(r.message for r in gather)}
    return {"verdict": ADMIT_SUPPORTED, "selected_engine": _eng,
            "preserved_declared_values": declared,
            "safe_user_message": f"{_eng} can produce the {_pur} mesh from the declared setup."}


# The PROTECTED declared fields whose silent change is a semantic-loss violation (section 6).
_PROTECTED_FIELDS = ("engine", "purpose", "input_kind", "dimensionality")


def detect_semantic_loss(declared: dict, proposed: dict) -> list[str]:
    losses: list[str] = []
    for f in _PROTECTED_FIELDS:
        d, p = (declared.get(f) or ""), (proposed.get(f) or "")
        if d and p and str(d).strip().lower() != str(p).strip().lower():
            losses.append(f"{f} changed from {d!r} to {p!r}")

    d_patches = {q["name"]: q["role"] for q in _normalise_patches(declared.get("patches")) if q["name"]}
    p_patches = {q["name"]: q["role"] for q in _normalise_patches(proposed.get("patches")) if q["name"]}
    if d_patches:
        if len(p_patches) < len(d_patches):
            losses.append(f"patch count dropped from {len(d_patches)} to {len(p_patches)} "
                          f"(declared: {', '.join(sorted(d_patches))})")
        for name, role in d_patches.items():
            if name not in p_patches:
                losses.append(f"declared patch {name!r} was dropped or merged away")
            elif p_patches[name] and role and p_patches[name] != role:
                losses.append(f"patch {name!r} role changed from {role!r} to {p_patches[name]!r}")

    d_ep, p_ep = (declared.get("engine_params") or {}), (proposed.get("engine_params") or {})
    for k in d_ep:
        if k not in p_ep:
            losses.append(f"declared engine parameter {k!r} was removed")
    return losses


# The brief's compatibility gate row: the full preview, flattened to the verdict and the one
# sentence that row shows. Params still outstanding are not incompatibility.
def check_engine_compatibility(engine: str, purpose: str, input_kind: str) -> dict:
    r = preview_admission(engine, purpose, input_kind)
    _map = {ADMIT_SUPPORTED: "supported", ADMIT_IMPOSSIBLE: "impossible",
            ADMIT_INCOMPLETE: "insufficient_information", ADMIT_MALFORMED: "malformed"}
    result = _map[r["verdict"]]
    if r["verdict"] == ADMIT_INCOMPLETE and "engine_params" in (r.get("missing_fields") or []):
        result = "supported"   # capability is fine; only params remain - the old shape called that OK
    return {"result": result, "engine": r.get("selected_engine"),
            "explanation": r["safe_user_message"]}


def _admission_message(rejection, engine: str, purpose: str, dim: str) -> str:
    from meshpipeline.engines.registry import engine_names, get_spec
    msg = rejection.message
    if rejection.code == "purpose_incompatible":
        any_server = any(_is_compatible(get_spec(n), purpose) for n in engine_names())
        msg += (" To proceed, change the engine, the purpose, or the input geometry - or ask me to "
                "recommend an engine for this and I will."
                if any_server else f" No implemented engine can mesh {_vocab.to_display(_vocab.PURPOSE, purpose)} yet.")
    elif rejection.code == "dimensionality_unsupported":
        any_supporter = any(
            (get_spec(n).input_contract and dim in get_spec(n).input_contract.dimensionalities)
            for n in engine_names())
        msg += (" To proceed, change the engine or the dimensionality - or ask me to recommend one "
                "that meshes this and I will."
                if any_supporter else f" No implemented engine meshes {dim} yet.")
    return msg


# Internal-flow port declarations: the binder downstream matches these names to the openings
# it MEASURES, by size and location. Anything it would have to guess about is refused HERE,
# where a refusal costs one question in chat instead of a meshing run. The 5/3 separation is
# the binder's tolerance band (a +/-25%% area test cannot tell closer sizes apart under
# normal manufacturing drift - a counterbored 40 measures like a reduced-bore 50).
_PORT_NAME_RE = None  # initialised lazily below to keep module import light
_RESERVED_PATCH_NAMES = frozenset({"outer", "FoamFile", "farfield"})
_MIN_DECLARED_AREA_SEPARATION = 5.0 / 3.0


def _declared_area_mm2(p: dict) -> float | None:
    d, ar, w, h = (p.get("diameter_mm"), p.get("area_mm2"), p.get("width_mm"),
                   p.get("height_mm"))
    try:
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            return 3.141592653589793 * (float(d) / 2.0) ** 2
        if isinstance(ar, (int, float)) and not isinstance(ar, bool):
            return float(ar)
        if (isinstance(w, (int, float)) and isinstance(h, (int, float))
                and not isinstance(w, bool) and not isinstance(h, bool)):
            return float(w) * float(h)
    except Exception:  # noqa: BLE001
        return None
    return None


def _validate_internal_ports(patches: list) -> list[str]:
    import math
    import re as _re

    global _PORT_NAME_RE
    if _PORT_NAME_RE is None:
        _PORT_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

    errors: list[str] = []
    entries = [p for p in patches if isinstance(p, dict)]
    all_names = {(p.get("name") or "").strip() for p in entries}

    for i, p in enumerate(entries):
        nm = (p.get("name") or "").strip()
        if nm and not _PORT_NAME_RE.match(nm):
            errors.append(
                f"patches[{i}].name {nm!r} cannot be built verbatim into a mesh - names must "
                "start with a letter and contain only letters, digits and underscores; ask the "
                "user for a mesh-safe spelling (e.g. inlet_1)")
        if nm in _RESERVED_PATCH_NAMES:
            errors.append(
                f"patches[{i}].name {nm!r} is reserved by the meshing engines - ask the user "
                "for a different name")

    ports = [(i, p) for i, p in enumerate(entries)
             if (p.get("type") or "").strip() in ("inlet", "outlet")]
    sized: list[tuple[str, str, float, bool]] = []  # (name, role, area, has_location)
    for i, p in ports:
        nm = (p.get("name") or "").strip() or f"patches[{i}]"
        forms = sum((isinstance(p.get("diameter_mm"), (int, float)),
                     isinstance(p.get("area_mm2"), (int, float)),
                     isinstance(p.get("width_mm"), (int, float))
                     or isinstance(p.get("height_mm"), (int, float))))
        if forms > 1:
            errors.append(
                f"port {nm!r} states more than one size form - keep exactly one of "
                "diameter_mm, area_mm2, or width_mm+height_mm")
            continue
        near = p.get("near_mm")
        has_near = near is not None
        if has_near and (not isinstance(near, (list, tuple)) or len(near) != 3
                         or any(isinstance(c, bool) or not isinstance(c, (int, float))
                                or not math.isfinite(float(c)) for c in near)):
            errors.append(f"port {nm!r}: near_mm must be [x, y, z] in millimetres")
            has_near = False
        for other in (p.get("interchangeable_with") or []):
            if str(other).strip() not in all_names:
                errors.append(
                    f"port {nm!r} claims interchangeability with {other!r}, which is not a "
                    "declared patch")
        raw_sizes = [p.get(k) for k in ('diameter_mm', 'area_mm2', 'width_mm', 'height_mm')]
        if any(isinstance(x, (int, float)) and not isinstance(x, bool) and float(x) <= 0
               for x in raw_sizes):
            errors.append(f"port {nm!r}: the stated size must be positive")
            continue
        area = _declared_area_mm2(p)
        if area is None and not has_near:
            errors.append(
                f"port {nm!r} has no stated size and no location - ask the user for its "
                "approximate diameter (or area, or width x height), or roughly where it is "
                "on the part, so the mesh can bind the name to the right opening")
            continue
        if area is not None:
            sized.append((nm, (p.get("type") or "").strip(), area, has_near))

    def _mutual(a_name: str, b_name: str) -> bool:
        by = {(p.get("name") or "").strip(): p for _i, p in ports}
        pa, pb = by.get(a_name) or {}, by.get(b_name) or {}
        return (b_name in [str(x).strip() for x in (pa.get("interchangeable_with") or [])]
                and a_name in [str(x).strip() for x in (pb.get("interchangeable_with") or [])])

    for x in range(len(sized)):
        for y in range(x + 1, len(sized)):
            an, ar_, aa, aloc = sized[x]
            bn, br_, ba, bloc = sized[y]
            lo, hi = min(aa, ba), max(aa, ba)
            if lo <= 0 or hi / lo >= _MIN_DECLARED_AREA_SEPARATION:
                continue
            if ar_ == br_ and _mutual(an, bn):
                continue                      # confirmed interchangeable twins bind either way
            if aloc and bloc:
                continue                      # locations disambiguate whatever the sizes say
            if ar_ == br_:
                errors.append(
                    f"ports {an!r} and {bn!r} state the same size with no way to tell them "
                    "apart - ask the user whether they are interchangeable (carry no distinct "
                    "streams), or for each one's rough location (near_mm)")
            else:
                da = round(2.0 * (aa / 3.141592653589793) ** 0.5, 1)
                db = round(2.0 * (ba / 3.141592653589793) ** 0.5, 1)
                errors.append(
                    f"ports {an!r} (~{da} mm) and {bn!r} (~{db} mm) are too close in size to "
                    "match reliably against the measured openings - ask the user for each "
                    "one's rough location (near_mm)")
    return errors


_EXTENT_DIRECTIONS = ("upstream", "downstream", "lateral", "vertical")


def _validate_domain_declaration(args: dict) -> list[str]:
    """The typed domain request: extents in reference lengths + the ruler in metres + the
    strictness bit. The domain gate compares against THESE numbers - so they must be sound
    here, where fixing them costs one question instead of a meshing run."""
    errors: list[str] = []
    ext = args.get("requested_extents")
    ref = args.get("reference_length_m")
    strict = args.get("requirements_strict")
    if strict is not None and not isinstance(strict, bool):
        errors.append("requirements_strict must be true or false")
    if ref is not None:
        if isinstance(ref, bool) or not isinstance(ref, (int, float)) or not ref > 0:
            errors.append("reference_length_m must be a positive number of metres")
    if ext is not None:
        if not isinstance(ext, dict):
            errors.append("requested_extents must be an object of direction -> multiple")
            return errors
        for k, val in ext.items():
            if k not in _EXTENT_DIRECTIONS:
                errors.append(
                    f"requested_extents key {k!r} is not a direction - use "
                    f"{', '.join(_EXTENT_DIRECTIONS)}")
            elif val is not None and (isinstance(val, bool)
                                      or not isinstance(val, (int, float)) or not val > 0):
                errors.append(f"requested_extents.{k} must be a positive number "
                              "(the user's stated multiple); omit directions they never stated")
        if ref is None:
            errors.append(
                "requested_extents needs reference_length_m - extents are multiples of a "
                "length, and without the ruler they cannot be measured; ask the user what "
                "one 'body length' is in their units")
    return errors


def validate_submission(args: dict) -> list[str]:
    _val_errors: list[str] = []

    # The mesh-detail preference is OPTIONAL. Omission, null, "" and whitespace all mean "the user
    # expressed no preference" and must never block an otherwise valid submission - the system then
    # applies its own default and labels it as such. A NON-EMPTY value outside the accepted input
    # vocabulary is still an error: a tier nobody can act on must not reach approval.
    _fid = (args.get("mesh_fidelity") or "").strip()
    if _fid and _fid.lower() not in _MESH_FIDELITY_INPUT_VALUES:
        _val_errors.append(
            f"mesh_fidelity {_fid!r} is not one of {_MESH_FIDELITY_INPUT_VALUES} "
            "(it is optional - omit it when the user expressed no preference)")

    if not (args.get("domain") or "").strip():
        _val_errors.append("domain must be a non-empty 2-5 word label for the geometry and simulation type")
    _req_txt = (args.get("request_txt") or "").strip()
    _rbf_txt = (args.get("review_brief_txt") or "").strip()
    if not _req_txt:
        _val_errors.append("request_txt must be a non-empty summary of all simulation requirements (4-10 sentences)")
    elif len(_req_txt) < 80:
        _val_errors.append("request_txt is too short - provide a complete summary (4-10 sentences) covering geometry, simulation type, all confirmed parameters, and mesh requirements")
    if not _rbf_txt:
        _val_errors.append("review_brief_txt must be non-empty acceptance criteria for the reviewer (5-10 sentences)")
    elif len(_rbf_txt) < 60:
        _val_errors.append("review_brief_txt is too short - include domain sizing, patch structure, cell quality targets, and surface integrity requirements (5-10 sentences)")

    # Boundary vocabulary + flow-ness derive from the declared PURPOSE (engines-as-
    # tools), NEVER the engine identity or a shared "all flow roles" bucket. The
    # schema admits every purpose's roles; here we validate patches against the EXACT
    # set for the submitted purpose (external_cfd = wall/farfield/symmetry/empty;
    # internal_cfd = wall/inlet/outlet/symmetry/empty; structural = fixed/load/…).
    from meshpipeline.engines.registry import get_spec as _get_spec0
    _purpose = (args.get("purpose") or "").strip()
    _roles = (set(_PURPOSES[_purpose].boundary_roles)
              if _purpose in _PURPOSE_KEYS else set(_ALL_BOUNDARY_ROLES))
    _is_flow = _purpose in _PURPOSE_KEYS and _PURPOSES[_purpose].requires_mesh_kind == "fluid-volume"

    _patches = args.get("patches")
    _types_seen: set[str] = set()
    if not isinstance(_patches, list) or (_is_flow and not _patches):
        _val_errors.append(
            "patches must be a list of {name, type} entries - for a flow mesh, at minimum one "
            "wall patch and an inflow/outflow definition; use the names the user actually said"
        )
        _patches = _patches if isinstance(_patches, list) else []
    else:
        _valid_types = _roles
        _patch_names_seen: set[str] = set()
        for _i, _p in enumerate(_patches):
            if not isinstance(_p, dict):
                _val_errors.append(f"patches[{_i}] must be an object with name and type fields")
                continue
            _pn = (_p.get("name") or "").strip()
            _pt = (_p.get("type") or "").strip()
            if not _pn:
                _val_errors.append(f"patches[{_i}].name must be non-empty")
            elif _pn in _patch_names_seen:
                _val_errors.append(f"patches[{_i}].name {_pn!r} duplicates an earlier patch - names must be unique")
            else:
                _patch_names_seen.add(_pn)
            if _pt not in _valid_types:
                _val_errors.append(
                    f"patches[{_i}].type {_pt!r} is invalid - must be one of "
                    f"{sorted(_valid_types)}"
                )
            else:
                _types_seen.add(_pt)
        # NOTE: symmetry-plane producibility and flow patch STRUCTURE (wall + inflow/outflow)
        # are ENGINE ADMISSIBILITY, not payload shape - they moved to EngineSpec.admit()
        # (the single admission path), evaluated once below with the rest of the declared
        # rules. This block validates only that patches are WELL-FORMED for the purpose.

    if _purpose == "internal_cfd" and isinstance(_patches, list):
        _val_errors.extend(_validate_internal_ports(_patches))

    _val_errors.extend(_validate_domain_declaration(args))

    # FAIL-CLOSED CAPTURE: the model must not leave far-field statements as prose only. The
    # legacy prose parser is the detector - if it finds extents in request_txt that the typed
    # fields do not carry, the submission bounces until they are captured. (A model that skims
    # the optional fields is exactly how a typed request silently degrades to prose guessing.)
    if _purpose == "external_cfd" and not args.get("requested_extents"):
        from meshpipeline.engines.domain_extent_gate import parse_requested_extents
        _prose = parse_requested_extents(str(args.get("request_txt") or ""))
        if _prose:
            _val_errors.append(
                "the request text states far-field extents ("
                + ", ".join(f"{k} {v:g}" for k, v in _prose.items())
                + ") but requested_extents was not filled in - capture them as "
                "requested_extents plus reference_length_m (the metre length they multiply); "
                "ask the user for the reference length if they never gave a number")

    _dim = args.get("dimensionality")
    if _dim not in set(Dimensionality):
        _val_errors.append(f"dimensionality must be one of: {', '.join(_DIMENSIONALITY_VALUES)}")


    _eng = (args.get("mesh_engine") or "").strip().lower()
    from meshpipeline.engines.registry import engine_names as _engine_names
    if _eng not in _engine_names():
        _val_errors.append(
            "mesh_engine must be a concrete, user-confirmed engine "
            f"({', '.join(_engine_names())}) - ask which toolchain the "
            "user works in, or propose one and get their explicit OK"
        )
    _esrc = (args.get("engine_source") or "").strip().lower()
    if _esrc not in ("user_direct", "suggested_confirmed"):
        _val_errors.append(
            "engine_source must be 'user_direct' or 'suggested_confirmed'"
        )

    # PURPOSE + submitted geometry kind (engines-as-tools): the user declares WHAT
    # they will DO with the mesh and WHAT the geometry represents. `_purpose` (read
    # above, where it drives the role vocabulary) is enum-checked here; both feed the
    # (engine × purpose × input) compatibility gate - a USER declaration, never
    # inferred from the geometry or filename.
    if _purpose not in _PURPOSE_KEYS:
        _val_errors.append(f"purpose must be one of: {_vocab.choices_text(_vocab.PURPOSE)}")
    _input_kind = (args.get("input_kind") or "").strip()
    if _input_kind not in _INPUT_KINDS:
        _val_errors.append(f"input_kind must be one of: {_vocab.choices_text(_vocab.INPUT_KIND)}")

    # engine_params SHAPE is payload validity (stays here); its per-engine VALIDITY moved
    # into spec.admit() below with the rest of the engine-admissibility rules.
    _eparams = args.get("engine_params")
    if not isinstance(_eparams, dict):
        _eparams = {}
        _val_errors.append(
            "engine_params must be an object with the chosen "
            "engine's declared parameters"
        )

 # ENGINE ADMISSIBILITY (declared phase) - the SINGLE engine-owned check
    # Once the payload PARSES (valid engine/purpose/input_kind/dimensionality), ask the engine
    # whether it can service the declared request: capability (engine × purpose × input),
    # dimensionality, symmetry-plane producibility, flow patch structure, 2D/3D empty-patch,
    # and engine_params validity. This is the SAME EngineSpec.admit() the pre-builder
    # geometry-admission node calls with measured evidence - one path, two evidence phases.
    # Intake enriches capability/dimensionality rejections with the cross-roster suggestion
    # (which engine COULD serve it) - roster knowledge is intake's, not a single engine's.
    if (_eng in _engine_names() and _purpose in _PURPOSE_KEYS
            and _input_kind in _INPUT_KINDS and _dim in set(Dimensionality)):
        from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
        _ev = AdmissionEvidence(
            engine=_eng, purpose=_purpose, input_kind=_input_kind, dimensionality=_dim,
            patches=tuple(
                PatchSummary(name=(p.get("name") or "").strip(),
                             type=(p.get("type") or "").strip())
                for p in _patches if isinstance(p, dict)),
            engine_params=_eparams,
        )
        for _r in _get_spec0(_eng).admit(_ev):
            _val_errors.append(_admission_message(_r, _eng, _purpose, str(_dim)))
    # (2D/3D empty-patch consistency also moved into EngineSpec.admit() - see above.)
    return _val_errors


# #
# NORMALISATION - turning a VALIDATED submission into the values the graph state carries.
# Extracted from agents/intake/agent.py::node_intake. Validation above answers "may this
# submission proceed?"; this answers "what exactly did the user declare?" - the two are different
# questions over the same vocabulary, which is why they live together and not in the node.
# Every rule here exists because a specific wrong value could otherwise reach approval:
#   * boundary roles are PURPOSE-owned, so a role belonging to another workflow is dropped;
#   * an engine outside the catalog becomes "" rather than being passed through;
#   * an omitted mesh fidelity is not a user selection - it resolves to the default, LABELLED
#     as the default, so nothing downstream can mistake it for a choice the user made.
# #


@dataclass(frozen=True)
class SubmittedRequirements:

    domain: str
    request_txt: str
    review_brief_txt: str
    intake_patches: list
    dimensionality: str
    purpose: str
    input_kind: str
    mesh_engine: str
    engine_source: str
    engine_params: dict
    requested_mesh_fidelity: str | None
    effective_mesh_fidelity: str
    mesh_fidelity_source: str


def normalise_submission(args: dict) -> SubmittedRequirements:
    from meshpipeline.engines.purposes import PURPOSES
    from meshpipeline.engines.registry import engine_names
    from meshpipeline.pipeline.enums import resolve_mesh_fidelity

    purpose = (args.get("purpose") or "").strip()
    # Region vocabulary derives from the declared PURPOSE (engines-as-tools): a role that belongs
    # to another workflow is not a boundary this run can carry.
    allowed_roles = (set(PURPOSES[purpose].boundary_roles) if purpose in PURPOSES
                     else set(_ALL_BOUNDARY_ROLES))
    patches: list[dict] = []
    for p in (args.get("patches", []) or []):
        if not isinstance(p, dict):
            continue
        name, role = (p.get("name") or "").strip(), (p.get("type") or "").strip()
        if name and role in allowed_roles:
            patches.append({"name": name, "type": role})

    # The mesh-detail preference, resolved ONCE by the shared policy: an omitted or blank value is
    # not a user selection, so it becomes requested=None + effective=STANDARD + source=default. A
    # structured `high` alias is canonicalized before anything is approved.
    requested, effective, source = resolve_mesh_fidelity(args.get("mesh_fidelity"))

    engine = (args.get("mesh_engine") or "").strip().lower()
    if engine not in engine_names():
        engine = ""                    # cannot happen post-validation; defensive only
    params = args.get("engine_params")
    if not isinstance(params, dict):
        params = {}                    # cannot happen post-validation; defensive only

    return SubmittedRequirements(
        domain=(args.get("domain") or "").strip(),
        request_txt=(args.get("request_txt") or "").strip(),
        review_brief_txt=(args.get("review_brief_txt") or "").strip(),
        intake_patches=patches,
        dimensionality=(args.get("dimensionality") or "").strip(),
        purpose=purpose,
        input_kind=(args.get("input_kind") or "").strip(),
        mesh_engine=engine,
        engine_source=(args.get("engine_source") or "").strip().lower(),
        engine_params=params,
        requested_mesh_fidelity=None if requested is None else requested.value,
        effective_mesh_fidelity=effective.value,
        mesh_fidelity_source=source.value,
    )
