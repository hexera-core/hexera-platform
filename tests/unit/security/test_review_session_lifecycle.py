# Responsibility: Verify a review session resolves artifacts by manifest key, closes on every exit, bounds its images.
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meshpipeline.contracts.review_evidence import (
    ArtifactFormat,
    CommandKind,
    EvidenceItem,
    InspectionTarget,
    RenderArtifactRequirement,
    RenderCapabilities,
    RenderCommand,
    RenderContext,
    ReviewEvidenceFailure,
    ReviewRenderError,
    TargetKind,
)
from meshpipeline.sandbox.review_session import (
    MAX_IMAGE_BYTES,
    ArtifactResolver,
    open_review_session,
)

SURFACE = RenderArtifactRequirement(
    artifact_key="mesh_paths.surface", allowed_formats=(ArtifactFormat.GMSH_MSH,),
    purpose="the mesh", required=True)
OPTIONAL = RenderArtifactRequirement(
    artifact_key="mesh_paths.lumen", allowed_formats=(ArtifactFormat.VTK_VTP,),
    purpose="the submitted lumen", required=False)


def _msh(path: Path) -> Path:
    path.write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    return path


def _png(path: Path, size: int = 256) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * size)
    return path


# artifact resolution
def test_a_required_artifact_resolves_by_manifest_key(tmp_path):
    _msh(tmp_path / "mesh.msh")
    r = ArtifactResolver(tmp_path, {"mesh_paths": {"surface": str(tmp_path / "mesh.msh")}})
    art = r.resolve(SURFACE)
    assert art is not None and art.path == (tmp_path / "mesh.msh").resolve()
    assert art.fmt is ArtifactFormat.GMSH_MSH


def test_a_missing_required_artifact_refuses_before_any_renderer_runs(tmp_path):
    r = ArtifactResolver(tmp_path, {})
    with pytest.raises(ReviewRenderError) as ei:
        r.resolve(SURFACE)
    assert ei.value.category is ReviewEvidenceFailure.EVIDENCE_MISSING


def test_a_required_artifact_named_but_absent_is_refused(tmp_path):
    r = ArtifactResolver(tmp_path, {"mesh_paths": {"surface": str(tmp_path / "gone.msh")}})
    with pytest.raises(ReviewRenderError):
        r.resolve(SURFACE)


def test_an_optional_artifact_absent_is_reported_not_fatal(tmp_path):
    r = ArtifactResolver(tmp_path, {})
    assert r.resolve(OPTIONAL) is None       # no raise: absence degrades, it does not fail


# confinement: the security boundary
def test_a_traversal_path_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = _msh(tmp_path / "secret.msh")
    r = ArtifactResolver(ws, {"mesh_paths": {"surface": "../secret.msh"}})
    with pytest.raises(ReviewRenderError, match="outside the job workspace"):
        r.resolve(SURFACE)
    assert outside.exists()                  # it exists; it is simply not reachable


def test_an_absolute_path_outside_the_workspace_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _msh(tmp_path / "elsewhere.msh")
    r = ArtifactResolver(ws, {"mesh_paths": {"surface": str(tmp_path / "elsewhere.msh")}})
    with pytest.raises(ReviewRenderError, match="outside the job workspace"):
        r.resolve(SURFACE)


def test_a_symlink_escape_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = _msh(tmp_path / "outside.msh")
    link = ws / "mesh.msh"
    link.symlink_to(target)
    assert link.is_file()                    # it reads fine - that is the danger
    r = ArtifactResolver(ws, {"mesh_paths": {"surface": str(link)}})
    with pytest.raises(ReviewRenderError, match="outside the job workspace"):
        r.resolve(SURFACE)


def test_an_in_workspace_symlink_is_allowed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    real = _msh(ws / "real.msh")
    link = ws / "mesh.msh"
    link.symlink_to(real)
    r = ArtifactResolver(ws, {"mesh_paths": {"surface": str(link)}})
    art = r.resolve(SURFACE)
    assert art is not None and art.path == real.resolve()


