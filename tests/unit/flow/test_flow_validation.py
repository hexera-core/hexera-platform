# Responsibility: Verify the shared prompts name no domain, and flow manifest checks apply regardless of the label.
from __future__ import annotations

import json
from pathlib import Path

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

PROMPTS_DIR = APP_DIR / "prompts"


class TestPromptDomainMismatch:
    def test_builder_prompt_no_domain_placeholder(self):
        # The cfmesh builder prompt is ENGINE-OWNED (engines/cfmesh/pack.py) - import
        # it from the bundle, not through the shared builder (which holds no engine
        # prompt; it resolves spec.system_prompt for the selected engine).
        from meshpipeline.engines.cfmesh.pack import CFMESH_SYSTEM
        assert "{domain}" not in CFMESH_SYSTEM[:200], \
            "cfmesh engine prompt still uses a {domain} placeholder in the role line"

    def test_builder_prompt_says_cfd(self):
        # CFD in the ENGINE's own prompt is correct - cfmesh is a CFD mesher.
        from meshpipeline.engines.cfmesh.pack import CFMESH_SYSTEM
        assert "cfd" in CFMESH_SYSTEM[:200].lower(), \
            "cfmesh engine prompt must mention CFD explicitly in the opening"

    def test_reviewer_prompt_no_domain_placeholder_in_role(self):
        text = (PROMPTS_DIR / "reviewer" / "system.txt").read_text()
        assert "{domain}" not in text[:200], \
            "reviewer_system.txt still uses {domain} in the role line"

    def test_reviewer_prompt_is_neutral(self):
        # Reviewer-ownership migration (2026-07-07): the reviewer prompt is now
        # engine/domain-NEUTRAL - the CFD/FEA persona + inspection specifics moved
        # into the injected workflow line + the composed engine/purpose review rubric.
        # (Full neutrality guard: test_review_rubric.test_shared_reviewer_prompts_are_neutral.)
        text = (PROMPTS_DIR / "reviewer" / "system.txt").read_text().lower()
        assert "mesh-quality reviewer" in text[:200]
        assert "cfd" not in text and "far-field" not in text

    def test_no_terminal_response_prompt_exists(self):
        assert not (PROMPTS_DIR / "intake" / "outcome.txt").exists()
        for path in PROMPTS_DIR.rglob("*.txt"):
            stem = path.stem.lower()
            assert stem not in {"outcome", "closer", "closing", "final", "finalizer",
                                "finalization", "result"}, f"terminal-response prompt: {path}"

    def test_intake_prompt_is_domain_neutral(self):
        text = (PROMPTS_DIR / "intake" / "system.txt").read_text().lower()
        assert "do not assume" in text or "domain neutralit" in text



def _make_cfd_manifest(
    has_wall=True, has_inflow=True, has_outflow=True, empty_patch=False, mesh_written=True
) -> dict:
    # cfMesh schema-2.1 manifest shape (see engines.cfmesh.cfmesh_runner.write_manifest).
    return {
        "schema_version": "2.1",
        "mesh_mode": "cfmesh",
        "mesh_written": mesh_written,
        "cell_count": 12345,
        "geometry": {"box_xmin": -1.0, "box_xmax": 1.0},
        "patches": {"body": [1], "farfield": [2]},
        "patch_types": {"body": "wall", "farfield": "farfield"},
        "validation": {
            "has_wall":    has_wall,
            "has_inflow":  has_inflow,
            "has_outflow": has_outflow,
            "patch_validation": {"body": True, "farfield": not empty_patch},
        },
    }


class TestManifestValidation:
    def _write_manifest(self, tmp_path, manifest, polymesh=True):
        (tmp_path / "mesh_manifest.json").write_text(json.dumps(manifest))
        if polymesh:
            pm = tmp_path / "constant" / "polyMesh"
            pm.mkdir(parents=True, exist_ok=True)
            # a polyMesh is the WHOLE set - `owner` alone is an interrupted write, and the
            # deliverable gate now says so (see test_openfoam_deliverable_contract.py)
            for f in ("owner", "neighbour", "points", "faces", "boundary"):
                (pm / f).write_text("dummy")

    def test_cfd_manifest_all_patches_passes(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        self._write_manifest(tmp_path, _make_cfd_manifest())
        ok, msg = _validate_manifest(tmp_path, domain="supersonic rocket external aerodynamics")
        assert ok, msg

    def test_cfd_manifest_missing_inlet_fails(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        self._write_manifest(tmp_path, _make_cfd_manifest(has_inflow=False))
        ok, msg = _validate_manifest(tmp_path, domain="external aerodynamics flow")
        assert not ok
        assert "inflow" in msg.lower() or "inlet" in msg.lower()

    def test_cfd_manifest_missing_outlet_fails(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        self._write_manifest(tmp_path, _make_cfd_manifest(has_outflow=False))
        ok, msg = _validate_manifest(tmp_path, domain="cfd external")
        assert not ok
        assert "outflow" in msg.lower() or "outlet" in msg.lower()

    def test_cfd_manifest_missing_body_fails(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        self._write_manifest(tmp_path, _make_cfd_manifest(has_wall=False))
        ok, msg = _validate_manifest(tmp_path, domain="external aerodynamics")
        assert not ok
        assert "wall" in msg.lower()

    def test_cfd_manifest_empty_patch_fails(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        self._write_manifest(tmp_path, _make_cfd_manifest(empty_patch=True))
        ok, msg = _validate_manifest(tmp_path, domain="external aerodynamics")
        assert not ok
        assert "zero faces" in msg.lower()

    def test_mesh_not_written_fails_regardless_of_domain(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        self._write_manifest(tmp_path, _make_cfd_manifest(mesh_written=False))
        ok, msg = _validate_manifest(tmp_path, domain="")
        assert not ok
        assert "not written" in msg.lower()

    def test_flow_checks_apply_regardless_of_label(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        manifest = {
            "schema_version": "2.1",
            "mesh_mode": "cfmesh",
            "mesh_written": True,
            "geometry": {},
            "patches": {},
            "patch_types": {},
            "validation": {
                "has_wall":    False,
                "has_inflow":  False,
                "has_outflow": False,
                "patch_validation": {},
            },
        }
        self._write_manifest(tmp_path, manifest)
        # The pre-rework escape hatch (declared non-CFD domain skipped wall/
        # inflow checks) is GONE: these gates are the FLOW engines' declared
        # chain, and only flow engines declare them - a wall-less manifest is
        # always a rejection now, whatever the descriptive label says.
        ok, msg = _validate_manifest(tmp_path, domain="structural_fea")
        assert not ok and "wall" in msg.lower()

    def test_empty_domain_applies_cfd_checks(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        self._write_manifest(tmp_path, _make_cfd_manifest(has_inflow=False))
        ok, msg = _validate_manifest(tmp_path, domain="")
        assert not ok
        assert "inflow" in msg.lower() or "inlet" in msg.lower()

    def test_manifest_missing_key_fails(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        manifest = {"schema_version": "1.0", "geometry": {}, "patches": {}}
        (tmp_path / "mesh_manifest.json").write_text(json.dumps(manifest))
        ok, msg = _validate_manifest(tmp_path, domain="cfd external")
        assert not ok
        assert "validation" in msg.lower() or "missing" in msg.lower()

    def test_missing_manifest_file_fails(self, tmp_path):
        from meshpipeline.engines.cfmesh.flow_gates import _validate_manifest
        ok, msg = _validate_manifest(tmp_path, domain="cfd external")
        assert not ok
        assert "missing" in msg.lower() or "not written" in msg.lower()
