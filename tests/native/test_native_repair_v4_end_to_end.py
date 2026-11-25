# Responsibility: The first END-TO-END proof of the CRS v4 engine seam. A real injected defect goes through the real
#                 production entry point `repair_v4()` once per arm (input_only, staged, search_det, search_ai) and,
#                 for the evaluation path, through `arms.run_file` once per arm, on the real kernel, the real worker
#                 pool, the real four-member panel and (for search_ai) the REAL model router over a fake transport.
# Boundaries: the native tier (`meshpipeline-mesh:native`: OCCT, gmsh, Netgen). NOTHING in the engine is faked: not the
#             pool, not the judge, not `_find_best`, not the panel. The ONLY double is the provider client under the
#             real router (`providers.client_for`), so no model is called and no money is spent.
#
# THIS TEST IS MEANT TO BE RED WHERE THE SEAM IS NOT WIRED. An arm that produces nothing, a stage that is a stub, or a
# constant standing in for a derived value FAILS with a message naming the arm and the stage. There is no skip, no
# xfail and no "ran without error" pass anywhere in this file.
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from tests.native._v4_flush_patch_part import write_flush_patch_box  # noqa: E402

import meshpipeline.cad.repair_v4 as v4  # noqa: E402
from meshpipeline.cad.repair_v4 import search as search_mod  # noqa: E402
from meshpipeline.cad.repair_v4.measurement import file_unmeasured  # noqa: E402  - the ONE rule, not restated
from meshpipeline.cad.repair_v4.verdict.assemble import default_members  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402

ARMS = ("input_only", "staged", "search_det", "search_ai")
ROUTE = "openai:gpt-6-astra"
DEADLINE_S = 300.0
_HERE = Path(__file__).resolve()


