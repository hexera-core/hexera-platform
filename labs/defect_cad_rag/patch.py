"""Promoted to `meshpipeline.cad.repair.patchdesc` (experiments 1-2); this shim keeps lab tooling
and recorded commands working. The single source of truth is the src module."""

from meshpipeline.cad.repair.patchdesc import *  # noqa: F401,F403
from meshpipeline.cad.repair.patchdesc import (  # noqa: F401
    _CURVES,
    _ENTITY_TYPES,
    _SURFACES,
    _Topology,
)
from meshpipeline.cad.repair.patchdesc import _log_ratio  # noqa: F401
