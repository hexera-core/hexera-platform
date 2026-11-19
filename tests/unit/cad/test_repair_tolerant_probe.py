# Responsibility: Prove tier 2b - the coarse tolerant-mesh quick pass that answers "would the
#                 tolerant rung eat this?" - and the `routing` entry the finishing verdict records
#                 it under.
# Boundaries: no fTetWild, no gmsh and no OCP. `subprocess.Popen` and the child's own `run_part`
#             are injected, `fea_tetwild.binary` is patched, and the shape is a stub. Nothing here
#             starts a process that meshes anything.
from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import goal as goal_mod
from meshpipeline.cad.repair import oracle
from meshpipeline.cad.repair import tolerant_probe as tp
from meshpipeline.cad.repair.contracts import RepairGoal

SHAPE = object()


# --------------------------------------------------------------------------------------------
# The parent: one bounded child, and a verdict for every way it can end.
# --------------------------------------------------------------------------------------------


class FakeProc:
    """Only what `_run_child` touches on a `subprocess.Popen`."""

    def __init__(self, out: str = "", returncode: int = 0, timeout: bool = False,
                 pid: int = 4242):
        self._out = out
        self.returncode = returncode
        self._timeout = timeout
        self.pid = pid
        self.killed = False
        self.waits: list[float | None] = []

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if self._timeout and not self.killed:
            raise subprocess.TimeoutExpired(cmd="tier2b", timeout=timeout or 0.0)
        return self._out, ""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _popen(monkeypatch, proc: FakeProc | None, *, raises: Exception | None = None) -> list[dict]:
    """Patch `subprocess.Popen` inside the probe and record how the child was started."""
    calls: list[dict] = []

    def fake(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        if raises is not None:
            raise raises
        assert proc is not None
        return proc

    monkeypatch.setattr(tp.subprocess, "Popen", fake)
    return calls


def _binary(monkeypatch, found: str | None = "/usr/local/bin/FloatTetwild_bin") -> None:
    from meshpipeline.engines import fea_tetwild

    monkeypatch.setattr(fea_tetwild, "binary", lambda: found)


def _child_line(**answer) -> str:
    return "\n" + json.dumps({"tets": 0, "seconds": 0.0, "stopped": False, "error": "",
                              "out_of_time": False, **answer}) + "\n"


def test_absent_binary_is_unavailable_not_a_verdict(monkeypatch, tmp_path):
    _binary(monkeypatch, None)
    calls = _popen(monkeypatch, None, raises=AssertionError("no child may be started"))
    result = tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=30.0)
    assert result.verdict == tp.UNAVAILABLE
    assert result.tets == 0
    assert calls == []


def test_tets_mean_it_eats(monkeypatch, tmp_path):
    _binary(monkeypatch)
    _popen(monkeypatch, FakeProc(_child_line(tets=1200, stopped=True, seconds=4.5)))
    result = tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=30.0)
    assert result.verdict == tp.EATS
    assert result.tets == 1200
    assert result.stopped is True
    assert result.seconds == 4.5


def test_an_error_is_a_refusal(monkeypatch, tmp_path):
    _binary(monkeypatch)
    said = "fTetWild exited 1: could not insert the surface"
    _popen(monkeypatch, FakeProc(_child_line(tets=0, error=said)))
    result = tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=30.0)
    assert result.verdict == tp.REFUSES
    assert result.message == said
    assert result.tets == 0


def test_killed_run_is_no_answer(monkeypatch, tmp_path):
    """Both ways the clock can win: the child saying so, and the parent's own timeout."""
    _binary(monkeypatch)
    _popen(monkeypatch, FakeProc(_child_line(error="out of time", out_of_time=True)))
    child_said = tp.tolerant_probe(tmp_path / "a.brep", diagonal_mm=100.0, budget_s=30.0)
    assert child_said.verdict == tp.NO_ANSWER

    _binary(monkeypatch)
    _popen(monkeypatch, FakeProc(timeout=True))
    parent_said = tp.tolerant_probe(tmp_path / "b.brep", diagonal_mm=100.0, budget_s=30.0)
    assert parent_said.verdict == tp.NO_ANSWER


def test_dead_child_is_no_answer(monkeypatch, tmp_path):
    _binary(monkeypatch)
    _popen(monkeypatch, FakeProc("", returncode=-11))
    result = tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=30.0)
    assert result.verdict == tp.NO_ANSWER
    assert result.tets == 0


