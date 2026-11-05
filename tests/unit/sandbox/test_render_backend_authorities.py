# Responsibility: Verify the camera maths, frame capture and mesh reading each behave as their own leaf authority.
from __future__ import annotations

import ast
import base64
import inspect
import logging
import subprocess
from pathlib import Path

import numpy as np
import pytest
from tests._scan import scanned

from meshpipeline.sandbox import backend as B
from meshpipeline.sandbox import camera_math as CM
from meshpipeline.sandbox import capture as CAP
from meshpipeline.sandbox import mesh_reader as MR

SANDBOX = Path(inspect.getsourcefile(B)).parent
RENDER_PATH = ("backend.py", "mesh_reader.py", "capture.py", "camera_math.py")


def _tree(name: str) -> ast.AST:
    return ast.parse((SANDBOX / name).read_text())


# camera_math: pure maths, and the degenerate cases it must not blow up on
def test_bounds_center_span_is_the_centre_and_the_longest_side():
    center, span = CM.bounds_center_span((0.0, 2.0, 0.0, 4.0, 0.0, 6.0))
    assert list(center) == [1.0, 2.0, 3.0]
    assert span == 6.0


def test_a_degenerate_bounding_box_still_yields_a_usable_span():
    center, span = CM.bounds_center_span((5.0, 5.0, 5.0, 5.0, 5.0, 5.0))
    assert list(center) == [5.0, 5.0, 5.0]
    assert span > 0.0, "a zero span would put the camera on top of the model"


def test_camera_right_is_perpendicular_to_the_view_direction_and_unit_length():
    right = CM.camera_right(position=(0.0, -10.0, 0.0), focal_point=(0.0, 0.0, 0.0),
                            up=(0.0, 0.0, 1.0))
    assert np.isclose(np.linalg.norm(right), 1.0)
    assert np.isclose(np.dot(right, np.array([0.0, 1.0, 0.0])), 0.0)


def test_camera_right_survives_an_up_vector_parallel_to_the_view():
    right = CM.camera_right(position=(0.0, 0.0, 10.0), focal_point=(0.0, 0.0, 0.0),
                            up=(0.0, 0.0, 1.0))
    assert np.all(np.isfinite(right)), "a degenerate camera produced a non-finite right vector"
    assert np.isclose(np.linalg.norm(right), 1.0)


def test_camera_true_up_is_orthogonalised_against_the_view_direction():
    true_up = CM.camera_true_up(position=(0.0, -10.0, 0.0), focal_point=(0.0, 0.0, 0.0),
                                up=(0.0, 0.7, 0.7))
    view = np.array([0.0, 1.0, 0.0])
    assert np.isclose(np.dot(true_up, view), 0.0, atol=1e-9), "the up vector is not square"
    assert np.isclose(np.linalg.norm(true_up), 1.0)


def test_rodrigues_rotates_a_quarter_turn_about_z():
    out = CM.rodrigues(np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0]), np.pi / 2)
    assert np.allclose(out, [0.0, 1.0, 0.0], atol=1e-9)


def test_rodrigues_of_zero_degrees_is_the_identity():
    v = np.array([3.0, -1.0, 2.0])
    assert np.allclose(CM.rodrigues(v, np.array([0.0, 0.0, 1.0]), 0.0), v)


def test_rodrigues_preserves_length():
    v = np.array([3.0, -1.0, 2.0])
    out = CM.rodrigues(v, np.array([1.0, 1.0, 0.0]), 1.234)
    assert np.isclose(np.linalg.norm(out), np.linalg.norm(v))


def test_rodrigues_survives_a_zero_length_axis():
    v = np.array([1.0, 2.0, 3.0])
    out = CM.rodrigues(v, np.array([0.0, 0.0, 0.0]), 0.5)
    assert np.all(np.isfinite(out)), "a zero axis produced a non-finite rotation"


def test_camera_math_is_a_leaf_with_no_project_or_render_dependency():
    banned = ("meshpipeline", "vtk", "pyvista", "gmsh", "PIL")
    for node in ast.walk(_tree("camera_math.py")):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [mod]
            for name in names:
                assert not any(name.startswith(b) for b in banned), (
                    f"camera_math imports {name} - it is no longer a leaf")