def test_the_error_names_the_key_not_the_filesystem_path(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _msh(tmp_path / "secret_layout.msh")
    r = ArtifactResolver(ws, {"mesh_paths": {"surface": str(tmp_path / "secret_layout.msh")}})
    with pytest.raises(ReviewRenderError) as ei:
        r.resolve(SURFACE)
    assert "mesh_paths.surface" in str(ei.value)
    assert "secret_layout" not in str(ei.value)


# format enforcement
def test_a_declared_format_contradicted_by_the_bytes_is_refused(tmp_path):
    bad = tmp_path / "mesh.msh"
    bad.write_bytes(b"PK\x03\x04not-a-mesh")
    r = ArtifactResolver(tmp_path, {"mesh_paths": {"surface": str(bad)}})
    with pytest.raises(ReviewRenderError, match="does not match any declared format"):
        r.resolve(SURFACE)


def test_the_extension_alone_does_not_establish_the_format(tmp_path):
    vtp = tmp_path / "lumen.vtp"
    vtp.write_bytes(b"$MeshFormat\n")        # gmsh content under a .vtp name
    r = ArtifactResolver(tmp_path, {"mesh_paths": {"lumen": str(vtp)}})
    with pytest.raises(ReviewRenderError, match="does not match any declared format"):
        r.resolve(OPTIONAL)


# the session
class _FakeInner:
    def __init__(self, ws: Path, *, fail_on=None):
        self.ws = ws
        self.opened = 1
        self.closes = 0
        self.opening_calls = 0
        self._fail_on = fail_on

    def capabilities(self):
        return RenderCapabilities(
            views=(), targets=(InspectionTarget(target_id="patch:wall", kind=TargetKind.PATCH,
                                                label="wall", purpose="p"),),
            entities=("wall",), operations=frozenset({CommandKind.TOGGLE_ENTITY}))

    def opening_evidence(self):
        self.opening_calls += 1
        return (EvidenceItem(evidence_id="open-0", seq=0, label="overview", purpose="p",
                             image_ref=str(_png(self.ws / "open0.png"))),)

    def execute(self, command):
        if self._fail_on == "execute":
            raise RuntimeError("renderer blew up")
        return EvidenceItem(evidence_id="f1", seq=1, label="wall", purpose="p",
                            image_ref=str(_png(self.ws / "f1.png")),
                            covers_target="patch:wall")

    def close(self):
        self.closes += 1


class _FakeSpec:
    def __init__(self, inner, artifacts=(SURFACE,)):
        self._inner = inner
        self.render_artifacts = artifacts

    @property
    def review_renderer(self):
        inner = self._inner

        class _R:
            def required_artifacts(self): return ()
            def open(self, ctx, artifacts):
                inner.received_artifacts = dict(artifacts)
                return inner
        return _R()


def _ctx(ws: Path) -> RenderContext:
    return RenderContext(workspace=str(ws), save_dir=str(ws),
                         manifest={"mesh_paths": {"surface": str(ws / "mesh.msh")}})


async def test_a_normal_session_opens_once_and_closes(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        assert not s.closed
    assert inner.closes == 1 and inner.opened == 1


async def test_opening_evidence_is_cached_not_re_rendered(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        a, b = s.opening_evidence(), s.opening_evidence()
    assert a == b and inner.opening_calls == 1


async def test_a_caller_exception_still_closes_the_session(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    with pytest.raises(ValueError):
        async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)):
            raise ValueError("the reviewer blew up")
    assert inner.closes == 1


async def test_a_renderer_exception_still_closes_the_session(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path, fail_on="execute")
    with pytest.raises(RuntimeError):
        async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
            s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))
    assert inner.closes == 1


async def test_cancellation_closes_the_session_and_still_cancels(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)

    async def _work():
        async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)):
            await asyncio.sleep(10)

    task = asyncio.create_task(_work())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.closes == 1


