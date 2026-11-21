"""The image channel for `look`: the investigator may SEE the region it is reasoning about.

Hermetic: `render_region` is a patched seam, so what is tested here are the RULES - the flag is
fail-closed and off means no rendering work at all, the caps hold per call and per region, a
render that gave nothing is text-first with the reason named, the transcript carries each image's
sha256 and never its bytes, and the image never spends the measured data's byte budget.
"""
from __future__ import annotations

import hashlib
import json

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import investigator_tools as it
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.regions import Region

ORIGINAL = Measure(valid=False, solids=2, shells=2, faces=12, edges=24, vertices=16,
                   diagonal_mm=100.0, max_tolerance_mm=1e-7, surface_area_mm2=600.0,
                   volume_mm3=1000.0)
#: A stand-in PNG. Not a real image: nothing here decodes it, and no test may write it anywhere.
PNG = b"\x89PNG\r\n\x1a\n" + b"pretend-pixels" * 64
#: The words the OFF answer must carry. A test that only asserts "no images arrived" passes
#: VACUOUSLY against a fail-open posture check, because the forbidden renderer's own refusal also
#: produces no images - so the discriminator has to be the reason, pinned literally.
OFF_REASON = "CAD_REPAIR_INVESTIGATOR_IMAGES is not 'live'"


def _defect(i: int) -> EntityDefect:
    return EntityDefect(
        code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
        entity_type="face", entity_index=i, message="gmsh could not mesh this face",
        measurements={"mesh_kind": "overlapping facets", "stage": "3D"},
        location={"bbox_min": (0.0, 0.0, float(i)), "bbox_max": (1.0, 1.0, float(i) + 1)},
        fingerprint=f"fp{i}")


def _faces(shape, indices):
    return {i: {"surface_type": "GeomAbs_Plane", "area_mm2": 4.0, "bbox_min": [0, 0, 0],
                "bbox_max": [1, 1, 1], "neighbours": 4} for i in indices}


def _workspace(n: int = 1) -> it.Workspace:
    found = tuple(_defect(i) for i in range(1, n + 1))
    region = Region(index=0, fingerprints=frozenset(d.fingerprint for d in found),
                    bbox_min=(0.0, 0.0, 0.0), bbox_max=(1.0, 1.0, 1.0),
                    face_indices=frozenset({1, 2}))
    return it.Workspace(region=region, defects=found, attempts=(), shape=object(),
                        original=ORIGINAL, baseline_shape=object(), face_reader=_faces)


@pytest.fixture(autouse=True)
def _no_kernel(monkeypatch):
    monkeypatch.setattr(it, "_cameras", lambda shape, defects: [
        {"centroid_mm": [0.0, 0.0, float(i)], "normal": [0.0, 0.0, 1.0], "extent_mm": 5.0}
        for i, _ in enumerate(defects)])


@pytest.fixture
def renders(monkeypatch):
    """`render_region` replaced by a recorder. Calls are the proof that rendering HAPPENED."""
    calls: list[dict] = []

    def fake(shape, cameras, *, size=(512, 512), budget_s: float):
        calls.append({"cameras": list(cameras), "size": tuple(size), "budget_s": budget_s})
        return [PNG + bytes([i]) for i in range(len(cameras))]

    monkeypatch.setattr(it, "render_region", fake)
    return calls


@pytest.fixture
def _no_render(monkeypatch):
    """Any rendering at all is the failure. Not a stub that returns nothing - a stub that SHOUTS."""
    def forbidden(*a, **k):
        raise AssertionError("render_region must not be reached")

    monkeypatch.setattr(it, "render_region", forbidden)