def test_camera_math_holds_no_state():
    assert not [n for n in ast.parse((SANDBOX / "camera_math.py").read_text()).body
                if isinstance(n, ast.ClassDef)], "camera_math grew a class - it is maths, not state"


# capture: numbering, saving that never fails a review, and a bounded video
def test_capture_numbers_frames_from_one_and_zero_pads_them(tmp_path):
    cap = CAP.SessionCapture(tmp_path)
    for _ in range(3):
        cap.save_and_encode(b"png-bytes")
    assert cap.shot_count == 3
    assert sorted(p.name for p in tmp_path.glob("*.png")) == ["001.png", "002.png", "003.png"]
    assert cap.last_shot_path == tmp_path / "003.png"


def test_capture_creates_its_own_save_directory(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    cap = CAP.SessionCapture(target)
    assert target.is_dir(), "capture did not create the directory it writes into"
    cap.save_and_encode(b"x")
    assert (target / "001.png").read_bytes() == b"x"


def test_capture_returns_the_encoded_image_even_with_no_save_directory():
    cap = CAP.SessionCapture(None)
    encoded = cap.save_and_encode(b"png-bytes")
    assert base64.b64decode(encoded) == b"png-bytes"
    assert cap.last_shot_path is None
    assert cap.shot_count == 1, "the frame still counts even when it is not written"


def test_a_save_failure_does_not_fail_the_review(tmp_path, caplog):
    cap = CAP.SessionCapture(tmp_path)
    tmp_path.chmod(0o500)
    try:
        encoded = cap.save_and_encode(b"png-bytes")
    finally:
        tmp_path.chmod(0o700)
    assert base64.b64decode(encoded) == b"png-bytes", "an unwritable directory ended the review"
    assert any("could not save screenshot" in r.getMessage() for r in caplog.records)


def test_a_failed_write_does_not_leave_the_previous_frame_reported_as_the_last_shot(tmp_path):
    cap = CAP.SessionCapture(tmp_path)
    cap.save_and_encode(b"first")
    assert cap.last_shot_path == tmp_path / "001.png"

    tmp_path.chmod(0o500)
    try:
        cap.save_and_encode(b"second")
    finally:
        tmp_path.chmod(0o700)
    assert cap.last_shot_path is None, (
        f"a failed write left {cap.last_shot_path} - the previous frame reported as the last shot")


def test_an_uncreatable_save_directory_does_not_fail_construction(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"i am a file, not a directory")
    cap = CAP.SessionCapture(blocker / "frames")     # must not raise
    assert base64.b64decode(cap.save_and_encode(b"x")) == b"x"


def test_the_video_is_a_no_op_without_frames_or_a_directory(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(CAP.subprocess, "run", lambda *a, **k: calls.append(a))

    CAP.SessionCapture(tmp_path).assemble_video()          # a directory, but no frames
    CAP.SessionCapture(None).assemble_video()              # frames impossible, no directory
    assert calls == [], "ffmpeg was invoked with nothing to assemble"


def test_the_video_is_invoked_as_an_explicit_argv_under_a_bounded_timeout(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(CAP.subprocess, "run", fake_run)
    cap = CAP.SessionCapture(tmp_path)
    cap.save_and_encode(b"frame")
    cap.assemble_video()

    assert isinstance(seen["cmd"], list) and seen["cmd"][0] == "ffmpeg", "not an explicit argv"
    assert seen["kwargs"].get("shell") in (None, False), "the video is assembled through a shell"
    assert seen["kwargs"]["timeout"] == CAP.VIDEO_TIMEOUT_S, "ffmpeg can hang the review"
    assert seen["kwargs"]["check"] is True
    assert str(tmp_path / CAP.VIDEO_NAME) in seen["cmd"]


def test_an_ffmpeg_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    def boom(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"codec exploded")

    monkeypatch.setattr(CAP.subprocess, "run", boom)
    cap = CAP.SessionCapture(tmp_path)
    cap.save_and_encode(b"frame")
    cap.assemble_video()                                    # must not raise
    assert any("ffmpeg failed" in r.getMessage() for r in caplog.records)


def test_an_ffmpeg_timeout_is_logged_not_raised(tmp_path, monkeypatch):
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, CAP.VIDEO_TIMEOUT_S)

    monkeypatch.setattr(CAP.subprocess, "run", slow)
    cap = CAP.SessionCapture(tmp_path)
    cap.save_and_encode(b"frame")
    cap.assemble_video()                                    # must not raise


class _FakePlotter:

    camera_set = False
    image_scale = 1

    class _RW:
        def Render(self):
            pass

    render_window = _RW()

    def screenshot(self, return_img=True, **kwargs):
        return np.zeros((4, 4, 4), dtype=np.uint8)          # RGBA, to prove alpha is dropped


def test_a_screenshot_is_one_numbered_frame_and_one_encoded_image(tmp_path):
    cap = CAP.SessionCapture(tmp_path)
    encoded = cap.screenshot(_FakePlotter())
    assert cap.shot_count == 1, "one screenshot must advance the counter exactly once"
    assert base64.b64decode(encoded)[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert (tmp_path / "001.png").exists()


def test_the_alpha_channel_is_dropped_from_a_captured_frame():
    img = CAP.screenshot_array(_FakePlotter())
    assert img.shape == (4, 4, 3), "RGBA reached the encoder"


def test_a_closed_plotter_raises_rather_than_returning_an_empty_frame():
    class Closed(_FakePlotter):
        render_window = None

    with pytest.raises(RuntimeError, match="render window"):
        CAP.screenshot_array(Closed())


def test_a_plotter_that_returns_no_image_raises():
    class Empty(_FakePlotter):
        def screenshot(self, return_img=True, **kwargs):
            return None

    with pytest.raises(RuntimeError, match="no image"):
        CAP.screenshot_array(Empty())


# mesh_reader: what the file says, and what a damaged file does
class _FakeGmsh:

    def __init__(self, initialized=False, bbox=(0.0, 0.0, 0.0, 2.0, 4.0, 6.0)):
        self._initialized = initialized
        self._bbox = bbox
        self.finalized = False
        self.initialized_by_us = False
        self.merged: list[str] = []
        outer = self

        class _Option:
            def setNumber(self, *a):
                pass

        class _Mesh:
            def getNodes(self):
                return (np.array([1, 2, 3]),
                        np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]),
                        None)

            def getElements(self, dim, tag):
                return ([2], None, [np.array([1, 2, 3])])

        class _Model:
            mesh = _Mesh()

            def remove(self):
                pass

            def add(self, name):
                pass

            def getBoundingBox(self, dim, tag):
                if outer._bbox is None:
                    raise RuntimeError("no bounding box")
                return outer._bbox

            def getPhysicalGroups(self, dim=None):
                return [(2, 7)] if dim in (None, 2) else []

            def getPhysicalName(self, dim, tag):
                return "inlet"

            def getEntitiesForPhysicalGroup(self, dim, tag):
                return [11]

        self.option = _Option()
        self.model = _Model()

    def isInitialized(self):
        return self._initialized

    def initialize(self, **kwargs):
        self.initialized_by_us = True
        self._initialized = True

    def merge(self, path):
        self.merged.append(path)

    def finalize(self):
        self.finalized = True


def test_read_mesh_returns_the_facts_a_scene_is_built_from(monkeypatch):
    fake = _FakeGmsh()
    monkeypatch.setattr(MR, "gmsh", fake)
    loaded = MR.read_mesh("/ws/mesh.msh", "mm")

    assert fake.merged == ["/ws/mesh.msh"], "the reader opened something other than its argument"
    assert loaded.bbox == {"xmin": 0.0, "xmax": 2.0, "ymin": 0.0, "ymax": 4.0,
                           "zmin": 0.0, "zmax": 6.0, "units": "mm"}
    assert loaded.entity_tags_by_name == {"inlet": [11]}
    assert loaded.physical_groups == [(2, 7, "inlet", (11,))]
    assert set(loaded.patch_meshes) == {"inlet"}
    assert loaded.patch_meshes["inlet"].n_cells == 1


def test_the_pan_step_is_a_tenth_of_the_longest_side(monkeypatch):
    monkeypatch.setattr(MR, "gmsh", _FakeGmsh(bbox=(0.0, 0.0, 0.0, 2.0, 4.0, 60.0)))
    assert MR.read_mesh("/ws/mesh.msh", "mm").pan_step == 6.0
    assert MR.PAN_STEP_FRACTION == 0.10


def test_a_bounding_box_that_will_not_query_degrades_rather_than_failing(monkeypatch, caplog):
    monkeypatch.setattr(MR, "gmsh", _FakeGmsh(bbox=None))
    loaded = MR.read_mesh("/ws/mesh.msh", "mm")
    assert loaded.bbox == {}
    assert loaded.pan_step == 1.0
    assert set(loaded.patch_meshes) == {"inlet"}, "a missing bbox lost the geometry"
    assert any("bbox query failed" in r.getMessage() for r in caplog.records)


def test_the_reader_finalises_only_the_session_it_opened(monkeypatch):
    ours = _FakeGmsh(initialized=False)
    monkeypatch.setattr(MR, "gmsh", ours)
    MR.read_mesh("/ws/mesh.msh", "mm")
    assert ours.initialized_by_us and ours.finalized, "the reader leaked the session it opened"

    theirs = _FakeGmsh(initialized=True)
    monkeypatch.setattr(MR, "gmsh", theirs)
    MR.read_mesh("/ws/mesh.msh", "mm")
    assert not theirs.finalized, "the reader finalised a session it did not open"


def test_a_failed_read_still_releases_the_session(monkeypatch):
    fake = _FakeGmsh()
    fake.merge = lambda path: (_ for _ in ()).throw(RuntimeError("corrupt mesh"))
    monkeypatch.setattr(MR, "gmsh", fake)
    with pytest.raises(RuntimeError, match="corrupt mesh"):
        MR.read_mesh("/ws/mesh.msh", "mm")
    assert fake.finalized, "a failed read leaked the gmsh session into the next render"


def test_malformed_node_coordinates_are_refused_by_name(monkeypatch):
    fake = _FakeGmsh()
    fake.model.mesh.getNodes = lambda: (np.array([1]), np.array([0.0, 1.0]), None)
    monkeypatch.setattr(MR, "gmsh", fake)
    with pytest.raises(ValueError, match=r"not divisible by 3.*/ws/mesh\.msh"):
        MR.read_mesh("/ws/mesh.msh", "mm")


def test_elements_referencing_undeclared_nodes_are_dropped_not_crashed(monkeypatch, caplog):
    fake = _FakeGmsh()
    fake.model.mesh.getElements = lambda dim, tag: ([2], None, [np.array([1, 2, 9999])])
    monkeypatch.setattr(MR, "gmsh", fake)
    loaded = MR.read_mesh("/ws/mesh.msh", "mm")
    assert loaded.patch_meshes == {}, "an out-of-range element became geometry"
    assert any("out-of-range node tags" in r.getMessage() for r in caplog.records)


def test_an_element_block_of_the_wrong_width_is_skipped(monkeypatch, caplog):
    fake = _FakeGmsh()
    fake.model.mesh.getElements = lambda dim, tag: ([2], None, [np.array([1, 2, 3, 1])])
    monkeypatch.setattr(MR, "gmsh", fake)
    assert MR.read_mesh("/ws/mesh.msh", "mm").patch_meshes == {}
    assert any("not divisible by" in r.getMessage() for r in caplog.records)


def test_an_unreadable_surface_does_not_lose_the_rest_of_the_patch(monkeypatch, caplog):
    fake = _FakeGmsh()
    fake.model.getEntitiesForPhysicalGroup = lambda dim, tag: [11, 12]

    def flaky(dim, tag):
        if tag == 11:
            raise RuntimeError("surface unreadable")
        return ([2], None, [np.array([1, 2, 3])])

    fake.model.mesh.getElements = flaky
    monkeypatch.setattr(MR, "gmsh", fake)
    loaded = MR.read_mesh("/ws/mesh.msh", "mm")
    assert loaded.patch_meshes["inlet"].n_cells == 1, "one bad surface lost the whole patch"
    assert any("Could not get elements" in r.getMessage() for r in caplog.records)


def test_an_empty_element_block_is_skipped(monkeypatch):
    fake = _FakeGmsh()
    fake.model.mesh.getElements = lambda dim, tag: ([2], None, [np.array([])])
    monkeypatch.setattr(MR, "gmsh", fake)
    assert MR.read_mesh("/ws/mesh.msh", "mm").patch_meshes == {}


def test_elements_referencing_a_gap_in_the_node_tags_are_dropped(monkeypatch, caplog):
    fake = _FakeGmsh()
    fake.model.mesh.getNodes = lambda: (
        np.array([1, 2, 5]),
        np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]),
        None)
    fake.model.mesh.getElements = lambda dim, tag: ([2], None, [np.array([1, 2, 3])])
    monkeypatch.setattr(MR, "gmsh", fake)
    assert MR.read_mesh("/ws/mesh.msh", "mm").patch_meshes == {}, "an unmapped tag became node 0"
    assert any("unmapped node tags" in r.getMessage() for r in caplog.records)


