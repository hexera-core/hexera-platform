# Responsibility: Verify the engine seam resolves by name, caches, and rejects an unknown engine.
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


def test_get_engine_returns_cfmesh_and_caches():
    from meshpipeline.engines.runtime import get_engine
    eng = get_engine()                              # no name → configured default
    assert eng.name == "cfmesh"
    assert get_engine() is eng                      # cached


def test_get_engine_is_name_keyed_and_rejects_unknown():
    import pytest

    from meshpipeline.engines.registry import UnknownEngineError
    from meshpipeline.engines.runtime import get_engine
    assert get_engine("cfmesh").name == "cfmesh"
    assert get_engine("").name == "cfmesh"                 # UNSET → default
    with pytest.raises(UnknownEngineError):
        get_engine("does-not-exist")



def test_consumers_depend_on_the_seam_not_the_concrete_runner():
    # No consumer may import a concrete runner; they reach engines through the seam.
    for rel in ("agents/builder/agent.py", "agents/builder/tools/meshing.py", "pipeline/executor.py",
                "cad/staging.py", "pipeline/geometry_admission.py"):
        assert "cfmesh_runner" not in (APP / rel).read_text(), \
            f"{rel} must depend on mesh_engine, not cfmesh_runner"
    # The files that actually INVOKE a runner do it through get_engine (the seam). agent.py
    # no longer calls a runner directly - it delegates surface staging to geometry.staging -
    # so the seam usage lives there, not in the builder node.
    for rel in ("agents/builder/tools/meshing.py", "pipeline/executor.py", "cad/staging.py"):
        src = (APP / rel).read_text()
        assert "mesh_engine import get_engine" in src or "get_engine(" in src, \
            f"{rel} must reach the runner through the get_engine seam"
    # ONE default source: engines.registry.default_engine() (the old
    # cfg.MESH_ENGINE knob duplicated it and could disagree)
    assert "MESH_ENGINE" not in "\n".join(p.read_text() for p in [*(APP / "settings").glob("*.py"), *APP.glob("agents/*/settings.py"), *APP.glob("engines/*/settings.py")])
    assert "default_engine()" in (APP / "engines" / "runtime.py").read_text()
