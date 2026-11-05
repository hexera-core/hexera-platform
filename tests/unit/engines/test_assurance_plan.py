# Responsibility: Verify each engine's assurance plan is derived from its spec, deterministically, never from its name.
from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest

from meshpipeline.contracts.review_evidence import (
    HardGateRequirement,
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    SpatialInspectionRequirement,
    TargetKind,
)
from meshpipeline.engines import assurance
from meshpipeline.engines.assurance import AssurancePlan, derive_assurance_plan
from meshpipeline.engines.registry import ENGINE_CATALOG

ENGINES = ("snappy", "cfmesh", "snappy_multiregion", "gmsh", "vmtk")


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_derives_a_plan(engine):
    plan = derive_assurance_plan(ENGINE_CATALOG[engine], "external_cfd")
    assert isinstance(plan, AssurancePlan) and plan.engine == engine and plan.axes


@pytest.mark.parametrize("engine", ENGINES)
def test_the_plan_is_deterministic(engine):
    a = derive_assurance_plan(ENGINE_CATALOG[engine], "external_cfd")
    b = derive_assurance_plan(ENGINE_CATALOG[engine], "external_cfd")
    assert a == b


@pytest.mark.parametrize("engine", ENGINES)
def test_gates_artifacts_targets_come_from_the_spec(engine):
    spec = ENGINE_CATALOG[engine]
    plan = derive_assurance_plan(spec, "external_cfd")
    axis_gates = {r.key for ax in plan.axes for r in getattr(ax, "requires", ())
                  if isinstance(r, HardGateRequirement)}
    assert plan.required_gate_keys == frozenset(g.key for g in spec.gates if g.blocking) | axis_gates
    assert plan.required_render_artifacts == frozenset(
        a.artifact_key for a in spec.render_artifacts if a.required)
    assert plan.required_targets == tuple(t for t in spec.inspection_targets if t.required)


@pytest.mark.parametrize("engine", ENGINES)
def test_requires_render_is_derived_from_capability_not_a_flag(engine):
    spec = ENGINE_CATALOG[engine]
    plan = derive_assurance_plan(spec, "external_cfd")
    assert plan.requires_render is bool(
        spec.renders_for_review and (plan.required_render_artifacts or plan.required_targets))


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_plan_requires_render(engine):
    plan = derive_assurance_plan(ENGINE_CATALOG[engine], "external_cfd")
    assert plan.requires_render


def test_the_plan_module_never_branches_on_an_engine_name():
    tree = ast.parse(inspect.getsource(assurance))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    evaluated = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in docs}
    for engine in ENGINE_CATALOG:
        assert engine not in evaluated, f"the plan module names the engine {engine!r}"


def test_derive_obtains_declarations_through_the_spec():
    src = inspect.getsource(derive_assurance_plan)
    for attr in ("spec.gates", "spec.render_artifacts", "spec.inspection_targets",
                 "spec.renders_for_review", "spec.name"):
        assert attr in src, f"derivation does not read {attr}"
    for engine in ENGINE_CATALOG:
        for dispatch in (f'== "{engine}"', f'"{engine}":', f'["{engine}"]'):
            assert dispatch not in src, f"derivation dispatches on {engine!r}"


def test_the_plan_holds_no_provider_or_backend_state():
    fields = {f.name for f in dataclasses.fields(AssurancePlan)}
    for banned in ("provider", "model", "backend", "runtime", "session", "sandbox", "publish"):
        assert not any(banned in f for f in fields), f"the plan carries {banned}"


def test_the_evidence_requirement_vocabulary_is_typed_and_closed():
    reqs = [HardGateRequirement("g"), MetricRequirement("m"), RenderViewRequirement("iso"),
            RenderTargetRequirement(TargetKind.PATCH, "patch:*"),
            SpatialInspectionRequirement(TargetKind.REGION)]
    for r in reqs:
        assert dataclasses.is_dataclass(r) and r.__dataclass_params__.frozen
    assert RenderTargetRequirement(TargetKind.OPENING, "opening:*").selector == "opening:*"


def test_review_axis_accepts_typed_requirements_additively():
    from meshpipeline.engines.review_types import ReviewAxis

    assert ReviewAxis(name="a", validation_axis="quality", guidance="g").requires == ()
    ax = ReviewAxis(name="b", validation_axis="quality", guidance="g",
                    requires=(MetricRequirement("cells"),
                              SpatialInspectionRequirement(TargetKind.REGION)))
    assert len(ax.requires) == 2


@pytest.mark.parametrize("engine", ENGINES)
def test_a_render_based_axis_anchor_becomes_a_metric_obligation(engine):
    plan = derive_assurance_plan(ENGINE_CATALOG[engine], "external_cfd")
    anchored = {k for ax in plan.axes for k in getattr(ax, "anchor", ())}
    assert anchored <= plan.required_metric_keys