def test_tolerant_probe_kills_the_whole_group(monkeypatch, tmp_path):
    """A timed-out child is killed by PROCESS GROUP: fTetWild is the child's own grandchild, and
    killing the python child alone would leave the mesher running on the worker."""
    _binary(monkeypatch)
    proc = FakeProc(timeout=True, pid=98765)
    _popen(monkeypatch, proc)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=30.0)
    assert result.verdict == tp.NO_ANSWER
    assert (98765, signal.SIGKILL) in killed


def test_the_parent_never_waits_past_the_budget_and_the_reap(monkeypatch, tmp_path):
    _binary(monkeypatch)
    proc = FakeProc(_child_line(tets=5))
    _popen(monkeypatch, proc)
    tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=12.0)
    assert proc.waits[0] == pytest.approx(12.0 + tp.REAP_S)


def test_the_child_is_a_fresh_session_with_stderr_discarded(monkeypatch, tmp_path):
    """Never forked from a threaded parent (the ABI rule), and its own session so the group kill
    reaches fTetWild."""
    _binary(monkeypatch)
    calls = _popen(monkeypatch, FakeProc(_child_line(tets=5)))
    tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=30.0)
    started = calls[0]
    assert started["start_new_session"] is True
    assert started["stdout"] is subprocess.PIPE
    assert started["stderr"] is subprocess.DEVNULL
    assert started["stdin"] is subprocess.DEVNULL
    assert "-m" in started["cmd"]
    assert "meshpipeline.cad.repair.tolerant_probe" in started["cmd"]


def test_probe_never_raises(monkeypatch, tmp_path):
    _binary(monkeypatch)
    _popen(monkeypatch, None, raises=OSError("no fork for you"))
    result = tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=30.0)
    assert result.verdict == tp.NO_ANSWER


def test_no_budget_spawns_nothing(monkeypatch, tmp_path):
    _binary(monkeypatch)
    calls = _popen(monkeypatch, None, raises=AssertionError("no child may be started"))
    result = tp.tolerant_probe(tmp_path / "shape.brep", diagonal_mm=100.0, budget_s=0.0)
    assert result.verdict == tp.NO_ANSWER
    assert result.message == tp.NO_TIME_LEFT
    assert calls == []


def test_the_parent_imports_fea_tetwild_only_for_the_binary(monkeypatch, tmp_path):
    """The parent asks `binary()` and nothing else: `run_part` and `tessellate` are the CHILD's.

    Patched to explode, so a parent that reached for either would fail here rather than quietly
    pull numpy and the mesher's machinery into a worker process.
    """
    from meshpipeline.engines import fea_tetwild

    _binary(monkeypatch)
    for name in ("run_part", "tessellate"):
        monkeypatch.setattr(fea_tetwild, name, _explode(name))
    _popen(monkeypatch, FakeProc(_child_line(tets=7)))
    assert tp.tolerant_probe(tmp_path / "s.brep", diagonal_mm=100.0,
                             budget_s=30.0).verdict == tp.EATS


def _explode(name):
    def boom(*args, **kwargs):
        raise AssertionError(f"{name} must not run in the parent process")
    return boom


# --------------------------------------------------------------------------------------------
# The child: coarse, one thread, and bounded by the budget it was handed.
# --------------------------------------------------------------------------------------------


def test_child_uses_coarse_one_thread_settings(monkeypatch):
    """The job fTetWild is handed IS the policy: one thread, `TIER2B_MAX_ITS` rounds, a coarse
    ideal edge, a doubled envelope and no sizing field. A routing answer, not a deliverable."""
    from meshpipeline.engines import fea_tetwild

    jobs: list[dict] = []

    def fake_run_part(job):
        jobs.append(dict(job))
        return {"tets": list(range(900)), "seconds": 2.0, "stopped": False}

    monkeypatch.setattr(fea_tetwild, "run_part", fake_run_part)
    monkeypatch.setattr(fea_tetwild, "tessellate",
                        lambda fmap, faces, deflection: ("XYZ", "TRIS", "LABELS"))
    monkeypatch.setattr(tp, "_read_faces", lambda path: ("SHAPE", "FMAP", 6))

    answer = tp.child_answer("/tmp/shape.brep", 100.0, 30.0)
    assert answer["tets"] == 900
    assert answer["error"] == ""

    job = jobs[0]
    assert job["threads"] == 1
    assert job["max_its"] == oracle.TIER2B_MAX_ITS == 10
    assert job["edge"] == pytest.approx(100.0 * tp.EDGE_DIAG_SHARE)
    assert job["epsilon"] == pytest.approx(100.0 * fea_tetwild.EPS_DIAG_SHARE * 2.0)
    assert job["bg"] is None
    assert job["name"] == tp.JOB_NAME
    # soft before hard, and the hard deadline is exactly the budget it was given
    assert job["hard_at"] - job["soft_at"] == pytest.approx(
        30.0 * (1.0 - fea_tetwild.SOFT_SHARE))