def test_a_model_that_will_not_clear_does_not_stop_the_read(monkeypatch, caplog):
    fake = _FakeGmsh()
    fake.model.remove = lambda: (_ for _ in ()).throw(RuntimeError("nothing to remove"))
    monkeypatch.setattr(MR, "gmsh", fake)
    caplog.set_level(logging.DEBUG, logger=MR.logger.name)
    assert set(MR.read_mesh("/ws/mesh.msh", "mm").patch_meshes) == {"inlet"}
    assert any("gmsh.model.remove() skipped" in r.getMessage() for r in caplog.records)


def test_a_session_that_will_not_finalise_still_returns_the_mesh(monkeypatch, caplog):
    fake = _FakeGmsh()
    fake.finalize = lambda: (_ for _ in ()).throw(RuntimeError("gmsh is wedged"))
    monkeypatch.setattr(MR, "gmsh", fake)
    caplog.set_level(logging.DEBUG, logger=MR.logger.name)
    assert set(MR.read_mesh("/ws/mesh.msh", "mm").patch_meshes) == {"inlet"}
    assert any("gmsh.finalize() failed" in r.getMessage() for r in caplog.records)


def test_an_unknown_element_type_is_ignored_rather_than_guessed(monkeypatch):
    fake = _FakeGmsh()
    fake.model.mesh.getElements = lambda dim, tag: ([4], None, [np.array([1, 2, 3, 1])])
    monkeypatch.setattr(MR, "gmsh", fake)
    assert MR.read_mesh("/ws/mesh.msh", "mm").patch_meshes == {}


