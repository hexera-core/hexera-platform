# Responsibility: Verify TIME-10: the second reading of a file's loose faces (fea_driver._solidified) runs in a process
# of its own and is stopped at its limit, the plain stitch's reading then standing and the run told; the closed-skin
# check (SURF-14) stops its extraction at its own limit and the file goes on as the solid its surfaces close round.
# Boundaries: small synthetic shapes; the bounded child is real in one test and faked as stopped in the others; no
# mesher, no store.
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")

from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from tests import surface_fixtures as sx  # noqa: E402
from tests.unit.engines.test_surface_run import _stage  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.contracts.decision_record import RULES_VERSION  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import surface_run  # noqa: E402


def test_the_volume_maker_beside_the_run_makes_the_same_volumes_as_here():
    # a 40 mm box written as its six faces: the bounded child's volumes are the in-process ones
    from meshpipeline.cad.scout import read_cad

    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), Path(tempfile.mkdtemp()) / "ws")
    faces = ms._faces(read_cad(FD.staged_solid(ws)))
    here, there = ms._volumes_of(faces, 1e-7), ms._volumes_bounded(faces, 1e-7, 120.0)
    assert len(here) == len(there) == 1
    assert [ms._map(v, TopAbs_FACE).Extent() for v in there] == [ms._map(v, TopAbs_FACE).Extent() for v in here]
    assert abs(ms._volume(there[0]) - ms._volume(here[0])) <= 1e-12 * abs(ms._volume(here[0]))


def test_a_volume_maker_past_its_limit_stops_the_second_reading(monkeypatch):
    # open surfaces: the sewing closes no body, so the volume maker runs - here faked as stopped at its limit
    calls = []

    def stopped(entry, kwargs, deadline_s):
        calls.append((entry, deadline_s))
        return "timeout", None

    monkeypatch.setattr(ms, "run_bounded", stopped)
    with pytest.raises(ms.VolumesStopped):
        ms.solidify(sx.bracket(), 1.0, volumes_limit_s=FD.SOLIDIFY_LIMIT_S)
    assert calls == [(ms.VOLUMES_ENTRY, FD.SOLIDIFY_LIMIT_S)] and FD.SOLIDIFY_LIMIT_S == 300.0


def test_a_stopped_second_reading_leaves_the_plain_stitch_and_is_told(tmp_path, monkeypatch):
    def stopped(shape, mm, *, volumes_limit_s=None):
        assert volumes_limit_s == FD.SOLIDIFY_LIMIT_S
        raise ms.VolumesStopped("stopped")

    monkeypatch.setattr(ms, "solidify", stopped)
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 1                    # the plain stitch's solid stands
    assert seen == {"closed": 1, "solidify_unfinished": FD.SOLIDIFY_LIMIT_S}
    words = FD.solidify_unfinished_words()
    assert "did not finish in 5 minutes" in words and "plain stitching" in words and "goes ahead" in words


def test_the_closed_skin_check_stops_at_its_limit_and_is_not_a_skin(tmp_path, monkeypatch):
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    assert FD.solids_from_shells(ws, {}) == 1
    asked = []

    def stopped(path, out_dir=None, *, mm_per_unit, deadline_s=None, **_k):
        asked.append(deadline_s)
        why = f"the mid-surface extraction ran past {deadline_s:.0f} s and was stopped"
        return {"bodies": [{"body": "Part_1", "verdict": "solid", "why": why}]}

    monkeypatch.setattr(ms, "extract_file", stopped)
    told: dict = {}
    assert surface_run.closed_skin_check(ws, told) is False
    assert asked == [surface_run.CLOSED_SKIN_LIMIT_S] and surface_run.CLOSED_SKIN_LIMIT_S == 300.0
    assert told == {"unfinished": surface_run.CLOSED_SKIN_LIMIT_S}
    assert "did not finish in 5 minutes" in surface_run.CLOSED_SKIN_UNFINISHED


def test_a_finished_closed_skin_check_is_not_told(tmp_path, monkeypatch):
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    assert FD.solids_from_shells(ws, {}) == 1
    no_plate = {"bodies": [{"body": "Part_1", "verdict": "solid", "why": surface_run.CLOSED_SKIN_NO_PLATE}]}
    monkeypatch.setattr(ms, "extract_file", lambda *a, **k: no_plate)
    told: dict = {}
    assert surface_run.closed_skin_check(ws, told) is True and told == {}


def test_a_nearly_closed_file_meshed_as_a_shell_is_told_it_has_gaps():
    from meshpipeline.cad import surface_model as sm

    kind, why, _share = sm.classify([{"area_mm2": 100.0, "open_share": 0.003, "closed": False}], said_shell=True)
    assert kind == "surface" and "gaps between them" in why and "no solid" in why and "a shell was asked for" in why


def test_the_rulebook_says_time_10():
    assert RULES_VERSION == "77"
