"""CAD simplification core (the twin of cad/repair): inspection, and the verified defeaturing
behind CAD_SIMPLIFY_ENABLED."""

from meshpipeline.cad.simplify.contracts import (
    FeatureCandidate,
    FeatureKind,
    SimplifyMode,
    SimplifyPolicy,
    SimplifyProfile,
    SimplifyReport,
    SimplifyResult,
    SimplifyStatus,
    SimplifyTarget,
)


def inspect_features(*args, **kwargs):
    from meshpipeline.cad.simplify.inspect import inspect_features as _inspect_features

    return _inspect_features(*args, **kwargs)


def simplify_step_file(*args, **kwargs):
    from meshpipeline.cad.simplify.simplify import simplify_step_file as _simplify_step_file

    return _simplify_step_file(*args, **kwargs)


__all__ = [
    "FeatureCandidate",
    "FeatureKind",
    "SimplifyMode",
    "SimplifyPolicy",
    "SimplifyProfile",
    "SimplifyReport",
    "SimplifyResult",
    "SimplifyStatus",
    "SimplifyTarget",
    "inspect_features",
    "simplify_step_file",
]