def test_the_supported_element_types_are_pinned():
    assert MR.PATCH_ELEMENT_TYPES == {2: (3, 3), 3: (4, 4), 9: (6, 3), 16: (8, 4), 10: (9, 4)}


def test_the_loaded_mesh_is_frozen():
    import dataclasses

    assert dataclasses.fields(MR.LoadedMesh)
    with pytest.raises(dataclasses.FrozenInstanceError):
        MR.LoadedMesh().pan_step = 99.0


# the split itself: one home per job, and one way to build a backend
#: symbol -> the module that owns it. A second definition anywhere in the tree is a copy.
OWNERS = {
    "read_mesh": "sandbox/mesh_reader.py",
    "extract_patch_meshes": "sandbox/mesh_reader.py",
    "LoadedMesh": "sandbox/mesh_reader.py",
    "bounds_center_span": "sandbox/camera_math.py",
    "camera_right": "sandbox/camera_math.py",
    "camera_true_up": "sandbox/camera_math.py",
    "rodrigues": "sandbox/camera_math.py",
    "SessionCapture": "sandbox/capture.py",
    "screenshot_array": "sandbox/capture.py",
    "MeshRenderBackend": "sandbox/backend.py",
}


@pytest.mark.parametrize("symbol,owner", sorted(OWNERS.items()))
def test_each_moved_symbol_has_exactly_one_definition(symbol, owner):
    src_root = SANDBOX.parents[1]          # src/
    homes = []
    for py in scanned(sorted(src_root.rglob("*.py")), "the shipped source tree"):
        for node in ast.parse(py.read_text(encoding="utf-8", errors="replace")).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == symbol:
                homes.append(py.relative_to(src_root).as_posix())
    assert homes == [f"meshpipeline/{owner}"], f"{symbol} is defined in {homes}"


