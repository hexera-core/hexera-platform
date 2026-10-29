"""The reviewer's gates tested presence; these pin the four that now test substance.

Job 575f76e6 delivered a correct conjugate mesh - 2,520,586 fluid cells, 4,061,885 solid, both
passing checkMesh, a 346,456-face interface on each side - and the review came back
`verdict=passed rounds=8 blocking=[] improve=[] concerns=[]` having seen neither region. Two
sources were cut first (test_a_picture_of_nothing_is_not_evidence): a volume that reads as no
cells is not a volume, and a cut that shows nothing is a refusal. What survived those cuts was
the reviewer's own contract, where four separate gates answered a question about substance with
a fact about presence:

    spec.py            one patch of any kind discharged "every renderable boundary patch"
    base.py            an absent `mesh_paths.volume` STRING waived every region obligation
    unified.py         image_ok=True, covered=True, written as literals beside a PNG path
    review_session.py  the only emptiness check in the path was the PNG's byte size

Each is pinned here at the thing it now measures.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from meshpipeline.agents.reviewer.eligibility import (
    _check_obligation,
    missing_target_obligations,
)
from meshpipeline.agents.reviewer.unified import record_operation_evidence
from meshpipeline.contracts.evidence_ledger import EvidenceLedger, InspectionTargetRef
from meshpipeline.contracts.review_evidence import (
    EvidenceItem,
    InspectionTarget,
    TargetKind,
)
from meshpipeline.engines.registry import engine_names, get_spec

CONJUGATE = "cfmesh_conjugate"


def _manifest(regions=("fluid", "solid"), volume="VTK/fluid/case_0/internal.vtu",
              slices=("across_the_flow", "through_the_plate")):
    return {
        "mesh_paths": {"surface": "/ws/mesh.msh", "volume": volume},
        "quality": {"regions": [{"name": r, "type": r} for r in regions]},
        "inspection_regions": [{"name": s} for s in slices],
    }


def _obligations(manifest, engine=CONJUGATE):
    return get_spec(engine).expected_target_obligations(manifest, {}, "")


def _of_kind(manifest, kind, engine=CONJUGATE):
    return [o for o in _obligations(manifest, engine) if o.kind is kind]


# #
# 1. the patch obligation says every region, and now requires every region
# #
def test_the_patch_obligation_names_one_id_per_delivered_region():
    """It was kind-only with min_count falling through to 1, under a provenance string reading
    "every renderable boundary patch" - so ONE inspection of ONE entity discharged it. This
    engine's renderable entities are its two regions (review_surface writes one physical group
    per region), and the review that passed with `patches: ['solid']` is what that cost."""
    patch_obs = _of_kind(_manifest(), TargetKind.PATCH)
    assert len(patch_obs) == 1
    assert patch_obs[0].exact_ids == ("patch:fluid", "patch:solid")
    assert patch_obs[0].required_count() == 2


def test_the_region_names_come_off_the_delivered_mesh_not_the_export():
    """checkMesh's own per-region rows. The export is the thing that fails, so an obligation
    read off it could be discharged by the export dropping a region."""
    src = inspect.getsource(
        __import__("meshpipeline.engines.cfmesh_conjugate.spec", fromlist=["x"])._region_names)
    assert '(m.get("quality") or {}).get("regions")' in src


def test_one_patch_does_not_discharge_two():
    """The exact shape of the live defect: the solid's boundary inspected, the coolant's absent
    from the file entirely, and the obligation satisfied."""
    ledger = EvidenceLedger()
    ledger.add_target_inspection(InspectionTargetRef(TargetKind.PATCH, "patch:solid"),
                                 "toggle_patch", image_ok=True, covered=True)
    (patch_ob,) = _of_kind(_manifest(), TargetKind.PATCH)
    reasons = _check_obligation(patch_ob, ledger)
    assert len(reasons) == 1
    assert "patch:fluid" in reasons[0]
    assert "toggle_patch on 'fluid'" in reasons[0], "the order must name the tool's own argument"


def test_a_surface_carrying_one_region_is_refused_before_the_model_is_asked():
    """Discovery offers what loaded; the obligation names what the mesh has. A surface holding
    the solid alone comes up short and the review is EXPECTED_TARGET_MISSING."""
    obs = _of_kind(_manifest(), TargetKind.PATCH)
    half = missing_target_obligations(obs, {TargetKind.PATCH: {"patch:solid"}})
    assert any("patch:fluid" in p for p in half)
    both = missing_target_obligations(obs, {TargetKind.PATCH: {"patch:solid", "patch:fluid"}})
    assert both == []


def test_a_manifest_with_no_region_rows_falls_back_rather_than_inventing_names():
    obs = _of_kind(_manifest(regions=()), TargetKind.PATCH)
    assert obs and obs[0].exact_ids == ()
    assert "no per-region rows" in obs[0].provenance


# #
# 2. the volume waiver is the engine's to refuse
# #
def test_the_conjugate_region_obligation_survives_an_absent_volume():
    """A conformal split and one merged block agree on every number the gates print and differ
    only in a cut through the coolant, so a run with no volume has produced nothing this review
    can judge. The obligation stands and is refused as missing evidence."""
    assert get_spec(CONJUGATE).review_requires_the_interior is True
    assert _of_kind(_manifest(volume=None, slices=()), TargetKind.REGION)


@pytest.mark.parametrize(
    "engine", [n for n in engine_names()
               if any(t.kind is TargetKind.REGION for t in get_spec(n).inspection_targets)
               and not get_spec(n).review_requires_the_interior])
def test_an_engine_whose_interior_is_a_view_still_waives_it(engine):
    """The waiver is right where the boundaries and the measured quality still say whether the
    mesh is good; demanding a slice there ended reviews on meshes that passed every gate."""
    assert _of_kind(_manifest(volume=None, slices=()), TargetKind.REGION, engine) == []


def test_the_declaration_is_declared_and_not_derived_from_the_targets():
    """Every region engine declares `region:*` required, so deriving the flag from the targets
    would delete the waiver for all of them at once - the failure the waiver was added for."""
    assert all(any(t.kind is TargetKind.REGION and t.required
                   for t in get_spec(n).inspection_targets)
               for n in engine_names()
               if any(t.kind is TargetKind.REGION for t in get_spec(n).inspection_targets))
    assert sum(get_spec(n).review_requires_the_interior for n in engine_names()) == 1


# #
# 3. image_ok and covered are read off the evidence
# #
def _ev(**kw):
    base = {"evidence_id": "s-1", "seq": 1, "label": "l", "purpose": "p", "image_ref": "/x.png"}
    return EvidenceItem(**{**base, **kw})


_INV = {"patch:wall": InspectionTarget("patch:wall", TargetKind.PATCH, "wall", "p", True)}


def test_coverage_is_filed_from_the_two_statements_agreeing():
    """The command's own address and the session's coverage claim. Every renderer in src sets
    both; they are the only independent pair available, and `covered=True` was a literal."""
    ledger = EvidenceLedger()
    eid = record_operation_evidence(
        ledger, _ev(target_ref="wall", covers_target="patch:wall"), _INV, "toggle_patch")
    rec = ledger.get(eid)
    assert rec.image_ok and rec.covered and rec.usable


def test_a_frame_that_covers_one_thing_and_addressed_another_is_not_coverage():
    ledger = EvidenceLedger()
    eid = record_operation_evidence(
        ledger, _ev(target_ref="inlet", covers_target="patch:wall"), _INV, "toggle_patch")
    rec = ledger.get(eid)
    assert rec.image_ok and not rec.covered and not rec.usable


def test_image_ok_is_false_wherever_the_session_kept_no_frame():
    """review_session._validate clears image_ref for a frame that escaped the sandbox, is
    missing, is outside the size band, is past the view budget, or was measured blank - so an
    empty image_ref is the one place all five land, and it files an attempt, not an inspection."""
    ledger = EvidenceLedger()
    eid = record_operation_evidence(
        ledger, _ev(image_ref="", target_ref="wall", covers_target="patch:wall",
                    diagnostics=("rejected: the frame is blank",)), _INV, "toggle_patch")
    rec = ledger.get(eid)
    assert rec.category == "validation" and not rec.usable
    assert "blank" in rec.detail


def test_no_literal_is_written_into_the_ledger_from_this_recorder():
    """The AST, not the prose: the comment above the code quotes the two literals it replaced,
    and a substring search over the source finds those first."""
    tree = ast.parse(inspect.getsource(record_operation_evidence).lstrip())
    filed = [k for node in ast.walk(tree) if isinstance(node, ast.Call)
             for k in node.keywords if k.arg in ("image_ok", "covered")]
    assert filed, "the recorder files neither - has it stopped calling the ledger?"
    for k in filed:
        assert not isinstance(k.value, ast.Constant), (
            f"{k.arg} is filed as the literal {getattr(k.value, 'value', None)!r}")


# #
# 4. the emptiness check is on the pixels, not the bytes
# #
def _png(path: Path, w: int, h: int, drawn: int) -> Path:
    """A frame of flat background with `drawn` pixels of mesh in it.

    The drawn pixels go at the END. Writing them from index 0 put one on the frame's corner,
    which is where the first version of the measure read the background from - the measure
    inverted and a near-blank frame read as nearly all drawn. The fixture found that; the
    measure now takes the frame's most common colour instead.
    """
    arr = np.full((h, w, 3), 30, dtype=np.uint8)
    flat = arr.reshape(-1, 3)
    if drawn:
        flat[-drawn:] = (200, 210, 230)
    Image.fromarray(arr).save(path)
    return path


def test_a_full_resolution_blank_frame_is_measured_as_blank(tmp_path):
    """It clears the byte-size band by several KB, which was the only emptiness check in the
    path, and eight of them read as eight interior inspections."""
    from meshpipeline.sandbox.review_session import frame_shows_nothing

    blank = _png(tmp_path / "blank.png", 1280, 960, drawn=0)
    assert blank.stat().st_size > 1024, "the frame must be big enough to pass the byte band"
    why = frame_shows_nothing(blank)
    assert why is not None and "blank" in why and "nothing was drawn" in why


def test_a_frame_with_a_mesh_in_it_passes(tmp_path):
    from meshpipeline.sandbox.review_session import frame_shows_nothing

    assert frame_shows_nothing(_png(tmp_path / "drawn.png", 1280, 960, drawn=50_000)) is None


def test_the_floor_is_a_share_of_the_frame_and_not_a_quality_bar(tmp_path):
    """A legitimate view with the model small in frame is thousands of pixels and passes; the
    floor is 1 in 10,000, which on 1280x960 is 122 pixels and cannot be a mesh at any zoom."""
    from meshpipeline.sandbox.review_session import (
        EMPTY_FRAME_PIXEL_SHARE,
        frame_shows_nothing,
    )

    assert EMPTY_FRAME_PIXEL_SHARE <= 1e-4
    assert frame_shows_nothing(_png(tmp_path / "small.png", 1280, 960, drawn=5_000)) is None
    assert frame_shows_nothing(_png(tmp_path / "speck.png", 1280, 960, drawn=40)) is not None


def test_a_frame_whose_corner_is_on_the_model_is_not_called_blank(tmp_path):
    """The first version of the measure read the background off the top-left pixel. A view
    framed so the mesh reaches that corner inverted it: nearly every pixel differed from the
    corner's mesh colour, so a half-empty frame read as full - and, the other way round, a
    frame that is all mesh reads as all background and would have been refused."""
    from meshpipeline.sandbox.review_session import frame_shows_nothing

    arr = np.full((480, 640, 3), 30, dtype=np.uint8)
    arr[:240, :320] = (200, 210, 230)        # the model fills the corner, a quarter of the frame
    path = tmp_path / "corner.png"
    Image.fromarray(arr).save(path)
    assert frame_shows_nothing(path) is None


def test_a_frame_that_cannot_be_measured_is_not_called_blank(tmp_path):
    """A measurement that cannot be taken is not evidence of blankness - the checks that own
    those cases (missing, unreadable, out of band) are already above it."""
    from meshpipeline.sandbox.review_session import frame_shows_nothing

    junk = tmp_path / "junk.png"
    junk.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    assert frame_shows_nothing(junk) is None
    assert frame_shows_nothing(tmp_path / "absent.png") is None


def test_the_session_clears_the_image_ref_of_a_blank_frame(tmp_path):
    """End of the path: the demoted item carries no image, so record_operation_evidence files a
    validation record and the coverage floor cannot count it."""
    from meshpipeline.sandbox.review_session import ReviewSession, _Limits

    _png(tmp_path / "001.png", 640, 480, drawn=0)
    session = ReviewSession.__new__(ReviewSession)
    session._root = tmp_path.resolve()
    session._limits = _Limits(max_images=8, max_image_bytes=12 * 1024 * 1024)
    session._images = 0
    out = session._validate(_ev(image_ref=str(tmp_path / "001.png"),
                                target_ref="across_the_flow",
                                covers_target="region:across_the_flow"))
    assert out.image_ref == ""
    assert any("blank" in d for d in out.diagnostics)


# #
# the invariant the coverage cross-check leans on
# #
_SRC = Path(inspect.getsourcefile(get_spec(CONJUGATE).__class__)).parents[1]


def test_every_renderer_that_claims_coverage_also_says_what_it_addressed():
    """`covered` is filed from target_ref and covers_target agreeing, so a renderer that sets
    only the second would lose all its coverage - silently, and in the safe direction, but
    silently. The rule is pinned at the source rather than in the fakes."""
    offenders = []
    for py in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8", errors="replace"))):
            if not isinstance(node, ast.Call):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            cov = kw.get("covers_target")
            if cov is None:
                continue
            if isinstance(cov, ast.Constant) and not cov.value:
                continue                      # covers_target="" - claims nothing
            if "target_ref" not in kw:
                offenders.append(f"{py.relative_to(_SRC).as_posix()}:{node.lineno}")
    assert offenders == [], (
        f"these claim coverage without saying what they addressed: {offenders}")
