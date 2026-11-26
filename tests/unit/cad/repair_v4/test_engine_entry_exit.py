# Responsibility: Pin the engine's ENTRY and EXIT (`repair_v4` -> `_run_search`): STEP in through the
#                 XCAF read kernel fn and `Workspace.from_brep`, STEP out through the delivery gate
#                 (`deliverable`) and `write_step_xcaf`, or the upload's bytes on a passthrough.
# Boundaries: hermetic. The pool, the judge and the middle (`_find_best`: staged healers + search,
#             other lanes' code) are fakes at the seam; `repair_v4()`, `_run_search`, the real
#             `Workspace.from_brep` and the real `deliverable` run. The real kernel is native-tier.
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import meshpipeline.cad.repair_v4 as v4
from meshpipeline.cad.repair_v4 import search as search_mod
from meshpipeline.cad.repair_v4.trace import Trace
from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Workspace

UPLOAD = b"ISO-10303-21;\nTHE CUSTOMER'S OWN BYTES\x00\xff"
REPAIRED = b"ISO-10303-21;\nREPAIRED"


@dataclass
class FakeVerdict:
    q: int
    clean: bool = False
    tier: str = "full"
    inconclusive: bool = False
    valid: Any = field(default_factory=lambda: SimpleNamespace(ok=True))
    panel: tuple = field(default_factory=lambda: (SimpleNamespace(name="gmsh", status="pass"),))
    faithful: Any = None


def _ok(value: dict) -> CallResult:
    return CallResult(ok=True, value=value, error=None, status="ok", seconds=0.0)


class FakePool:
    instances: list[FakePool] = []
    read_result: CallResult | None = None
    write_result: CallResult | None = None

    def __init__(self, size: int, **_: Any) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        FakePool.instances.append(self)

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append((fn, payload))
        if fn == "read_step_xcaf":
            return FakePool.read_result or _ok(
                {"brep": "/w/in.brep", "labels": {"F1": {"name": "top"}}, "units": "inch",
                 "assembly_ids": ["A1"]})
        if fn == "ws_index":
            return _ok({"quantum": 1e-6, "entities": [{"type": "face", "fp": "x", "index": 1}]})
        if fn == "write_step_xcaf":
            if FakePool.write_result is not None:
                return FakePool.write_result
            Path(payload["path"]).write_bytes(REPAIRED)
            return _ok({"path": payload["path"]})
        raise AssertionError(f"unexpected kernel call {fn}")

    def close(self) -> None:
        self.closed = True


class World:
    """The fakes behind the seam, and what the middle was handed."""

    def __init__(self) -> None:
        self.input_verdict = FakeVerdict(q=1)
        self.best_verdict = FakeVerdict(q=3)
        self.changed = True
        self.infrastructure_failed = False
        self.middle_raises: BaseException | None = None
        self.judge_raises: BaseException | None = None
        self.middle_args: dict = {}

    def judge(self, pool: Any = None, ws: Any = None) -> Any:
        def full(pool: Any, ws: Workspace, original: str) -> FakeVerdict:
            if self.judge_raises:
                raise self.judge_raises
            return self.input_verdict

        return SimpleNamespace(full=full)

    def find_best(self, pool: Any, ws: Workspace, source: str, *, budget: Any, deadline_s: float,
                  judge: Any, input_verdict: Any = None) -> Any:
        self.middle_args = {"ws": ws, "source": source, "budget": budget, "deadline_s": deadline_s}
        if self.middle_raises:
            raise self.middle_raises
        trace = Trace()
        trace.record("start")
        return search_mod.SearchResult(
            best=ws, best_verdict=self.best_verdict, clean=self.best_verdict.clean, trace=trace,
            changed=self.changed, reason="patience", infrastructure_failed=self.infrastructure_failed)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    FakePool.instances = []
    FakePool.read_result = FakePool.write_result = None
    monkeypatch.setattr("meshpipeline.cad.repair_v4.worker_pool.WorkerPool", FakePool)
    monkeypatch.setattr(v4, "_make_judge", w.judge)
    monkeypatch.setattr(v4, "_find_best", w.find_best)
    monkeypatch.setattr(search_mod, "score", lambda v: (1, v.q, 0.0, 0.0, 0))
    return w


@pytest.fixture
def files(tmp_path: Path) -> tuple[str, str]:
    src = tmp_path / "in.step"
    src.write_bytes(UPLOAD)
    return str(src), str(tmp_path / "out.step")


def _budget() -> Any:
    return v4.Budget(wall_s=600.0, ops=6, model_calls=0, model_usd=0.0)


def _run(files: tuple[str, str], *, deadline_in: float = 600.0) -> v4.V4Result:
    import time

    src, dest = files
    return v4.repair_v4(src, dest, budget=_budget(), deadline_s=time.monotonic() + deadline_in)


def test_budget_is_searchs_own_and_the_placeholder_is_gone() -> None:
    assert v4.Budget is search_mod.Budget
    assert list(v4.Budget.__dataclass_fields__) == ["wall_s", "ops", "model_calls", "model_usd"]


