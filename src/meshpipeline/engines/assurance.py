# Responsibility: Derive what a given run must have inspected before its review can be trusted.
# Boundaries: it plans required evidence; it collects none.
# Collaborates with: contracts/review_evidence.py and agents/reviewer/eligibility.py.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from meshpipeline.contracts.review_evidence import (
    HardGateRequirement,
    InspectionTarget,
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    SpatialInspectionRequirement,
)


@dataclass(frozen=True)
class AssurancePlan:

    engine: str                              # identity, for provenance - NEVER for dispatch
    purpose: str
    axes: tuple                              # the composed engine ∪ purpose ReviewAxis tuple
    required_gate_keys: frozenset[str]
    required_metric_keys: frozenset[str]
    required_render_artifacts: frozenset[str]
    required_targets: tuple[InspectionTarget, ...]
    requires_render: bool                    # the DERIVED successor to `visual_review`

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(ax.name for ax in self.axes)


def _axis_requirements(axes: tuple) -> dict[str, set[str]]:
    metric_keys: set[str] = set()
    gate_keys: set[str] = set()
    view_ids: set[str] = set()
    for ax in axes:
        for req in getattr(ax, "requires", ()):
            if isinstance(req, MetricRequirement):
                metric_keys.add(req.key)
            elif isinstance(req, HardGateRequirement):
                gate_keys.add(req.key)
            elif isinstance(req, (RenderViewRequirement,)):
                view_ids.add(req.view_id)
    return {"metric": metric_keys, "gate": gate_keys, "view": view_ids}


def derive_assurance_plan(spec: Any, purpose: str = "",
                          user_dispute: Any = None,
                          phase: str = "") -> AssurancePlan:
    # Shared composition mechanism - unions the engine's own axes with the purpose's, dedups by
    # name, stamps ownership. It does not branch on the engine.
    from meshpipeline.engines.quality_criteria import compose_review_rubric

    axes = tuple(compose_review_rubric(spec.name, purpose, user_dispute, phase))
    from_axes = _axis_requirements(axes)

    gate_keys = frozenset(g.key for g in spec.gates if getattr(g, "blocking", True))
    gate_keys |= frozenset(from_axes["gate"])

    render_artifacts = frozenset(
        a.artifact_key for a in spec.render_artifacts if getattr(a, "required", False))
    required_targets = tuple(t for t in spec.inspection_targets if t.required)

    # THE DERIVED SUCCESSOR TO `visual_review`. An engine's review obliges rendered evidence iff it
    # declares a renderer AND has something rendered to require (a required artifact or target).
    # Mechanical, from capability - not an authored policy flag.
    requires_render = bool(
        spec.renders_for_review and (render_artifacts or required_targets))

    return AssurancePlan(
        engine=spec.name,
        purpose=purpose,
        axes=axes,
        required_gate_keys=gate_keys,
        required_metric_keys=frozenset(from_axes["metric"]),
        required_render_artifacts=render_artifacts,
        required_targets=required_targets,
        requires_render=requires_render,
    )


__all__ = [
    "AssurancePlan",
    "HardGateRequirement",
    "MetricRequirement",
    "RenderTargetRequirement",
    "RenderViewRequirement",
    "SpatialInspectionRequirement",
    "derive_assurance_plan",
]
