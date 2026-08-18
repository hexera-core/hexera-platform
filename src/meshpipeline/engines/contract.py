# Responsibility: Decide whether a job satisfies the engine's declared input contract before any meshing starts.
# Boundaries: it checks the request against the declaration; it neither repairs the request nor inspects geometry.
# Collaborates with: engines/base.py for the contract and pipeline/geometry_admission.py.
from __future__ import annotations

_SPANWISE_TYPES_2D = frozenset({"empty"})

_SPANWISE_EMPTY_KEY = "<spanwise-empty>"


def contract_applicable(intake_patches: list) -> bool:
    return bool(_normalise_patches(intake_patches))


def _collapse_spanwise_empty(patches: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    has_empty = False
    for name, ptype in patches.items():
        if ptype in _SPANWISE_TYPES_2D:
            has_empty = True
            continue
        out[name] = ptype
    if has_empty:
        out[_SPANWISE_EMPTY_KEY] = "empty"
    return out


def _label(name: str) -> str:
    if name == _SPANWISE_EMPTY_KEY:
        return "the spanwise empty patch(es) (front/back - one merged 'frontAndBack' group is accepted)"
    return name


def _normalise_patches(entries: list) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        n = (e.get("name") or "").strip()
        t = (e.get("type") or "").strip()
        if n and t:
            out[n] = t
    return out


def check_contract(
    intake_patches: list,
    manifest_patches: list,
    manifest_patch_types: dict,
) -> tuple[bool, str]:
    declared = _normalise_patches(intake_patches)
    if not declared:
        # NOT APPLICABLE: no user boundary contract exists for this run. Never fabricates patches.
        return True, ""

    # From here the user DID declare boundaries, so absent/empty/malformed delivered evidence is a
    # FAILURE, not a skip - the `if not actual: return False` below is load-bearing.
    actual: dict[str, str] = {}
    if isinstance(manifest_patches, dict):
        _names: list[str] = [n for n in manifest_patches.keys() if isinstance(n, str)]
    elif isinstance(manifest_patches, list):
        _names = [n for n in manifest_patches if isinstance(n, str)]
    else:
        _names = []

    if _names and isinstance(manifest_patch_types, dict):
        for n in _names:
            t = manifest_patch_types.get(n)
            if isinstance(t, str):
                actual[n] = t

    if not actual:
        return False, _format_diagnostic(
            declared, actual,
            extra_msg="Manifest is missing the patches array or patch_types map.",
        )

    declared_cmp = _collapse_spanwise_empty(declared)
    actual_cmp = _collapse_spanwise_empty(actual)

    declared_names = set(declared_cmp.keys())
    actual_names = set(actual_cmp.keys())

    missing = declared_names - actual_names
    extra   = actual_names - declared_names
    mistyped = {
        n: (declared_cmp[n], actual_cmp[n])
        for n in declared_names & actual_names
        if declared_cmp[n] != actual_cmp[n]
    }

    if not (missing or extra or mistyped):
        return True, ""

    return False, _format_diagnostic(
        declared_cmp, actual_cmp,
        missing=sorted(missing),
        extra=sorted(extra),
        mistyped=mistyped,
    )


def _format_diagnostic(
    declared: dict[str, str],
    actual: dict[str, str],
    *,
    missing: list[str] | None = None,
    extra: list[str] | None = None,
    mistyped: dict[str, tuple[str, str]] | None = None,
    extra_msg: str = "",
) -> str:
    lines: list[str] = [
        "[CONTRACT_MISMATCH] The mesh you produced does not match the patch "
        "contract the user agreed to at intake. The pipeline enforces this "
        "contract exactly - patch names and types must match. Fix your patch "
        "definitions so the manifest declares EXACTLY the contracted patches:",
        "",
        "Contracted (from intake - user-confirmed):",
    ]
    for n, t in sorted(declared.items()):
        lines.append(f"  • {_label(n)}  →  type {t}")
    lines.append("")
    lines.append("Built (from manifest):")
    if actual:
        for n, t in sorted(actual.items()):
            lines.append(f"  • {_label(n)}  →  type {t}")
    else:
        lines.append("  (no patches declared)")
    lines.append("")
    if missing:
        lines.append("MISSING (in contract, but your mesh has no such patch):")
        for n in missing:
            lines.append(f"  • {_label(n)}  (type {declared.get(n, 'empty')})")
        lines.append("")
    if extra:
        lines.append("EXTRA (in your mesh, but the user did NOT ask for it):")
        for n in extra:
            lines.append(f"  • {_label(n)}  (type {actual.get(n, 'empty')})")
        lines.append("")
    if mistyped:
        lines.append("TYPE MISMATCH (correct name, wrong type):")
        for n, (want, got) in mistyped.items():
            lines.append(f"  • {_label(n)}: contract says {want!r}, mesh says {got!r}")
        lines.append("")
    if extra_msg:
        lines.append(extra_msg)
        lines.append("")
    lines.append(
        "Fix the patch definitions: rename / merge / split patches so the "
        "exported mesh contains exactly the contracted patch names. Do NOT "
        "change mesh sizing, refinement, or topology when fixing this - only "
        "the patch definitions need to change. If the user asked for "
        "a single 'symmetry' patch but your mesh has 4 spanwise faces, merge "
        "them into ONE patch named 'symmetry' (or whatever the "
        "contract says) rather than producing 4 separate patches."
    )
    return "\n".join(lines)