def test_the_child_reports_a_refusal_rather_than_raising(monkeypatch):
    from meshpipeline.engines import fea_tetwild

    monkeypatch.setattr(fea_tetwild, "run_part",
                        lambda job: {"error": "fTetWild exited 1: bad surface", "seconds": 1.0})
    monkeypatch.setattr(fea_tetwild, "tessellate", lambda *a: ("XYZ", "TRIS", "LABELS"))
    monkeypatch.setattr(tp, "_read_faces", lambda path: ("SHAPE", "FMAP", 6))

    answer = tp.child_answer("/tmp/shape.brep", 100.0, 30.0)
    assert answer["tets"] == 0
    assert answer["error"] == "fTetWild exited 1: bad surface"
    assert answer["out_of_time"] is False


def test_the_child_passes_out_of_time_through(monkeypatch):
    from meshpipeline.engines import fea_tetwild

    monkeypatch.setattr(fea_tetwild, "run_part",
                        lambda job: {"error": "stopped", "out_of_time": True})
    monkeypatch.setattr(fea_tetwild, "tessellate", lambda *a: ("XYZ", "TRIS", "LABELS"))
    monkeypatch.setattr(tp, "_read_faces", lambda path: ("SHAPE", "FMAP", 6))

    assert tp.child_answer("/tmp/s.brep", 100.0, 30.0)["out_of_time"] is True


def test_the_child_tessellates_every_face_within_the_envelope(monkeypatch):
    from meshpipeline.engines import fea_tetwild

    asked: list[tuple] = []
    monkeypatch.setattr(fea_tetwild, "tessellate",
                        lambda fmap, faces, deflection: (asked.append((faces, deflection))
                                                         or ("XYZ", "TRIS", "LABELS")))
    monkeypatch.setattr(fea_tetwild, "run_part", lambda job: {"tets": [1], "seconds": 0.1})
    monkeypatch.setattr(tp, "_read_faces", lambda path: ("SHAPE", "FMAP", 4))

    tp.child_answer("/tmp/s.brep", 100.0, 30.0)
    faces, deflection = asked[0]
    assert faces == [0, 1, 2, 3]
    assert deflection == pytest.approx(
        100.0 * fea_tetwild.EPS_DIAG_SHARE * 2.0 * fea_tetwild.TESSELLATION_SHARE)


# --------------------------------------------------------------------------------------------
# The posture: default-off, fail-closed.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [("live", "live"), ("Live", "off"), ("LIVE", "off"),
                                           ("on", "off"), ("", "off"), ("off", "off"),
                                           (" live", "off")])
def test_tolerant_posture_fails_closed(monkeypatch, raw, expected):
    monkeypatch.setenv("CAD_REPAIR_TOLERANT_PROBE", raw)
    assert rcfg._tolerant_probe_posture() == expected


def test_the_posture_is_off_when_unset(monkeypatch):
    monkeypatch.delenv("CAD_REPAIR_TOLERANT_PROBE", raising=False)
    assert rcfg._tolerant_probe_posture() == "off"
    assert rcfg.CAD_REPAIR_TOLERANT_PROBE in ("live", "off")


def test_the_tolerant_timeout_defaults_to_thirty_seconds(monkeypatch):
    assert rcfg.CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S == 30.0


def test_the_posture_is_a_registered_setting():
    from meshpipeline.settings.inventory import all_vars

    names = {v.name: v for v in all_vars()}
    assert names["CAD_REPAIR_TOLERANT_PROBE"].default == "off"
    assert names["CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S"].default == "30.0"


# --------------------------------------------------------------------------------------------
# The routing entry in the finishing verdict.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeMeasure:
    valid: bool = True
    diagonal_mm: float = 100.0
    solids: int = 1
    faces: int = 200


def _meshability(kind: str = "", stage: str = "") -> dict:
    return {"kind": kind, "stage": stage}


def _probe_result(elements: int = 500):
    from meshpipeline.cad.repair import mesh_probe

    return mesh_probe.ProbeResult(True, elements, None)


class TolerantRecorder:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.result


