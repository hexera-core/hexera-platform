"""The manifest filters `mesh_paths.volume` through an existence guard and hardcodes the surface.

WHY THIS EXISTS. `engines/manifest._published_volume` states the rule in its own docstring -
"Absence is a real, stated state; a stand-in path is not" - and one line later
`write_manifest` writes

    "mesh_paths": {"surface": str((ws / "mesh.msh").resolve()), "volume": volume_path}

so the key the rule was written for obeys it and the key beside it does not. The surface is named
unconditionally, whether or not anything wrote it.

MEASURED. Job 6ddb58aa-2224-4210-a2ac-926bc2e7ac42's own mesh_manifest.json, read on 2026-11-14:

    {'surface': '/srv/workspaces/6ddb58aa-.../generation_1/attempt_1/mesh.msh', 'volume': None}

and `ls mesh.msh` in that directory: "No such file or directory". The volume is honestly absent
because the guard ran; the surface is a path to nothing because it did not.

WHAT IT COSTS, AND WHAT IT DOES NOT. `mesh_paths.surface` is required=True for all seven engines
(each bundle's `_shared._RENDER_ARTIFACTS`), and `sandbox/review_session.resolve_raw` refuses it
BOTH ways with the same ReviewEvidenceFailure.EVIDENCE_MISSING: a missing value raises "required
artifact 'mesh_paths.surface' is not in the manifest" and a value naming a file that is not there
raises "required artifact 'mesh_paths.surface' does not exist". So applying the rule to the
surface changes which sentence the review refuses with and never whether it refuses. Nothing has
to change alongside: `agents/reviewer/visual._recover_manifest` already tests the key for
truthiness, and today a dangling path reads True there - it treats a manifest naming a file that
does not exist as the good one and never looks on disk for a better.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines.manifest import write_manifest

_MSH = b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n"


def _write(ws: Path, **kw):
    return write_manifest(
        ws, patch_types={"inlet": "patch", "outlet": "patch", "wall": "wall"},
        patch_entities={}, bbox=(0, 0, 0, 1, 1, 1),
        quality={"cells": 50_000, "max_non_ortho": 41.0, "fatal": [], "skew_fraction": 0.0},
        mesh_units="m", mesh_mode="cfmesh_conjugate", **kw)


def test_a_surface_that_was_written_is_published(tmp_path):
    (tmp_path / "mesh.msh").write_bytes(_MSH)
    m = _write(tmp_path)
    assert m["mesh_paths"]["surface"] == str((tmp_path / "mesh.msh").resolve())


def test_a_surface_nobody_wrote_is_not_published(tmp_path):
    """Job 6ddb58aa exactly: no mesh.msh on disk and the manifest naming one."""
    m = _write(tmp_path)
    assert m["mesh_paths"]["surface"] is None, (
        "the manifest names a file that is not there - the guard's own rule, applied to the key "
        "beside the one it already guards")


def test_a_directory_where_the_surface_belongs_is_not_published(tmp_path):
    """The volume guard's own failure mode, on the other key: an OpenFOAM `constant/` published
    as a file artifact is no mesh any reader opens."""
    (tmp_path / "mesh.msh").mkdir()
    m = _write(tmp_path)
    assert m["mesh_paths"]["surface"] is None


def test_the_volume_still_obeys_the_rule_it_already_obeyed(tmp_path):
    (tmp_path / "mesh.msh").write_bytes(_MSH)
    vtu = tmp_path / "VTK" / "case_0" / "internal.vtu"
    vtu.parent.mkdir(parents=True)
    vtu.write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="UnstructuredGrid"></VTKFile>\n')
    assert _write(tmp_path, volume_path=str(vtu))["mesh_paths"]["volume"] == str(vtu)
    assert _write(tmp_path, volume_path=None)["mesh_paths"]["volume"] is None
    d = tmp_path / "constant"
    d.mkdir()
    assert _write(tmp_path, volume_path=str(d))["mesh_paths"]["volume"] is None


def test_an_absent_surface_is_refused_the_same_way_a_dangling_one_is(tmp_path):
    """The behaviour this change is allowed to alter is the SENTENCE, not the verdict. Both
    readings raise EVIDENCE_MISSING out of the one resolver every engine's review goes through."""
    from meshpipeline.contracts.review_evidence import (
        ArtifactFormat,
        RenderArtifactRequirement,
        ReviewEvidenceFailure,
    )
    from meshpipeline.sandbox.review_session import ArtifactResolver, ReviewRenderError

    req = RenderArtifactRequirement(
        artifact_key="mesh_paths.surface", allowed_formats=(ArtifactFormat.GMSH_MSH,),
        purpose="the delivered boundaries", required=True)
    dangling = ArtifactResolver(tmp_path, {"mesh_paths": {"surface": str(tmp_path / "gone.msh")}})
    absent = ArtifactResolver(tmp_path, {"mesh_paths": {"surface": None}})
    for resolver in (dangling, absent):
        with pytest.raises(ReviewRenderError) as ei:
            resolver.resolve(req)
        assert ei.value.category is ReviewEvidenceFailure.EVIDENCE_MISSING
        assert "mesh_paths.surface" in str(ei.value)


def test_the_surface_is_required_by_every_engine_that_declares_one():
    """Before changing the key, the claim that nothing may quietly lose a review: every bundle
    requires it, so None can never be read as "this engine does not need one"."""
    from meshpipeline.engines.registry import engine_names, get_spec

    declared = 0
    for name in engine_names():
        for a in get_spec(name).render_artifacts:
            if a.artifact_key != "mesh_paths.surface":
                continue
            declared += 1
            assert a.required is True, f"{name} would silently review without a surface"
    assert declared >= 7, f"only {declared} engines declare a review surface"
