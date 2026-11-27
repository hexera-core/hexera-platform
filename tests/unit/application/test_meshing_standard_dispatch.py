# Responsibility: Verify the team's meshing standard travels with an approved run (STD-1..3): the dispatch carries it only when the organisation has one (a run without it dispatches exactly the payload it always had), the run's state holds it, a dispute re-run keeps the standard its parent was meshed under, the result keeps a step's "From your settings" line as it is, and the run stamp names the standard by a digest.
# Boundaries: plain data and monkeypatched seams; no database, no worker, no model.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.contracts import user_notes as un
from meshpipeline.engines import meshing_standard as MS

STANDARD = {"text": "Global Mesh Size = 10 mm", "reading": {"key": "k1", "arguments": "{}"},
            "updated_by": "engineer@example.com", "updated_at": "2026-11-11T09:30:00+00:00"}


def test_the_dispatch_carries_the_standard_only_when_there_is_one(monkeypatch):
    import meshpipeline.application.dispatch_contract as dc
    from meshpipeline.agents.intake import approval as ap

    captured: dict = {}
    monkeypatch.setattr(dc, "build", lambda **f: captured.setdefault("f", f))
    ref = SimpleNamespace(to_payload=lambda: {})
    session = SimpleNamespace(llm_metadata=[], user_notes=None)
    snapshot = {"id": "a1", "payload": {"patches": []}}
    ap._build_dispatch_payload(job_id="j", owner_id="u", session_id="s", session=session, snapshot=snapshot,
                               source_ref=ref, interp_ref=ref, meshing_standard=dict(STANDARD))
    assert captured["f"]["meshing_standard"] == STANDARD
    without = []
    for kw in ({}, {"meshing_standard": None}):
        captured.clear()
        ap._build_dispatch_payload(job_id="j", owner_id="u", session_id="s", session=session, snapshot=snapshot,
                                   source_ref=ref, interp_ref=ref, **kw)
        without.append(captured["f"])
    assert "meshing_standard" not in without[0] and without[0] == without[1]
    assert "meshing_standard" in dc.accepted_keys() and "meshing_standard" not in dc.required_keys()


def test_the_run_state_holds_the_standard_and_none_without_one():
    from meshpipeline.application.pipeline_run import JobRequest
    from meshpipeline.pipeline.state_factory import make_pipeline_state

    req = JobRequest("j", meshing_standard=dict(STANDARD))
    state = make_pipeline_state("j", "u", meshing_standard=req.meshing_standard)
    assert MS.standard_of(state) == STANDARD
    assert JobRequest("j").meshing_standard is None
    assert make_pipeline_state("j", "u")[MS.STATE_KEY] is None


def test_a_settings_line_is_kept_as_it_is_and_a_notes_line_reads_as_before(tmp_path):
    from meshpipeline.application.pipeline_run import _notes_remarks

    assert un.result_lines(["From your settings: all 4 things you asked for are met.", "refined at the holes"]) == [
        "From your settings: all 4 things you asked for are met.", "From your notes: refined at the holes"]
    # a run with a standard and no notes: only what a step said of it (never "they went to every attempt")
    state = {MS.STATE_KEY: STANDARD, "openfoam_workspace": str(tmp_path), "engine": "gmsh"}
    assert _notes_remarks(state) == []
    un.record_remark(tmp_path, "From your settings: all 4 things you asked for are met.", by="mesh_requirements")
    assert _notes_remarks(state) == ["From your settings: all 4 things you asked for are met."]
    assert _notes_remarks({"openfoam_workspace": str(tmp_path)}) == [], "neither: no line"


def test_the_run_stamp_names_the_standard_by_a_digest_only(tmp_path):
    from meshpipeline.application import run_reproducibility as rr

    rec = rr.build("j", {MS.STATE_KEY: STANDARD, "openfoam_workspace": str(tmp_path)})
    assert rec["meshing_standard"]["reading_kept"] is True and len(rec["meshing_standard"]["sha256"]) == 64
    assert STANDARD["text"] not in repr(rec)
    assert "meshing_standard" not in rr.build("j", {"openfoam_workspace": str(tmp_path)})


async def test_a_dispute_rerun_keeps_the_standard_its_parent_was_meshed_under(monkeypatch):
    """STD-3 on both reruns of a finished run (VER-9's change, VER-11 / VER-18's second look): the new run is meshed
    under the standard its parent was meshed under, as the parent's payload holds it - never the team's standard as
    saved now, though it changed since. Driven through the dispute route, and through the field set a rerun is built
    from when the parent's own payload is gone."""
    from types import SimpleNamespace as NS

    from tests.unit.application import test_change_request as TCR

    from meshpipeline.api.v1 import simulation
    from meshpipeline.application import meshing_standard_service

    current = {**STANDARD, "text": "Global Mesh Size = 4 mm", "updated_at": "2026-11-27T08:00:00+00:00"}

    async def _current(*a, **k):
        raise AssertionError(f"a rerun read the team's standard as saved now ({current['text']})")

    monkeypatch.setattr(meshing_standard_service, "for_dispatch", _current)
    kinds = []
    for body in ({"comment": TCR._OWNER_WORDS, "flags": TCR._TWO_MARKS},           # a change: a new run
                 {"comment": "", "flags": TCR._TWO_MARKS[:1]}):                    # a second look, then the rebuild
        parent = TCR._parent()
        parent.dispatch_payload = {**parent.dispatch_payload, "meshing_standard": dict(STANDARD)}
        resp, payload = await TCR._post(body, parent=parent)
        assert resp.status_code == 202, resp.text
        assert payload["meshing_standard"] == STANDARD, (resp.json()["kind"], payload.get("meshing_standard"))
        kinds.append(resp.json()["kind"])
    assert kinds == ["change", "review"]
    # the parent's payload gone (retention) but its standard kept: the rerun's fields carry that one
    intake = simulation._ParentIntake(session_id="s")
    parent = NS(id="p", geometry_interpretation_id=None, geometry_source=None)
    base = await simulation._rerun_base(None, parent, intake, {"meshing_standard": dict(STANDARD)}, owner_id="u",
                                        organization_id="o")
    assert base["meshing_standard"] == STANDARD
    assert "meshing_standard" not in await simulation._rerun_base(None, parent, intake, {}, owner_id="u",
                                                                  organization_id="o")