def _verdict(*, tolerant_live: bool, tolerant_prober, kind: str = "overlapping_facets",
             stage: str = "3d", remaining_s: float = 1_000.0,
             tolerant_timeout_s: float = 30.0):
    """`finishing_verdict` with both probers injected and the clock pinned."""
    # The tier-2 reading the routing question is asked ABOUT: its kind and stage decide whether
    # tier 2b is asked at all, so they are driven through the real tier-2 prober's failure.
    from meshpipeline.cad.repair import mesh_probe

    reading = (mesh_probe.ProbeResult(
        False, 0, mesh_probe.MeshFailure(kind, (), None, "said so", stage=stage))
        if kind else _probe_result())

    def prober(**kwargs):
        return reading

    return goal_mod.finishing_verdict(
        RepairGoal.standalone, shape=SHAPE, final=FakeMeasure(), defects=(), packages=(),
        deadline_s=remaining_s, clock=lambda: 0.0, prober=prober,
        tolerant_live=tolerant_live, tolerant_timeout_s=tolerant_timeout_s,
        tolerant_prober=tolerant_prober)


def test_volume_stage_overlap_is_a_pure_reading_of_the_meshability():
    assert goal_mod.volume_stage_overlap(_meshability("overlapping_facets", "3d")) is True
    assert goal_mod.volume_stage_overlap(_meshability("edge_through_face", "3d")) is True
    assert goal_mod.volume_stage_overlap(_meshability("overlapping_facets", "2d")) is False
    assert goal_mod.volume_stage_overlap(_meshability("open_loop", "3d")) is False
    assert goal_mod.volume_stage_overlap(_meshability("", "")) is False
    assert goal_mod.volume_stage_overlap({}) is False


def test_off_adds_no_routing_key():
    """ABSENT, NOT EMPTY: `repair_corpus.goal_row` reads the key's PRESENCE as the flag's signal."""
    def boom(**kwargs):
        raise AssertionError("the tolerant probe must not be called with the flag off")

    verdict = _verdict(tolerant_live=False, tolerant_prober=boom)
    assert "routing" not in verdict


def test_not_volume_stage_is_not_asked():
    def boom(**kwargs):
        raise AssertionError("a non-overlap failure must not be routed")

    verdict = _verdict(tolerant_live=True, tolerant_prober=boom, kind="open_loop", stage="2d")
    assert verdict["routing"]["verdict"] == tp.NOT_ASKED
    assert verdict["routing"]["tier"] == "2b"
    assert verdict["routing"]["tets"] == 0


def test_a_meshing_part_is_not_asked():
    def boom(**kwargs):
        raise AssertionError("a part that meshed has nothing to route")

    verdict = _verdict(tolerant_live=True, tolerant_prober=boom, kind="", stage="")
    assert verdict["routing"]["verdict"] == tp.NOT_ASKED


def test_volume_stage_overlap_is_asked_with_the_routing_budget():
    recorder = TolerantRecorder(tp.TolerantResult(tp.EATS, 1200, 3.5, False, ""))
    verdict = _verdict(tolerant_live=True, tolerant_prober=recorder, remaining_s=200.0)
    expected = oracle.probe_budget_s(oracle.CHECK_ROUTING, 200.0, timeout_s=30.0)
    assert recorder.calls[0]["budget_s"] == expected
    assert recorder.calls[0]["diagonal_mm"] == 100.0
    assert verdict["routing"] == {"tier": "2b", "verdict": tp.EATS, "tets": 1200,
                                  "seconds": 3.5, "stopped": False, "budget_s": expected,
                                  "message": ""}


@pytest.mark.parametrize("remaining_s", [-1.0, 0.0, 1.0, 9.0, 10.0, 11.0, 40.0, 100.0, 1e6])
def test_the_routing_budget_is_the_oracles_rule_at_every_remainder(remaining_s):
    """A TABLE: one big remainder saturates at the timeout and would keep passing if the slack
    term below the deadline were dropped - and that is exactly where a repair run ends."""
    expected = oracle.probe_budget_s(oracle.CHECK_ROUTING, remaining_s, timeout_s=30.0)
    recorder = TolerantRecorder(tp.TolerantResult(tp.EATS, 10, 0.1, False, ""))
    verdict = _verdict(tolerant_live=True, tolerant_prober=recorder, remaining_s=remaining_s)
    assert verdict["routing"]["budget_s"] == expected
    if expected > 0.0:
        assert recorder.calls[0]["budget_s"] == expected
    else:
        # Nothing is spawned without a budget. Below the slack the TIER-2 probe is skipped too,
        # so there is no overlap reading to route and the honest answer is that nobody asked -
        # NOT a no-answer, which would claim we tried and heard nothing.
        assert recorder.calls == []
        assert verdict["routing"]["verdict"] == tp.NOT_ASKED


