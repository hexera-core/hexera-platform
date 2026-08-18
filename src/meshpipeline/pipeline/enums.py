# Responsibility: Hold the pipeline's closed vocabularies and the fidelity policy derived from them.
# Owns: verdicts, dimensionality, mesh-fidelity tiers and their resolution, and the failure sections.
# Boundaries: vocabulary and the rules for reading it consistently; no node's behaviour lives here.
from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    PASS = "PASS"  # noqa: S105  (a verdict, not a password)
    FAIL = "FAIL"


class QualityOutcome(StrEnum):
    API_FAILURE        = "api_failure"
    UNSOLVABLE         = "unsolvable"
    PIPELINE_FAILURE   = "pipeline_failure"
    SUCCEEDED          = "succeeded"
    REVIEWER_REJECTION = "reviewer_rejection"


class Dimensionality(StrEnum):
    # Two values only. "2.5D" was removed: a thin slab meshed in full 3D with
    # symmetry patches on the spanwise faces IS a 3D case - a separate vocabulary
    # value only invited pseudo-2D workarounds.
    TWO_D      = "2D"
    THREE_D    = "3D"


class MeshFidelity(StrEnum):
    DRAFT    = "draft"      # fast, lower-cost: geometry checks, boundary setup, early iteration
    STANDARD = "standard"   # balanced default for ordinary use
    MAX      = "max"        # highest BOUNDED detail the selected engine supports


class MeshFidelitySource(StrEnum):
    USER    = "user"
    DEFAULT = "default"


#: The effective tier when the user states no preference. Declared once so every layer agrees.
DEFAULT_MESH_FIDELITY = MeshFidelity.STANDARD

#: Version of the three-tier default policy. It is fingerprinted with the approval, so changing the
#: default tier later cannot silently re-interpret an already approved job: the recomputed intent no
#: longer matches and the run is refused.
FIDELITY_POLICY_VERSION = "3tier-v1"


class FidelityContractError(ValueError):
    pass


def canonical_mesh_fidelity(value) -> MeshFidelity | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    try:
        return MeshFidelity(raw)
    except ValueError:
        raise FidelityContractError(
            f"mesh detail preference {value!r} is not one of "
            f"{[m.value for m in MeshFidelity]}") from None


def authoring_tier(value) -> MeshFidelity:
    # Non-raising resolution for the advisory authoring hint: a non-canonical value falls back to the
    # default rather than failing a build over an advisory setting.
    raw = str(value or "").strip().lower()
    try:
        return MeshFidelity(raw)
    except ValueError:
        return DEFAULT_MESH_FIDELITY


def resolve_mesh_fidelity(requested) -> tuple[MeshFidelity | None, MeshFidelity, MeshFidelitySource]:
    req = canonical_mesh_fidelity(requested)
    if req is None:
        return None, DEFAULT_MESH_FIDELITY, MeshFidelitySource.DEFAULT
    return req, req, MeshFidelitySource.USER


def assert_fidelity_consistent(*, requested, effective, source, policy_version=None) -> None:
    req = None if requested in (None, "") else str(requested).strip().lower()
    eff = str(effective or "").strip().lower()
    src = str(source or "").strip().lower()

    if src not in (MeshFidelitySource.USER, MeshFidelitySource.DEFAULT):
        raise FidelityContractError(f"mesh_fidelity_source {source!r} is not user|default")
    if eff not in {m.value for m in MeshFidelity}:
        raise FidelityContractError(
            f"effective_mesh_fidelity {effective!r} is not one of "
            f"{[m.value for m in MeshFidelity]}")
    if req is not None and req not in {m.value for m in MeshFidelity}:
        raise FidelityContractError(
            f"requested_mesh_fidelity {requested!r} is not one of "
            f"{[m.value for m in MeshFidelity]} or null")
    if src == MeshFidelitySource.USER:
        if req is None:
            raise FidelityContractError("source=user requires a non-null requested_mesh_fidelity")
        if eff != req:
            raise FidelityContractError(
                f"source=user requires effective == requested (got {eff!r} vs {req!r})")
    else:
        if req is not None:
            raise FidelityContractError(
                f"source=default requires a null requested_mesh_fidelity (got {requested!r})")
        if eff != DEFAULT_MESH_FIDELITY.value:
            raise FidelityContractError(
                f"source=default requires effective == {DEFAULT_MESH_FIDELITY.value!r} "
                f"(got {eff!r})")
    if policy_version is not None and str(policy_version) != FIDELITY_POLICY_VERSION:
        raise FidelityContractError(
            f"fidelity_policy_version {policy_version!r} is not {FIDELITY_POLICY_VERSION!r} - the "
            "default/alias policy changed after this job was approved")


#: The user-facing label. "Fidelity" alone reads as a quality guarantee; this does not.
FIDELITY_LABEL = "Mesh detail preference"


def fidelity_value_text(*, effective, source) -> str:
    tier = str(effective or "").strip().lower()
    if not tier:
        return ""
    suffix = ("user requested" if str(source) == MeshFidelitySource.USER else "system default")
    return f"{tier.capitalize()} ({suffix})"


def render_fidelity_line(*, effective, source) -> str:
    value = fidelity_value_text(effective=effective, source=source)
    return f"{FIDELITY_LABEL}: {value}" if value else ""


class FailureSection(StrEnum):
    GEOMETRY = "GEOMETRY"
    DOMAIN   = "DOMAIN"
    MESH     = "MESH"
    GROUPS   = "GROUPS"
    LAYERS   = "LAYERS"
    MANIFEST = "MANIFEST"
    PATCHES  = "PATCHES"
    TOPOLOGY = "TOPOLOGY"


# Rejection sources that are NOT declared gates: the engine seams the executor calls
# directly, plus a finalize that failed before the gate chain ever ran.
SEAM_SECTIONS: dict[str, str] = {
    "finalize":      FailureSection.MESH,
    "domain_extent": FailureSection.DOMAIN,
    "solvability":   FailureSection.MESH,
    # the builder's input-contract pre-flight: the INPUT geometry is unmeshable (e.g. a
    # self-intersecting surface for a fill engine) - a geometry defect, not a mesh one.
    "geometry":      FailureSection.GEOMETRY,
}