#: What the split removed from `backend.py`. Each was either moved to an authority or was a
#: one-line forward to one; re-adding any of them puts a second path to the same job back.
REMOVED_FROM_BACKEND = ("_screenshot_array", "_extract_patch_meshes", "_bounds_center_span",
                        "_cam_right", "_cam_true_up", "_rodrigues", "_render_png_bytes",
                        "_save_and_encode", "_assemble_video")


@pytest.mark.parametrize("name", REMOVED_FROM_BACKEND)
def test_the_backend_does_not_re_export_what_it_gave_away(name):
    assert not hasattr(B, name), f"backend re-exports {name} - a compatibility shim came back"
    assert not hasattr(B.MeshRenderBackend, name), (
        f"MeshRenderBackend.{name} is back - it forwards to an authority that already owns it")


def test_gmsh_is_reached_through_exactly_one_module_of_the_render_path():
    users = [n for n in RENDER_PATH
             if any(isinstance(node, ast.Import) and any(a.name == "gmsh" for a in node.names)
                    for node in ast.walk(_tree(n)))]
    assert users == ["mesh_reader.py"], f"gmsh is imported by {users}"


def test_the_render_path_runs_exactly_one_subprocess_and_capture_owns_it():
    users = [n for n in RENDER_PATH
             if any(isinstance(node, ast.Import) and any(a.name == "subprocess" for a in node.names)
                    for node in ast.walk(_tree(n)))]
    assert users == ["capture.py"], f"subprocess is reachable from {users}"


