# Responsibility: Pin the meshability pass's contract with lane F's rows - one gate (the pass
#                 replaces the gap pre-pass, never both), records built from the session's own
#                 verdicts with author "pass", and the run ALWAYS returning a STEP when the pass is
#                 live: the upload's bytes, unchanged, on every exit, with the exception re-raised.
# Boundaries: no kernel. Sessions are stubs; the repair itself is monkeypatched.
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from tests.unit.cad.test_repair_resolve_sub_mesh_gap import (
    _REGION,
    _gap_clash,
    _Work,
)

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import conservative, region_loop


@pytest.fixture(autouse=True)
def _seam_opted_in(monkeypatch):
    # `repair_step_file` refuses BEFORE choosing an engine unless the deployment opted in (the seam's switch,
    # tests/native/test_native_cad_repair.py proves it for every engine). These tests are about what happens after
    # that, with the engines stubbed, so they run as an opted-in deployment.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)


@pytest.fixture
def gap_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", True)


@pytest.fixture
def pass_live(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESHABILITY_PASS", "live")


def _run(work):
    return region_loop._meshability_pass(work, (_REGION,), remaining=lambda: 1000.0,
                                         reserve=10.0, tolerance_mm=1e-4, max_ops=6)


def test_with_both_flags_on_each_face_gets_one_application_per_rung(gap_on, pass_live):
    from meshpipeline.cad.repair.gap_split import SAG_LADDER

    work = _Work((_gap_clash(),))
    out, records = _run(work)
    assert work.offered == list(SAG_LADDER)          # once per rung, never twice
    assert [r["rung"] for r in records] == list(range(len(SAG_LADDER)))
    assert all(r["author"] == "pass" and r["op"] == "resolve_sub_mesh_gap" for r in records)
    assert all(r["verdict"] == "rolled_back:target_remains" for r in records)
    assert all(r["probe_after"] is None for r in records)   # nothing kept, nothing probed


def test_a_kept_application_is_probed_as_delivered(gap_on, pass_live, monkeypatch):
    monkeypatch.setattr(region_loop, "_probe_delivered",
                        lambda shape, diagonal, **_: {"meshes": True, "kind": ""})
    work = _Work((_gap_clash(),), commit_on=1)
    work.shape = object()
    _out, records = _run(work)
    assert records[0]["verdict"] == "kept"
    assert records[0]["probe_after"] == {"meshes": True, "kind": ""}


def test_the_pass_needs_its_flag_and_the_gap_flag_alone_is_the_old_prepass(gap_on, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESHABILITY_PASS", "off")
    work = _Work((_gap_clash(),), commit_on=1)
    out, records = _run(work)
    assert len(out) == 1 and records[0]["op"] == "resolve_sub_mesh_gap"
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", False)
    assert _run(_Work((_gap_clash(),))) == ([], [])


def test_a_decline_is_recorded_with_its_reason(gap_on, pass_live):
    class _Declines(_Work):
        def try_package(self, package):
            verdict = super().try_package(package)
            return dataclasses.replace(verdict, verdicts=tuple(
                dataclasses.replace(v, evidence={"declined": "flat (no curvature)"})
                for v in verdict.verdicts))

    work = _Declines((_gap_clash(),))
    _out, records = _run(work)
    assert len(records) == 1 and records[0]["verdict"] == "declined:flat (no curvature)"


def test_the_flag_reads_live_only(monkeypatch):
    for value, on in (("live", True), ("off", False), ("Live", False), ("", False)):
        monkeypatch.setattr(rcfg, "CAD_REPAIR_MESHABILITY_PASS", value)
        assert region_loop.meshability_pass_on() is on


# ALWAYS RETURN A STEP

@pytest.fixture
def upload(tmp_path) -> Path:
    path = tmp_path / "part.step"
    path.write_bytes(b"ISO-10303-21; the customer's exact bytes\n")
    return path


@pytest.mark.parametrize("error", [
    conservative.RepairRefused("nothing was kept", measurements={"ending": "nothing_held"}),
    conservative.RepairUnavailable("repair is switched off"),
    RuntimeError("unexpected"),
], ids=["refused", "unavailable", "unexpected"])
def test_every_exit_leaves_the_uploads_bytes_and_re_raises(pass_live, upload, tmp_path,
                                                          monkeypatch, error):
    from meshpipeline.cad.repair import mesh_hints

    monkeypatch.setattr(mesh_hints, "hints_for", lambda path, defects, **_: None)

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(conservative, "_repair_step_file", boom)
    destination = tmp_path / "out" / "part-repaired.step"
    with pytest.raises(type(error)) as raised:
        conservative.repair_step_file(upload, destination)
    assert raised.value is error
    assert destination.read_bytes() == upload.read_bytes()
    assert raised.value.measurements["delivery_source"] == "passthrough"
    assert raised.value.measurements["hints"] == "none"


def test_with_the_flag_off_a_refusal_writes_nothing(upload, tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESHABILITY_PASS", "off")

    def refuse(*args, **kwargs):
        raise conservative.RepairRefused("nothing was kept")

    monkeypatch.setattr(conservative, "_repair_step_file", refuse)
    destination = tmp_path / "part-repaired.step"
    with pytest.raises(conservative.RepairRefused) as raised:
        conservative.repair_step_file(upload, destination)
    assert not destination.exists() and "delivery_source" not in raised.value.measurements


def test_unresolved_mesh_findings_go_to_lane_gs_hints(pass_live, upload, tmp_path, monkeypatch):
    from meshpipeline.cad.repair import mesh_hints

    finding = _gap_clash()
    seen: list = []

    def refuse(*args, **kwargs):
        raise conservative._unresolved(conservative.RepairRefused("x"), (finding,))

    def hints_for(path, defects, **_):
        seen.append((Path(path), tuple(defects)))
        sidecar = Path(f"{path}.mesh-hints.json")
        sidecar.write_text("{}")
        return sidecar

    monkeypatch.setattr(conservative, "_repair_step_file", refuse)
    monkeypatch.setattr(mesh_hints, "hints_for", hints_for)
    destination = tmp_path / "part-repaired.step"
    with pytest.raises(conservative.RepairRefused) as raised:
        conservative.repair_step_file(upload, destination)
    # THE SIDECAR SITS BESIDE THE PASSTHROUGH, made from the upload's own bytes.
    assert raised.value.measurements["hints"] == f"{destination}.mesh-hints.json"
    assert seen == [(destination, (finding,))]
    assert destination.read_bytes() == upload.read_bytes()


def test_no_hint_means_no_sidecar_and_a_failing_provider_keeps_the_delivery(
        pass_live, upload, tmp_path, monkeypatch):
    from meshpipeline.cad.repair import mesh_hints

    def refuse(*args, **kwargs):
        raise conservative.RepairRefused("x")

    monkeypatch.setattr(conservative, "_repair_step_file", refuse)
    monkeypatch.setattr(mesh_hints, "hints_for", lambda path, defects, **_: None)
    destination = tmp_path / "part-repaired.step"
    with pytest.raises(conservative.RepairRefused) as raised:
        conservative.repair_step_file(upload, destination)
    assert raised.value.measurements["hints"] == "none"

    def broken(path, defects, **_):
        raise RuntimeError("provider")

    monkeypatch.setattr(mesh_hints, "hints_for", broken)
    with pytest.raises(conservative.RepairRefused) as raised:
        conservative.repair_step_file(upload, destination)
    assert raised.value.measurements["hints"] == "failed: RuntimeError"
    assert destination.read_bytes() == upload.read_bytes()