def _devtools(name: str) -> Any:
    root = Path("/srv/devtools/validation/v4_eval")
    if not root.exists():
        root = _HERE.parents[2] / "devtools" / "validation" / "v4_eval"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(f"t31_{name}", root / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- the defective part: a real box with a real injected defect -------------------------------------------------------


@pytest.fixture(scope="module")
def defect_step(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A FIXABLE defect: a 3 x 3 mm face deleted from a 100 x 60 x 40 mm box (loop ~3.4% of the diagonal, within the
    fill cap). It is NOT a deleted face of a bare box: every such face spans 57-87% of the diagonal, no op may repair it
    by design, and `face_delete` now refuses to produce one (that is the declined stratum, `face_delete_large`)."""
    inj = _devtools("inject")
    root = tmp_path_factory.mktemp("t31_defect")
    clean = write_flush_patch_box(root / "flush_patch_box.step")
    case = inj.inject(clean, "face_delete", 3, root)  # an open shell: one small face of the part is gone
    assert case.label["kind"] == "face_delete" and case.label["status"] == "structural"
    assert case.label["loop_span_ratio"] <= case.label["fill_cap_ratio"]
    return str(case.path)


# --- the record: one line per (path, arm) for the task report ------------------------------------------------------------


@dataclass
class Run:
    arm: str
    seconds: float = 0.0
    result: Any = None
    error: str | None = None
    dest: str = ""
    notes: dict = field(default_factory=dict)


RECORD: list[dict] = []


def _record(path: str, run: Run, **extra: Any) -> None:
    RECORD.append({"path": path, "arm": run.arm, "seconds": round(run.seconds, 2), "error": run.error, **extra})
    out = os.environ.get("T31_RECORD")
    if out:
        Path(out).write_text(json.dumps(RECORD, indent=1, default=str))


# --- the fake transport under the REAL router ----------------------------------------------------------------------


class Wire:
    """The provider client the real router dials. Counts requests; answers with an empty `propose`."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def client(self) -> Any:
        async def _create(**kwargs: Any) -> Any:
            self.requests.append(kwargs)
            return SimpleNamespace(
                output=[{"type": "function_call", "call_id": f"c{len(self.requests)}", "name": "propose",
                         "arguments": '{"candidates": []}'}],
                usage={"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 0},
                       "output_tokens_details": {"reasoning_tokens": 0}},
                status="completed")
        return SimpleNamespace(responses=SimpleNamespace(create=_create))


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    from meshpipeline.adapters._shared.resilience import reset_breakers
    from meshpipeline.adapters.model_inference import providers
    from meshpipeline.cad.repair_v4 import model_route
    from meshpipeline.contracts import inference_telemetry, model_capacity
    from meshpipeline.contracts import model_inference as mi

    reset_breakers()
    w = Wire()
    monkeypatch.setattr(providers, "client_for", lambda target: w.client())
    model_route.install_router()  # the router a wave shard installs: CachingModelRouter over the real router
    inference_telemetry.set_inference_telemetry(None)
    yield w
    mi.set_model_router(None)  # type: ignore[arg-type]
    model_capacity.set_capacity_controller(None)
    reset_breakers()


def _counting_ledger(tmp_path: Path) -> tuple[Any, list[tuple]]:
    from meshpipeline.cad.repair_v4.model_ledger import WaveLedger

    ledger = WaveLedger.for_test(cap_usd=5.0, path=tmp_path / "ledger.jsonl")
    admits: list[tuple] = []
    real = type(ledger).admit

    def admit(self: Any, file_id: str, estimate_usd: float, *, file_ceiling_usd: float) -> Any:
        a = real(self, file_id, estimate_usd, file_ceiling_usd=file_ceiling_usd)
        admits.append((file_id, estimate_usd, a.ok))
        return a

    type(ledger).admit = admit  # type: ignore[method-assign]
    ledger._t31_restore = lambda: setattr(type(ledger), "admit", real)  # type: ignore[attr-defined]
    return ledger, admits


# --- A. the production entry point, once per arm -----------------------------------------------------------------------


def _drive_repair_v4(arm: str, source: str, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Run:
    """`repair_v4()` with the arm selected the only way the production entry lets one be selected: its `ARM`."""
    run = Run(arm=arm, dest=str(tmp / f"out_{arm}.step"))
    monkeypatch.setattr(v4, "ARM", arm)
    seen_h: list[float] = []
    real_find_sites = search_mod.find_sites

    def spy_find_sites(pool: Any, ws: Any, verdict: Any, *, h_mm: float, **kw: Any) -> Any:
        seen_h.append(h_mm)
        return real_find_sites(pool, ws, verdict, h_mm=h_mm, **kw)

    monkeypatch.setattr(search_mod, "find_sites", spy_find_sites)
    budget = v4.Budget(wall_s=120.0, ops=12, model_calls=4 if arm == "search_ai" else 0,
                       model_usd=1.0 if arm == "search_ai" else 0.0)
    t0 = time.monotonic()
    try:
        run.result = v4.repair_v4(source, run.dest, budget=budget, deadline_s=time.monotonic() + DEADLINE_S)
    except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on with the arm named
        run.error = f"{type(exc).__name__}: {exc}"
        run.notes["traceback"] = traceback.format_exc()[-1500:]
    run.seconds = time.monotonic() - t0
    run.notes["h_mm_seen"] = seen_h
    return run


def _read_back(path: str, tmp: Path) -> int:
    """Faces in the STEP at `path`, read by the real kernel."""
    pool = WorkerPool(1)
    try:
        read = pool.call("read_step_xcaf", {"path": path, "brep_out": str(tmp / "rb.brep")}, timeout_s=120)
        assert read.ok, read.error
        return len(read.value["labels"]) or 1
    finally:
        pool.close()


@pytest.mark.parametrize("arm", ARMS)
def test_repair_v4_produces_a_step_a_verdict_and_a_measured_answer(
        arm: str, defect_step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    run = _drive_repair_v4(arm, defect_step, tmp_path, monkeypatch)
    _record("repair_v4", run, delivered=getattr(run.result, "delivered", None),
            summary=getattr(run.result, "verdict_summary", None), h_mm_seen=run.notes["h_mm_seen"],
            wire_requests=len(wire.requests))
    if run.error is not None:
        pytest.fail(f"ARM {arm!r} PRODUCED NOTHING at stage repair_v4._find_best / arm_generators: the production "
                    f"entry point raised {run.error}. No path through repair_v4() runs this arm.")
    result = run.result
    # 1. a STEP at dest; on a passthrough its bytes ARE the upload's, not merely a file that exists
    assert Path(run.dest).is_file(), f"ARM {arm!r}: stage write/passthrough wrote no STEP at dest"
    assert Path(run.dest).stat().st_size > 0, f"ARM {arm!r}: dest is empty"
    if not result.delivered:
        assert Path(run.dest).read_bytes() == Path(defect_step).read_bytes(), \
            f"ARM {arm!r}: a passthrough must leave the upload's bytes at dest, byte for byte"
    else:
        assert Path(run.dest).read_bytes().startswith(b"ISO-10303-21"), f"ARM {arm!r}: delivered dest is not a STEP"
        _read_back(run.dest, tmp_path)
    # 2. a verdict, from a real panel - never an empty or one-mesher one
    summary = result.verdict_summary
    assert summary.get("status"), f"ARM {arm!r}: verdict_summary carries no status: {summary!r}"
    panel = summary.get("input", {}).get("panel")
    expected = [m.name for m in default_members("full")]  # whatever the panel IS; no member count is assumed here
    assert panel and list(panel) == expected, \
        f"ARM {arm!r}: the input verdict's panel {list(panel or [])} is not the full panel {expected}"
    assert all(status != "None" for status in panel.values()), f"ARM {arm!r}: a panel member has no status: {panel}"
    assert any("gmsh" not in name for name in panel), \
        f"ARM {arm!r}: the panel is one mesher's opinion (only gmsh members): {list(panel)}"
    # 3. a report row carrying whether the file was measured. `V4Result` is the only thing `conservative` hands the
    #    report; the rule's inputs must be derivable from it
    search = summary.get("search", {})
    missing = [k for k in ("reason", "infrastructure_failed", "inconclusive_candidates", "verdict_candidates")
               if k not in search]
    assert not missing, (
        f"ARM {arm!r}: V4Result.verdict_summary['search'] lacks {missing}, so file_unmeasured() (the shared rule in "
        "measurement.py) cannot be applied to a repair_v4 result; only the eval path (arms.run_file) carries them. "
        "Contract change owned by the controller.")
    assert file_unmeasured(
        reason=search["reason"], infrastructure_failed=search["infrastructure_failed"],
        inconclusive_candidates=search["inconclusive_candidates"], verdict_candidates=search["verdict_candidates"],
        out_clean=search["clean"]) is False, f"ARM {arm!r}: the file reads as never measured"


def test_repair_v4_sizes_the_mesh_from_the_part_not_from_a_constant(
        defect_step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    """22a's `H_MM = 1.0` vs `search.h_mm_for(pool, ws)` (`1000 * fea_plan.base_size(diag/1000)`)."""
    from meshpipeline.cad.repair_v4.workspace import Workspace

    run = _drive_repair_v4("search_det", defect_step, tmp_path, monkeypatch)
    assert run.error is None, f"search_det PRODUCED NOTHING: {run.error}"
    pool = WorkerPool(1)
    try:
        read = pool.call("read_step_xcaf", {"path": defect_step, "brep_out": str(tmp_path / "h.brep")}, timeout_s=120)
        derived = search_mod.h_mm_for(pool, Workspace.from_brep(pool, read.value["brep"], read.value["labels"]))
    finally:
        pool.close()
    seen = run.notes["h_mm_seen"]
    assert seen, "repair_v4 never asked the judge for sites: the search stage did not reach site finding"
    assert all(abs(h - derived) < 1e-9 for h in seen), (
        f"repair_v4 clustered sites at h_mm={sorted(set(seen))} but the part's derived mesh size is {derived:.6f} mm "
        f"(search.h_mm_for). repair_v4/__init__.py still hard-codes H_MM = {v4.H_MM} and calls default_judge, "
        "not default_judge_for.")


# --- B. the evaluation path, once per arm: `arms.run_file` produces the report row ------------------------------------------


@pytest.mark.parametrize("arm_name", ARMS)
def test_run_file_produces_a_measured_report_row(arm_name: str, defect_step: str, tmp_path: Path, wire: Wire) -> None:
    arms, evalspec, report = _devtools("arms"), _devtools("evalspec"), _devtools("report")
    spec = evalspec.arm(arm_name)
    ledger, admits = _counting_ledger(tmp_path) if spec.uses_model else (None, [])
    pool = WorkerPool(2)
    run = Run(arm=arm_name)
    try:
        engine = arms.RealEngine()
        ws = engine.open_workspace(pool, defect_step, str(tmp_path / "wd"))
        h_mm = search_mod.h_mm_for(pool, ws)
        entry = SimpleNamespace(name="t31_box_face_delete", sha256="0" * 64, source="inject")
        budget = evalspec.Budget(wall_s=120.0, ops=12, model_usd=spec.budget.model_usd)
        t0 = time.monotonic()
        try:
            outcome = arms.run_file(arm_name, entry, defect_step, pool=pool, engine=engine, ledger=ledger, arm=spec,
                                    budget=budget, h_mm=h_mm, route=ROUTE if spec.uses_model else None, max_turns=4,
                                    job_id="t31", workdir=str(tmp_path / "wd2"), workers=2)
        finally:
            run.seconds = time.monotonic() - t0
    finally:
        pool.close()
        if ledger is not None:
            ledger._t31_restore()
    _record("run_file", run, row=outcome.row, excluded=outcome.excluded, wire_requests=len(wire.requests),
            admits=len(admits), h_mm=h_mm)
    assert outcome.excluded is None, f"ARM {arm_name!r} PRODUCED NO ROW: it was excluded: {outcome.excluded}"
    row = outcome.row
    assert row is not None, f"ARM {arm_name!r} produced neither a row nor an exclusion"
    assert row["arm"] == arm_name and row["wall_s"] >= 0
    assert row["panel_size"] == len(default_members("full")), f"ARM {arm_name!r}: panel_size {row['panel_size']}"
    assert row["in_valid"] is not None and row["out_valid"] is not None, f"ARM {arm_name!r}: no verdict layers in row"
    if arm_name in ("search_det", "search_ai"):
        measured = not file_unmeasured(
            reason=row["reason"], infrastructure_failed=row["infrastructure_failed"],
            inconclusive_candidates=row["inconclusive_candidates"], verdict_candidates=row["verdict_candidates"],
            out_clean=row["out_clean"])
        assert measured and row["inconclusive"] is False, f"ARM {arm_name!r}: the row says the file was NOT measured: {row}"
        assert row["verdict_candidates"] > 0, f"ARM {arm_name!r}: the search measured no candidate: {row}"
    else:
        # input_only / staged are verdict-only arms: the row carries no search fields, so measured-ness is the row's
        # own `inconclusive` flag. The row must still round-trip through the report's own loader
        assert row["inconclusive"] is False, f"ARM {arm_name!r}: the row says the file was not measured"
    parsed = report._row_from_dict(row)
    assert parsed.arm == arm_name and parsed.counts is True, f"ARM {arm_name!r}: the report would not count this row"
    if arm_name == "search_ai":
        assert wire.requests, "ARM 'search_ai': the AI proposer put no request on the wire (a stub proposer?)"
        assert len(admits) == len(wire.requests), (
            f"ARM 'search_ai': {len(wire.requests)} requests on the wire but {len(admits)} ledger admits; "
            "every request must be admitted exactly once")
        assert all(ok for _f, _e, ok in admits), f"a request was admitted that the ledger refused: {admits}"
        assert row["model_usd"] > 0.0, "ARM 'search_ai': requests were made but no spend was recorded"
    else:
        assert not wire.requests, f"ARM {arm_name!r} must not reach a model; {len(wire.requests)} requests on the wire"


# --- B2. the precondition of the T2 smoke: a FIXABLE defect is REPAIRED, not merely given a verdict -------------------------


@pytest.mark.parametrize("arm", ("search_det", "search_ai"))
def test_a_fixable_injected_case_ends_clean(
        arm: str, defect_step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    """The assertion the 'reaches a verdict' tests above cannot make: they pass on a CORRECT decline. This defect is
    repairable (tests/native/test_native_repair_v4_fill_cap.py proves `fill_loop` + `make_volume` take it to a clean
    verdict on the real judge), so an arm that does not end with `clean` has not found a repair that exists. If this
    is red, the T2 smoke would measure 'the search never proposed the fix' and call it a result."""
    run = _drive_repair_v4(arm, defect_step, tmp_path, monkeypatch)
    assert run.error is None, f"ARM {arm!r} PRODUCED NOTHING: {run.error}"
    result = run.result
    trace = json.loads(Path(result.trace_path).read_text()) if result.trace_path else {"events": []}
    proposed = sorted({e["op"] for e in trace["events"] if e.get("kind") == "candidate"})
    stop = next((e for e in trace["events"] if e.get("kind") == "stop"), {})
    assert result.verdict_summary["search"]["clean"] is True and result.delivered, (
        f"ARM {arm!r}: a repairable defect (loop within the fill cap) did NOT end clean. search reason "
        f"{stop.get('reason')!r}; the search proposed only {proposed}; fill_loop proposed: {'fill_loop' in proposed}. "
        f"verdict_summary={result.verdict_summary!r}")
    assert Path(run.dest).read_bytes().startswith(b"ISO-10303-21")


# --- C. the spawn exposure: can a caller reach repair_v4() from a module with NO `__main__` guard? ----------------------


def test_repair_v4_is_callable_from_a_script_without_a_main_guard(defect_step: str, tmp_path: Path) -> None:
    """The pool spawns (never forks), so a spawned child re-imports the caller's `__main__`. `_run_search` builds its
    pool inside a function, so pytest needs no guard - but a plain script that calls `repair_v4()` at module level
    re-executes itself in every worker. Production code must not make callers know that."""
    import subprocess

    script = tmp_path / "noguard.py"
    script.write_text(
        "import time\nimport meshpipeline.cad.repair_v4 as v4\n"
        f"r = v4.repair_v4({defect_step!r}, {str(tmp_path / 'ng_out.step')!r},"
        " budget=v4.Budget(wall_s=30, ops=2, model_calls=0, model_usd=0.0), deadline_s=time.monotonic() + 120)\n"
        "print('DONE', r.delivered)\n")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=240,
                          env={**os.environ, "PYTHONPATH": str(_HERE.parents[2] / "src")})
    assert "DONE" in proc.stdout, (
        "repair_v4() called from a module without an `if __name__ == '__main__':` guard fails: "
        f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.stdout[-300:]} "
        "(WorkerPool spawns; production exposure, not a test artifact)")