def _live(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR_IMAGES", "live", raising=False)


# --- the declaration -------------------------------------------------------------------------

def test_look_declares_a_boolean_view_and_stays_read_only():
    assert it.LOOK.args["view"] is bool
    assert it.LOOK.read_only and it.LOOK in it.READ_ONLY
    assert [spec.name for spec in it.READ_ONLY] == ["look", "measure", "probe"]
    assert it.ALL_TOOLS == it.READ_ONLY + it.EDITING
    look = next(s for s in it.tool_schemas() if s.name == "look")
    assert look.parameters["properties"]["view"] == {"type": "boolean"}
    assert look.parameters["required"] == []
    assert "view (boolean, optional)" in it.tool_prose()


@pytest.mark.parametrize(("args", "names"), [
    ({"view": "yes"}, ("'view'", "boolean")),
    ({"view": 1}, ("'view'", "boolean")),
    ({"views": True}, ("'views'", "view")),
])
def test_a_bad_view_argument_is_refused_by_name(args, names, _no_render):
    result = it.run_tool(_workspace(), "look", args, budget_s=5.0)
    assert not result.ok and result.data == {}
    for name in names:
        assert name in result.error, result.error


# --- fail-closed -----------------------------------------------------------------------------

def test_look_without_view_is_byte_identical_whatever_the_posture(monkeypatch, _no_render):
    """The flag cannot change a `look` that did not ask for a view - not one byte of it."""
    off = it.envelope(1, it.run_tool(_workspace(2), "look", {}, budget_s=5.0))
    _live(monkeypatch)
    live = it.envelope(1, it.run_tool(_workspace(2), "look", {}, budget_s=5.0))
    assert off == live
    assert "views" not in json.loads(off)["tool_result"]["data"]


def test_images_off_is_text_only_and_renders_nothing(_no_render):
    """`CAD_REPAIR_INVESTIGATOR_IMAGES` defaults off, so asking is answered - in text."""
    assert rcfg.CAD_REPAIR_INVESTIGATOR_IMAGES == "off"
    result = it.run_tool(_workspace(2), "look", {"view": True}, budget_s=5.0)
    assert result.ok and result.views == ()
    assert result.data["views"] == []
    assert OFF_REASON in result.data["views_reason"], result.data["views_reason"]
    # The measured evidence is untouched: text-first means the numbers still arrived.
    assert len(result.data["cameras"]) == 2
    assert result.data["observation"]["defects"]


@pytest.mark.parametrize("posture", ["", "on", "LIVE", "yes", "1"])
def test_only_the_exact_word_live_renders(monkeypatch, posture, _no_render):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR_IMAGES", posture, raising=False)
    result = it.run_tool(_workspace(), "look", {"view": True}, budget_s=5.0)
    assert result.views == () and result.data["views"] == []
    # The REASON, not just the emptiness: this is what a fail-open posture check gets wrong.
    assert OFF_REASON in result.data["views_reason"], result.data["views_reason"]


def test_a_missing_flag_attribute_is_read_as_off(monkeypatch, _no_render):
    monkeypatch.delattr(rcfg, "CAD_REPAIR_INVESTIGATOR_IMAGES", raising=False)
    result = it.run_tool(_workspace(), "look", {"view": True}, budget_s=5.0)
    assert result.views == () and result.data["views"] == []
    assert OFF_REASON in result.data["views_reason"], result.data["views_reason"]


# --- the caps --------------------------------------------------------------------------------

def test_at_most_two_images_per_look(monkeypatch, renders):
    _live(monkeypatch)
    assert it.IMAGES_PER_LOOK_MAX == 2
    result = it.run_tool(_workspace(5), "look", {"view": True}, budget_s=5.0)
    assert len(result.views) == 2
    # The cap is applied BEFORE the render, not after: only two cameras were ever drawn.
    assert len(renders[0]["cameras"]) == 2
    # ...and they are the first two cameras `look` showed, in that order.
    assert renders[0]["cameras"] == result.data["cameras"][:2]
    # All five cameras are still reported as numbers - the cap is on pixels, not evidence.
    assert len(result.data["cameras"]) == 5


def test_image_cap_per_region(monkeypatch, renders):
    """One workspace is one region, and the region's whole budget is six images."""
    _live(monkeypatch)
    assert it.IMAGES_PER_REGION_MAX == 6
    ws = _workspace(5)
    counts = [len(it.run_tool(ws, "look", {"view": True}, budget_s=5.0).views)
              for _ in range(5)]
    assert counts == [2, 2, 2, 0, 0]
    assert sum(counts) == it.IMAGES_PER_REGION_MAX
    assert len(renders) == 3  # the exhausted looks did no rendering work at all
    spent = it.run_tool(ws, "look", {"view": True}, budget_s=5.0)
    assert spent.ok and spent.data["views"] == []
    assert "6" in spent.data["views_reason"], spent.data["views_reason"]


# --- a render that gave nothing ---------------------------------------------------------------

def test_render_failure_is_text_only_with_reason(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(it, "render_region", lambda *a, **k: [])
    result = it.run_tool(_workspace(2), "look", {"view": True}, budget_s=5.0)
    assert result.ok and result.views == ()
    assert result.data["views"] == []
    assert "no view" in result.data["views_reason"], result.data["views_reason"]
    assert len(result.data["cameras"]) == 2


def test_a_raising_renderer_still_leaves_a_whole_look(monkeypatch):
    """`look` is the investigator's one orientation tool: it must survive its own camera."""
    _live(monkeypatch)
    monkeypatch.setattr(it, "render_region",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vtk died")))
    result = it.run_tool(_workspace(2), "look", {"view": True}, budget_s=5.0)
    assert result.ok and result.views == ()
    assert result.data["views"] == [] and result.data["views_reason"]
    assert len(result.data["cameras"]) == 2


def test_render_region_never_raises_and_gives_nothing_on_failure(monkeypatch):
    from meshpipeline.cad.repair import localize

    monkeypatch.setattr(localize, "_bounded",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no fork")))
    assert it.render_region(object(), [{"centroid_mm": [0, 0, 0], "normal": [0, 0, 1],
                                        "extent_mm": 1.0}], budget_s=1.0) == []
    monkeypatch.setattr(localize, "_bounded", lambda *a, **k: None)
    assert it.render_region(object(), [{"centroid_mm": [0, 0, 0], "normal": [0, 0, 1],
                                        "extent_mm": 1.0}], budget_s=1.0) == []


def test_render_region_asks_for_nothing_when_there_is_no_camera(monkeypatch):
    from meshpipeline.cad.repair import localize

    monkeypatch.setattr(localize, "_bounded",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked anyway")))
    assert it.render_region(object(), [], budget_s=1.0) == []


def test_render_region_runs_under_a_bounded_child(monkeypatch):
    """Not in this process, and not past the budget: the kernel and vtk go in `_bounded`."""
    from meshpipeline.cad.repair import localize

    seen: dict = {}

    def fake_bounded(fn, *args, budget_s):
        seen["fn"] = fn
        seen["args"] = args
        seen["budget_s"] = budget_s
        return [PNG]

    monkeypatch.setattr(localize, "_bounded", fake_bounded)
    cams = [{"centroid_mm": [0, 0, 0], "normal": [0, 0, 1], "extent_mm": 1.0}]
    assert it.render_region(object(), cams, size=(64, 64), budget_s=3.5) == [PNG]
    assert seen["fn"] is it._render_kernel
    assert seen["args"][1] == cams and seen["args"][2] == (64, 64)
    assert seen["budget_s"] == 3.5


# --- the transcript, and the byte budget -------------------------------------------------------

def test_transcript_records_hash_not_bytes(monkeypatch, renders):
    _live(monkeypatch)
    result = it.run_tool(_workspace(2), "look", {"view": True}, budget_s=5.0)
    assert len(result.views) == 2
    text = it.envelope(7, result)
    for i, blob in enumerate(result.views):
        entry = result.data["views"][i]
        assert entry["sha256"] == hashlib.sha256(blob).hexdigest()
        assert entry["bytes"] == len(blob)
        assert entry["camera"] == i
        assert entry["size"] == list(it.VIEW_SIZE)
        assert entry["sha256"] in text
    # The bytes themselves are nowhere in what is written down.
    assert b"pretend-pixels" not in text.encode()
    assert "\\u0089PNG" not in text and "PNG" not in text


def test_the_image_does_not_spend_the_results_byte_budget(monkeypatch):
    """The picture is orientation; the numbers are the evidence. They travel in DIFFERENT places,
    so a 400 kB view cannot evict a measurement."""
    _live(monkeypatch)
    big = [b"x" * 400_000, b"y" * 400_000]
    monkeypatch.setattr(it, "render_region", lambda *a, **k: list(big))
    result = it.run_tool(_workspace(2), "look", {"view": True}, budget_s=5.0)
    assert list(result.views) == big
    text = it.envelope(1, result)
    assert len(text.encode()) <= it.RESULT_BYTES_MAX
    data = json.loads(text)["tool_result"]["data"]
    assert len(data["views"]) == 2 and data["views"][0]["bytes"] == 400_000
    # This observation is big enough that `_shrink` always drops something - which is exactly why
    # the proof has to be an EQUALITY, not "nothing was dropped": strike the one view key and what
    # is left is the text-only `look`, item for item and omission count for omission count. 800 kB
    # of pixels cost not one measured item.
    monkeypatch.setattr(it, "render_region", _forbidden)
    plain = json.loads(it.envelope(1, it.run_tool(_workspace(2), "look", {}, budget_s=5.0)))
    assert {k: v for k, v in data.items() if k != "views"} == plain["tool_result"]["data"]
    assert [k for k in _keys(data) if k.endswith("_omitted")] == \
           [k for k in _keys(plain["tool_result"]["data"]) if k.endswith("_omitted")]


def _forbidden(*a, **k):
    raise AssertionError("render_region must not be reached")


def _keys(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_keys(item))
    return found