def test_an_overlap_with_no_routing_time_is_a_no_answer():
    """Tier 2 answered `overlapping_facets` at the volume stage, and tier 2b had no seconds left.

    THE QUESTION WAS ASKED AND WENT UNANSWERED, which is a different row from a part nobody
    routed: a zero-budget pass must never be recorded as the tolerant rung refusing.
    """
    def boom(**kwargs):
        raise AssertionError("a zero budget must spawn nothing")

    verdict = _verdict(tolerant_live=True, tolerant_prober=boom, remaining_s=1_000.0,
                       tolerant_timeout_s=0.0)
    assert verdict["routing"]["budget_s"] == 0.0
    assert verdict["routing"]["verdict"] == tp.NO_ANSWER
    assert verdict["routing"]["message"] == tp.NO_TIME_LEFT


def test_tier_2b_is_bounded_by_its_own_timeout_not_tier_twos_cap():
    """`TIER_2B.hard_cap_s` is None on purpose: the routing pass is bounded by its OWN setting."""
    assert oracle.TIER_2B.hard_cap_s is None
    assert oracle.probe_budget_s(oracle.CHECK_ROUTING, 10_000.0, timeout_s=30.0) == 30.0
    assert oracle.probe_budget_s(oracle.CHECK_ROUTING, 10_000.0, timeout_s=120.0) == 120.0


def test_a_tolerant_refusal_keeps_its_message():
    said = "fTetWild exited 1: could not insert the surface"
    recorder = TolerantRecorder(tp.TolerantResult(tp.REFUSES, 0, 9.0, False, said))
    verdict = _verdict(tolerant_live=True, tolerant_prober=recorder)
    assert verdict["routing"]["verdict"] == tp.REFUSES
    assert verdict["routing"]["message"] == said


def test_the_routing_verdict_is_recorded_only_and_changes_no_score():
    """Nothing stops early on tier 2b in this plan: the tier-2 score is untouched by it."""
    recorder = TolerantRecorder(tp.TolerantResult(tp.EATS, 1200, 3.5, False, ""))
    with_routing = _verdict(tolerant_live=True, tolerant_prober=recorder)
    without = _verdict(tolerant_live=False, tolerant_prober=lambda **k: None)
    assert with_routing["score"] == without["score"] == 0.0
    assert with_routing["meshability"] == without["meshability"]
    assert with_routing["grind"] == without["grind"]


def test_a_tolerant_probe_failure_cannot_break_the_verdict():
    """The verdict NEVER raises - including through the rung it records."""
    def boom(**kwargs):
        raise RuntimeError("the tolerant probe exploded")

    verdict = _verdict(tolerant_live=True, tolerant_prober=boom)
    assert verdict == {"profile": "standalone", "error": "RuntimeError"}


def test_the_routing_keys_are_what_the_corpus_runner_consumes():
    """Producer/consumer parity with `repair_corpus.goal_row`, which reads `verdict` and
    `seconds` off `routing` and nothing else."""
    recorder = TolerantRecorder(tp.TolerantResult(tp.EATS, 1200, 3.5, True, ""))
    routing = _verdict(tolerant_live=True, tolerant_prober=recorder)["routing"]
    assert sorted(routing) == ["budget_s", "message", "seconds", "stopped", "tets", "tier",
                               "verdict"]


def test_the_verdicts_are_the_five_named_strings():
    assert (tp.EATS, tp.REFUSES, tp.NO_ANSWER, tp.UNAVAILABLE, tp.NOT_ASKED) == (
        "tolerant_rung_eats_it", "tolerant_rung_refuses", "no_answer", "unavailable",
        "not_asked")
    assert tp.REAP_S == 5.0


def test_the_call_site_reads_the_posture_rather_than_passing_a_constant():
    """`conservative.repair_step_file` must read `CAD_REPAIR_TOLERANT_PROBE`, not a literal.

    An AST read of the one call site, because the seam's failure mode is textual: the Task 4
    comment said `tolerant_live=False` and a reviewer must never find that constant again.
    """
    import ast
    from pathlib import Path

    from meshpipeline.cad.repair import conservative

    tree = ast.parse(Path(conservative.__file__).read_text())
    reserve_calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "reserve_s"]
    assert len(reserve_calls) == 1
    names = {kw.arg: kw.value for kw in reserve_calls[0].keywords}
    assert sorted(names) == ["tolerant_live", "tolerant_timeout_s"]
    for arg, value in names.items():
        assert not isinstance(value, ast.Constant), arg
    source = Path(conservative.__file__).read_text()
    assert "CAD_REPAIR_TOLERANT_PROBE" in source
    assert "tolerant_live=False" not in source
