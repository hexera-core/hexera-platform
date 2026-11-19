"""Diagnostics-only CAD simplification core (the twin of cad/repair)."""

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
]