async def test_close_is_idempotent(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        s.close()
        s.close()
    assert inner.closes == 1


async def test_no_command_runs_after_close(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        s.close()
        with pytest.raises(ReviewRenderError, match="closed"):
            s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))
        with pytest.raises(ReviewRenderError, match="closed"):
            s.opening_evidence()


async def test_a_missing_required_artifact_prevents_open(tmp_path):
    inner = _FakeInner(tmp_path)
    with pytest.raises(ReviewRenderError):
        async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)):
            pass
    assert inner.closes == 0, "the renderer must not have been opened at all"


async def test_an_unsupported_command_fails_clearly(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        with pytest.raises(ReviewRenderError, match="does not support"):
            s.execute(RenderCommand(kind=CommandKind.APPLY_CLIP))


# evidence validation
class _BadEvidence(_FakeInner):
    def __init__(self, ws, ref):
        super().__init__(ws)
        self._ref = ref

    def execute(self, command):
        return EvidenceItem(evidence_id="bad", seq=1, label="x", purpose="p",
                            image_ref=self._ref, covers_target="patch:wall")


@pytest.mark.parametrize("make_ref,why", [
    (lambda tmp: str(tmp / "nope.png"), "missing file"),
    (lambda tmp: "", "empty reference"),
])
async def test_unusable_evidence_cannot_claim_coverage(make_ref, why, tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _BadEvidence(tmp_path, make_ref(tmp_path))
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        frame = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))
    assert frame.image_ref == "", f"{why}: coverage could advance on nothing"
    assert not frame.rendered


async def test_an_image_outside_the_sandbox_output_area_is_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _msh(ws / "mesh.msh")
    escaped = _png(tmp_path / "escaped.png")
    inner = _BadEvidence(ws, str(escaped))
    async with open_review_session(_FakeSpec(inner), _ctx(ws)) as s:
        frame = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))
    assert frame.image_ref == "" and any("escaped" in d for d in frame.diagnostics)


async def test_an_oversized_image_is_rejected(tmp_path):
    _msh(tmp_path / "mesh.msh")
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1))
    inner = _BadEvidence(tmp_path, str(big))
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        frame = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))
    assert frame.image_ref == ""


async def test_a_rejected_frame_is_demoted_not_dropped(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _BadEvidence(tmp_path, str(tmp_path / "gone.png"))
    async with open_review_session(_FakeSpec(inner), _ctx(tmp_path)) as s:
        frame = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))
    assert frame.evidence_id == "bad"                 # the frame survives
    assert frame.covers_target == "patch:wall"        # its intent survives
    assert frame.image_ref == ""                      # but it proves nothing
    assert frame.diagnostics                          # and says why


def test_the_lifecycle_imports_no_provider_or_model_code():
    import ast
    import inspect

    from meshpipeline.sandbox import review_session

    tree = ast.parse(inspect.getsource(review_session))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    for m in mods:
        assert "model_inference" not in m and "openai" not in m, f"lifecycle imports {m}"


async def test_an_engine_cannot_raise_the_image_ceiling(tmp_path):
    from meshpipeline.sandbox.review_session import MAX_IMAGES_PER_SESSION

    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    greedy = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path),
                           manifest={"mesh_paths": {"surface": str(tmp_path / "mesh.msh")}},
                           max_images=10_000)
    async with open_review_session(_FakeSpec(inner), greedy) as s:
        assert s._limits.max_images == MAX_IMAGES_PER_SESSION


async def test_an_engine_may_request_a_lower_image_ceiling(tmp_path):
    _msh(tmp_path / "mesh.msh")
    inner = _FakeInner(tmp_path)
    modest = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path),
                           manifest={"mesh_paths": {"surface": str(tmp_path / "mesh.msh")}},
                           max_images=3)
    async with open_review_session(_FakeSpec(inner), modest) as s:
        assert s._limits.max_images == 3
