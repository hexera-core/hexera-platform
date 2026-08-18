# Responsibility: Run an engine's declared quality gates and decide what a CRASHING gate means.
# Boundaries: it executes gates and reports outcomes; it does not define any gate's threshold.
# Collaborates with: each engine's flow_gates module and engines/quality_criteria.py.
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GateCtx:

    workspace: Path
    engine: str = ""
    domain: str = ""
    intake_patches: list = field(default_factory=list)
    engine_params: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)   # lazily loaded (see below)

    def manifest_or_load(self) -> dict:
        if not self.manifest:
            try:
                with open(Path(self.workspace) / "mesh_manifest.json") as f:
                    self.manifest = json.load(f) or {}
            except Exception:
                self.manifest = {}
        return self.manifest


@dataclass(frozen=True)
class GateSpec:
    key: str                                        # e.g. "manifest_valid"
    check: Callable[[GateCtx], tuple[bool, str]]    # (ok, feedback-on-reject)
    blocking: bool = True
    # WHICH PART of the mesh setup this gate speaks for. The gate that authors the
    # rejection feedback also names its category, so the classifier is a LOOKUP instead
    # of a regex over the feedback text (which went stale the moment a new engine shipped
    # a new marker). One of pipeline.enums.FailureSection.
    section: str = "MESH"
    # WHAT THIS GATE PROVES, said to the ENGINEER who will run the mesh - not the
    # internal key. This is a TRUST surface: the user is being shown the checks their
    # mesh survived, so it must read as evidence ("no negative-volume cells"), never
    # as a variable name ("manifest_valid"). Engine-declared, because the gate is.
    proves: str = ""


def run_gates(gates: tuple, ctx: GateCtx, on_result=None) -> tuple[bool, str, str]:
    from meshpipeline.errors import FailureClass, SystemFailure
    for g in gates:
        try:
            ok, feedback = g.check(ctx)
            if on_result is not None:
                try:
                    on_result(g.key, ok, feedback)
                except Exception:      # reporting must never break validation
                    logger.debug("run_gates: on_result reporter failed", exc_info=True)
        except SystemFailure:
            raise
        except Exception as exc:
            logger.exception("gate %r CRASHED - system failure, not a mesh rejection", g.key)
            raise SystemFailure(f"gate:{g.key}", FailureClass.INTERNAL,
                                f"gate {g.key!r} crashed: {exc}", cause=exc) from exc
        if not ok and g.blocking:
            return False, g.key, feedback
    return True, "", ""


__all__ = ["GateCtx", "GateSpec", "run_gates"]
