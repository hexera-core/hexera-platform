# Responsibility: Say which output classes a run must produce before it can be called delivered.
# Boundaries: a policy question answered from the engine's declaration; it inspects no file.
from __future__ import annotations

#: Bump when the REQUIRED/OPTIONAL classification changes in a way that alters what a job must
#: deliver. Adding a new optional export does not require a bump; promoting one to required does.
ARTIFACT_POLICY_VERSION = "artifacts-v2"

#: The logical artifact class that constitutes delivery of the mesh itself.
REQUIRED_BUNDLE_CLASS = "mesh_bundle"

#: The viewer/quality payload the pipeline renders from the workspace it owns. Required, because
#: the API has no workspace in a hosted deployment: a mesh the user is told about but cannot view
#: is not a completed delivery, and the payload comes from the same run that produced the bundle.
REQUIRED_VIEWER_CLASS = "viewer_data"

#: Classes that are delivered when available but never required. Their absence is at most a warning.
OPTIONAL_CLASSES: tuple[str, ...] = ("mesh",)


def required_output_classes(engine) -> list[str]:
    name = str(engine or "").strip().lower()
    if not name:
        return []
    try:
        from meshpipeline.engines.registry import get_spec
        spec = get_spec(name)
    except Exception:  # noqa: BLE001 - unknown engine: no policy, and admission refuses the run
        return []
    if not getattr(spec, "deliverable", None):
        return []
    return [REQUIRED_BUNDLE_CLASS, REQUIRED_VIEWER_CLASS]


def is_required_class(engine, artifact_class) -> bool:
    return str(artifact_class) in required_output_classes(engine)


def required_ready(engine, delivered_classes) -> bool:
    required = required_output_classes(engine)
    if not required:
        return False
    have = {str(c) for c in (delivered_classes or [])}
    return all(c in have for c in required)


def optional_warnings(delivered_classes) -> list[str]:
    have = {str(c) for c in (delivered_classes or [])}
    return [c for c in OPTIONAL_CLASSES if c not in have]


__all__ = ["ARTIFACT_POLICY_VERSION", "REQUIRED_BUNDLE_CLASS", "REQUIRED_VIEWER_CLASS", "OPTIONAL_CLASSES",
           "required_output_classes", "is_required_class", "required_ready", "optional_warnings"]