def test_the_public_surface_of_the_backend_is_unchanged_by_the_split():
    public = {n for n in dir(B.MeshRenderBackend) if not n.startswith("_")} | {"__init__"}
    assert public == {
        "__init__", "close", "get_navigation_context", "get_patch_colour_legend",
        "go_to_coordinates", "has_geometry", "inspect_region", "last_shot_path", "move_camera",
        "patch_bounds", "patch_names", "physical_groups", "reset_view", "rotate_camera",
        "scene_facts", "set_camera_preset", "set_navigation_defaults", "take_screenshot",
        "toggle_patch", "zoom", "zoom_to_region"}


def test_last_shot_path_is_a_property_not_a_method():
    assert isinstance(B.MeshRenderBackend.__dict__["last_shot_path"], property), (
        "last_shot_path is no longer a property; its two consumers would receive a bound method")


def test_the_capture_authority_is_what_last_shot_path_reports(tmp_path):
    cap = CAP.SessionCapture(tmp_path)
    assert cap.last_shot_path is None
    cap.save_and_encode(b"frame")
    assert cap.last_shot_path == tmp_path / "001.png"


def test_the_render_adapter_is_the_only_place_a_backend_is_constructed():
    src_root = SANDBOX.parents[1]          # src/
    sites = []
    for py in sorted(src_root.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8", errors="replace"))):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("MeshRenderBackend"):
                sites.append(f"{py.relative_to(src_root).as_posix()}:{node.lineno}")
    assert len(sites) == 1 and sites[0].startswith("meshpipeline/sandbox/render_adapter.py"), (
        f"MeshRenderBackend is constructed at {sites} - build_backend() is not the only way in")


def test_every_engine_reaches_the_backend_through_that_one_factory():
    engines = SANDBOX.parents[0] / "engines"
    renderers = sorted(engines.glob("*/review_renderer.py"))
    assert len(renderers) >= 5, f"expected five engine renderers, found {len(renderers)}"
    for py in renderers:
        src = py.read_text()
        assert "build_backend" in src, f"{py.parent.name} does not use the backend factory"
        assert "MeshRenderBackend(" not in src, (
            f"{py.parent.name} constructs the concrete backend directly")
