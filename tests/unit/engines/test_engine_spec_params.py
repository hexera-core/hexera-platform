# Responsibility: Verify flow topology is a capability rather than an engine parameter, and is derived from purpose.
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines import registry as ec  # noqa: E402


def test_topology_is_not_a_declared_param():
    for eng in ("cfmesh", "snappy"):
        assert "topology" not in [p.key for p in ec.get_spec(eng).intake_params]


def test_capabilities_declare_their_flow_topologies():
    caps = {c.output_kind: c.topologies for c in ec.get_spec("cfmesh").capabilities}
    assert set(caps["fluid-volume"]) == {"internal", "external"}
    # vmtk is a lumen mesher: internal only, no far field
    vmtk_caps = ec.get_spec("vmtk").capabilities
    assert set(vmtk_caps[0].topologies) == {"internal"}


def test_validate_rejects_topology_now_that_it_is_not_a_param():
    assert any("unknown param 'topology'" in p for p in
               ec.validate_engine_params("cfmesh", {"topology": "internal"}))
    assert any("unknown param 'topology'" in p for p in
               ec.validate_engine_params("snappy", {"topology": "external"}))
    # an engine with no declared params validates an empty dict cleanly
    assert ec.validate_engine_params("cfmesh", {}) == []


def test_resolve_engine_params_returns_engine_native_only_never_topology():
    assert ec.resolve_engine_params("snappy", None) == {}
    assert ec.resolve_engine_params("cfmesh", {}) == {}
    assert "topology" not in ec.resolve_engine_params("cfmesh", {"topology": "internal"})
    assert ec.resolve_engine_params("gmsh", {"element_order": "2"}) == {"element_order": "2"}


def test_topology_of_derives_the_flow_regime_from_the_purpose():
    from meshpipeline.engines.purposes import topology_of
    assert topology_of("external_cfd") == "external"
    assert topology_of("internal_cfd") == "internal"
    assert topology_of("structural") == ""          # structural has no flow regime
    assert topology_of("") == "" and topology_of("bogus") == ""


def test_topology_never_enters_vmtk_engine_params():
    resolved = ec.resolve_engine_params("vmtk", {"wall_layers": "on"})
    assert "topology" not in resolved
    assert resolved == {"wall_layers": "on"}


def test_engines_producing_topology_reads_capabilities_not_params():
    # gmsh serves either topology from a PREPARED fluid-domain solid; the wrap-and-fill
    # engines from a body surface; vmtk internal only.
    assert set(ec.engines_producing_topology("internal")) == {"cfmesh", "gmsh", "snappy", "vmtk"}
    assert set(ec.engines_producing_topology("external")) == {"cfmesh", "gmsh", "snappy"}
    assert "vmtk" not in ec.engines_producing_topology("external")
    assert ec.engines_producing_topology("bogus") == []


def test_intake_schema_requires_engine_params():
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    fn = next(t["function"] for t in INTAKE_TOOLS
              if t["function"]["name"] == "submit_requirements")
    assert "engine_params" in fn["parameters"]["properties"]
    assert "engine_params" in fn["parameters"]["required"]


def test_intake_prompt_does_not_ask_topology():
    import meshpipeline.agents.intake.agent as intake
    system = intake.compose_intake_system()
    assert "engine_params" in system
    assert "topology (external | internal)" not in system


def test_worker_seeding_resolves_params(monkeypatch):
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "application" / "pipeline_run.py").read_text()
    assert "resolve_engine_params" in src
    assert '"engine_params"' in src