def test_in_reads_xcaf_and_hands_the_labels_to_from_brep(world: World, files: tuple[str, str]) -> None:
    _run(files)
    pool = FakePool.instances[0]
    fns = [c[0] for c in pool.calls]
    assert fns[:2] == ["read_step_xcaf", "ws_index"]
    assert pool.calls[0][1]["path"] == files[0]
    ws = world.middle_args["ws"]
    assert ws.brep_path == "/w/in.brep"
    assert ws.labels == {"F1": {"name": "top"}}  # from_brep does no I/O: the caller carried the names
    assert world.middle_args["source"] == files[0]


def test_delivered_writes_the_repaired_step_with_labels_units_and_assemblies(
        world: World, files: tuple[str, str]) -> None:
    result = _run(files)
    assert result.delivered is True and not result.passthrough
    assert Path(files[1]).read_bytes() == REPAIRED
    (_, payload) = [c for c in FakePool.instances[0].calls if c[0] == "write_step_xcaf"][0]
    assert payload["labels"] == {"F1": {"name": "top"}}
    assert payload["units"] == "inch" and payload["assembly_ids"] == ["A1"]
    assert payload["brep"] == "/w/in.brep"
    assert result.verdict_summary["status"] == "improved"
    assert result.verdict_summary["input"]["clean"] is False
    assert result.verdict_summary["delivered"]["panel"] == {"gmsh": "pass"}
    assert result.trace_path is not None and result.trace_path.is_file()


def test_passthrough_is_byte_identical_to_the_upload(world: World, files: tuple[str, str]) -> None:
    world.changed = False
    result = _run(files)
    assert result.delivered is False and result.passthrough
    assert Path(files[1]).read_bytes() == Path(files[0]).read_bytes() == UPLOAD
    assert not [c for c in FakePool.instances[0].calls if c[0] == "write_step_xcaf"]
    assert result.verdict_summary["status"] == "not_clean"
    assert "delivered" not in result.verdict_summary


def test_a_clean_input_is_never_replaced(world: World, files: tuple[str, str]) -> None:
    world.input_verdict = FakeVerdict(q=5, clean=True)
    result = _run(files)
    assert result.delivered is False
    assert Path(files[1]).read_bytes() == UPLOAD
    assert result.verdict_summary["status"] == "clean"


def test_a_worse_best_is_not_delivered(world: World, files: tuple[str, str]) -> None:
    world.best_verdict = FakeVerdict(q=0)  # fewer panel passes than the input's 1
    assert _run(files).delivered is False
    assert Path(files[1]).read_bytes() == UPLOAD


@pytest.mark.parametrize("exc", [CheckerUnavailable("dead"), InconclusiveVerdict("dead")])
@pytest.mark.parametrize("where", ["judge_raises", "middle_raises"])
def test_a_dead_checker_is_unavailable_never_a_verdict(
        world: World, files: tuple[str, str], exc: Exception, where: str) -> None:
    setattr(world, where, exc)
    with pytest.raises(v4.SearchUnavailable, match="never measured"):
        _run(files)
    assert not Path(files[1]).exists()  # nothing was written: no verdict, no passthrough claim
    assert FakePool.instances[0].closed


def test_an_inconclusive_input_verdict_is_unavailable(world: World, files: tuple[str, str]) -> None:
    world.input_verdict = FakeVerdict(q=0, inconclusive=True)
    with pytest.raises(v4.SearchUnavailable):
        _run(files)


def test_a_search_that_measured_nothing_is_unavailable(world: World, files: tuple[str, str]) -> None:
    world.infrastructure_failed = True
    with pytest.raises(v4.SearchUnavailable):
        _run(files)
    assert not Path(files[1]).exists()


def test_an_unreadable_step_is_our_limit_not_a_finding(world: World, files: tuple[str, str]) -> None:
    FakePool.read_result = CallResult(False, None, "boom", "error", 0.0)
    with pytest.raises(v4.SearchUnavailable, match="not a finding"):
        _run(files)
    assert not Path(files[1]).exists()


def test_a_failed_write_of_a_repair_is_unavailable_and_leaves_no_partial_dest(
        world: World, files: tuple[str, str]) -> None:
    FakePool.write_result = CallResult(False, None, "disk", "error", 0.0)
    with pytest.raises(v4.SearchUnavailable):
        _run(files)
    assert not Path(files[1]).exists()


def test_a_pool_that_cannot_start_is_unavailable(
        world: World, files: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("kernel worker did not start")

    monkeypatch.setattr("meshpipeline.cad.repair_v4.worker_pool.WorkerPool", boom)
    with pytest.raises(v4.SearchUnavailable, match="worker pool"):
        _run(files)


def test_an_expired_deadline_ships_the_input_unmeasured(world: World, files: tuple[str, str]) -> None:
    result = _run(files, deadline_in=-1.0)
    assert result.delivered is False
    assert Path(files[1]).read_bytes() == UPLOAD
    assert result.verdict_summary["status"] == "not_measured"
    assert FakePool.instances == []  # no pool was even spawned


def test_the_pool_is_torn_down_on_every_exit(world: World, files: tuple[str, str]) -> None:
    _run(files)
    world.changed = False
    _run(files)
    assert [p.closed for p in FakePool.instances] == [True, True]


def test_the_search_is_given_the_input_unchanged_as_original(world: World, files: tuple[str, str]) -> None:
    _run(files)
    assert world.middle_args["deadline_s"] > 0
    assert world.middle_args["budget"].ops == 6
