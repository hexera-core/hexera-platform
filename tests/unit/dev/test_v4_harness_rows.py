# Responsibility: Prove the v4 harness writes the rows the report reads (and refuses to write ones it would refuse),
#                 copies the plan_wave provenance into each shard, and that the launcher claims the wave through
#                 plan_wave. Hermetic: a fake arm runner, a directory marker store, a recorded gcloud.
# Boundaries: no CAD, no model, no bucket. The arm dispatch itself (Task 22c) is faked as `runner`; what is under
#             test is what happens to a row between the runner and the report.
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from meshpipeline.cad.repair_v4 import model_ledger as ml  # noqa: E402

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
import report  # noqa: E402
import run_arm as ra  # noqa: E402

ev, rp = ra.evalspec, ra.reprobe_mod
COMMIT, IMAGE = "a" * 40, "img@sha256:" + "b" * 64
PANEL = 4  # the full panel today; nothing below depends on the number


def _sha(i: int) -> str:
    return f"{i:064x}"


class Entry:
    def __init__(self, i: int, source: str = "abc_fail") -> None:
        self.name, self.sha256, self.source = f"f{i}.step", _sha(i), source


def _write_world(tmp_path: Path, split_name: str, n: int = 12, *, source: str = "abc_fail"):
    """A pool, a split file and staged inputs for `n` files, all in one shard."""
    pool = tmp_path / "pool.json"
    entries = [ra.pool_mod.PoolEntry(f"f{i}.step", source, _sha(i), "u", None, "lic") for i in range(n)]
    ra.pool_mod.write_pool(entries, pool, sources_included=[source])
    clean = split_name == "clean"
    rows = [rp.ProbeRow(f"f{i}.step", _sha(i), source, {"tier": "full", "clean": clean, "counts_for_scoreboard": True},
                        None if clean else "panel", "" if clean else "plc_intersection", None) for i in range(n)]
    sp = tmp_path / f"{split_name}.json"
    sp.write_text(json.dumps({"split": split_name, "files": [asdict(r) for r in rows]}))
    inputs = tmp_path / "inputs"
    for i in range(n):
        (inputs / _sha(i)).mkdir(parents=True)
        (inputs / _sha(i) / f"f{i}.step").write_text("ISO-10303-21;")
    return pool, sp, inputs, rows


def conclusive_row(entry, arm: str, *, in_clean: bool = False, fixed: bool = False, **over) -> dict:
    out = in_clean or fixed
    row = {"name": entry.name, "sha256": entry.sha256, "source": entry.source, "arm": arm, "inconclusive": False,
           "model_usd": 0.0, "wall_s": 1.0, "in_clean": in_clean, "in_panel_passes": PANEL if in_clean else 1,
           "in_valid": True, "in_faithful_ok": True, "out_valid": True, "out_faithful_ok": True,
           "out_panel_passes": PANEL if out else 1, "panel_size": PANEL, "out_tier": "full", "out_clean": out}
    if arm in report.SEARCH_ARMS:
        row.update(reason="clean" if out else "patience", verdict_candidates=0 if in_clean else 4,
                   inconclusive_candidates=0, infrastructure_failed=False)
    row.update(over)
    return row


def run_shard(tmp_path, wave, *, runner, plan=None, extra=(), split_name=None, n=12, source="abc_fail"):
    pool, sp, inputs, rows = _write_world(tmp_path, split_name or wave.split, n, source=source)
    out = tmp_path / "out"
    argv = ["--mode", "harness", "--shard", "1/1", "--inputs", str(inputs), "--out", str(out), "--pool", str(pool),
            "--commit", COMMIT, "--image", IMAGE, "--sources", source,
            "--wave-type", wave.wave_type, "--wave-id", "wv1", "--n-shards", "1",
            "--ledger-path", str(tmp_path / "ledger.jsonl"), "--marker-bucket", "dev-crsv3-transfer-224734058693",
            "--h-mm", "0.5", *(plan.provenance_argv() if plan is not None else []), *extra]
    return argv, out, sp


@pytest.fixture
def world(tmp_path, monkeypatch):
    """main() with the marker store replaced by a directory one that carries a passed probe record."""
    mp = ra.marker_probe
    markers = ml.FileMarkerStore(tmp_path / "markers")
    mp.record_pass(markers, backend="gcs/dev-crsv3-transfer-224734058693", run_id="r", rounds=50, at=__import__("time").time())
    monkeypatch.setattr(ra, "make_marker_store", lambda bucket: (markers, f"gcs/{bucket}"))
    return markers


def go(monkeypatch, argv, sp, runner):
    monkeypatch.setattr(ra, "ARM_RUNNER", runner)
    monkeypatch.setattr(ra.reprobe_mod, "SPLITS_DIR", sp.parent)
    return ra.main(argv)


def prov_of(out: Path) -> dict:
    return json.loads((out / "PROVENANCE.json").read_text())


# ------------------------------------------------------------------ rows the report reads

def test_the_harness_writes_rows_the_report_loads_and_counts(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["dev_deterministic"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)
    seen: list[tuple[str, str]] = []

    def runner(entry, arm, source):
        seen.append((entry.name, arm))
        assert Path(source).is_file()
        return conclusive_row(entry, arm, fixed=(arm == "staged" and int(entry.name[1:-5]) % 2 == 0)), None

    assert go(monkeypatch, argv, sp, runner) == 0, prov_of(out).get("error")
    assert len(seen) == 12 * 2 and {a for _, a in seen} == {"input_only", "staged"}
    rows = json.loads((out / "rows.json").read_text())
    assert len(rows) == 24 and json.loads((out / "EXCLUDED.json").read_text()) == []
    assert len((out / "rows.jsonl").read_text().splitlines()) == 24
    shard = tmp_path / "wave" / "shard-1-of-1"
    shard.parent.mkdir()
    out.rename(shard)
    result = report.load_wave(shard.parent, wave, rp.read_split(sp), n_shards=1)
    rep = report.build_report(result)
    assert rep["arms"]["staged"]["clean_out"] == 6 and rep["arms"]["input_only"]["clean_out"] == 0
    assert rep["arms"]["staged"]["clean_gain"] == 6  # ... and the report agrees the staged arm is not the input


def test_plan_provenance_is_copied_into_the_shard_and_the_report_can_verify_the_slot(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["heldout"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)
    assert go(monkeypatch, argv, sp, lambda e, a, s: (conclusive_row(e, a), None)) == 0, prov_of(out).get("error")
    prov = prov_of(out)
    for key, want in plan.provenance().items():
        assert prov[key] == want, key
    assert prov["slot"] == "slot/held_out/1" and prov["ledger"] == "shard"
    assert prov["ledger_detail"]["cap_usd"] == 250.0  # the table's cap over one shard
    # The held-out wave lists v3_meshability_pass_ai, which run_arm cannot run (v3_eval runs it). The report needs a row
    # for every (file, arm), so the MERGE step must add v3's rows; until it exists the report refuses, which is right.
    unmerged = tmp_path / "unmerged" / "shard-1-of-1"
    shutil.copytree(out, unmerged)
    with pytest.raises(report.ReportRefused, match="v3_meshability_pass_ai"):
        report.load_wave(unmerged.parent, wave, rp.read_split(sp), n_shards=1, frozen_commit=COMMIT, markers=world)
    rows_path = out / "rows.json"
    v3_rows = [conclusive_row(Entry(i), "v3_meshability_pass_ai") for i in range(12)]
    rows_path.write_text(json.dumps([*json.loads(rows_path.read_text()), *v3_rows], sort_keys=True))
    shard = tmp_path / "wave" / "shard-1-of-1"
    shard.parent.mkdir()
    out.rename(shard)
    res = report.load_wave(shard.parent, wave, rp.read_split(sp), n_shards=1, frozen_commit=COMMIT, markers=world)
    assert res.slot == "slot/held_out/1" and res.slot_verified
    assert report.build_report(res)["primary_artifact"] is True


def test_a_shard_without_the_plan_is_what_the_heldout_report_refuses(tmp_path, monkeypatch, world) -> None:
    """The sibling of the test above: strip the provenance the harness copies and the report refuses, so the copy is
    load-bearing and not decorative."""
    wave = ev.WAVES["heldout"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)
    assert go(monkeypatch, argv, sp, lambda e, a, s: (conclusive_row(e, a), None)) == 0
    p = out / "PROVENANCE.json"
    p.write_text(json.dumps({k: v for k, v in prov_of(out).items() if k != "slot"}))
    shard = tmp_path / "wave" / "shard-1-of-1"
    shard.parent.mkdir()
    out.rename(shard)
    with pytest.raises(report.ReportRefused, match=r"copy\s+plan_wave\(\.\.\.\)\.provenance\(\)"):
        report.load_wave(shard.parent, wave, rp.read_split(sp), n_shards=1, frozen_commit=COMMIT, markers=world)


def test_a_plan_for_another_wave_is_not_stamped_on_this_shard(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["dev_checkpoint"]
    other = ev.plan_wave(wave, wave_id="some-other-wave", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=other)  # shard says --wave-id wv1
    assert go(monkeypatch, argv, sp, lambda e, a, s: (conclusive_row(e, a), None)) == 4
    assert "wave_id" in prov_of(out)["error"] and not (out / "rows.json").exists()
    assert "slot" not in prov_of(out)  # nothing of the plan reached the provenance


# ------------------------------------------------------------------ infrastructure is not a verdict

def test_a_search_that_measured_nothing_cannot_be_written_as_a_conclusive_row(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["dev_search"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)

    def liar(entry, arm, source):  # inconclusive=False beside a search that says nothing was measured
        return conclusive_row(entry, arm, reason="no_verdict", verdict_candidates=0, inconclusive_candidates=4,
                              infrastructure_failed=True), None

    assert go(monkeypatch, argv, sp, liar) == 4
    assert "never got a verdict" in prov_of(out)["error"]
    assert not (out / "rows.json").exists()  # nothing half-written for a collector to trust


def test_an_inconclusive_row_and_an_excluded_file_are_written_as_such_never_as_failures(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["dev_search"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)

    def runner(entry, arm, source):
        i = int(entry.name[1:-5])
        if i == 0:   # the search saw only dead checkers
            return {"name": entry.name, "sha256": entry.sha256, "source": entry.source, "arm": arm, "inconclusive": True,
                    "model_usd": 0.0, "wall_s": 1.0, "reason": "no_verdict", "verdict_candidates": 0,
                    "inconclusive_candidates": 5, "infrastructure_failed": True}, None
        if i == 1:
            return None, "checker_unavailable: gmsh is not installed"
        if i == 2:
            raise RuntimeError("worker pool died")
        return conclusive_row(entry, arm, fixed=True), None

    assert go(monkeypatch, argv, sp, runner) == 0, prov_of(out).get("error")
    rows = json.loads((out / "rows.json").read_text())
    excluded = json.loads((out / "EXCLUDED.json").read_text())
    assert len(rows) == 9 * 2 + 2 and sum(r["inconclusive"] for r in rows) == 2
    assert {x["name"] for x in excluded} == {"f1.step", "f2.step"} and len(excluded) == 4
    assert all(x["reason"].startswith(("checker_unavailable", "error: RuntimeError")) for x in excluded)
    assert not any(r["name"] in ("f1.step", "f2.step") for r in rows)  # an excluded file has NO row, so it has no verdict
    assert prov_of(out)["unmeasured_rows"] == 2


def test_a_missing_input_is_excluded_not_a_failure(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["dev_deterministic"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)
    (tmp_path / "inputs" / _sha(3) / "f3.step").unlink()
    assert go(monkeypatch, argv, sp, lambda e, a, s: (conclusive_row(e, a), None)) == 0
    assert [x["reason"].split(":")[0] for x in json.loads((out / "EXCLUDED.json").read_text())] == ["input_missing"] * 2


def test_with_no_runner_the_harness_calls_the_real_arm_dispatch_not_a_second_one(tmp_path, monkeypatch, world) -> None:
    """`--mode harness` with a plan IS `run_arms`: with no per-file runner it calls `arms.run_file` (22c's dispatch);
    the old refusal (ArmDispatchMissing) is gone because the dispatch it waited for exists."""
    wave = ev.WAVES["dev_deterministic"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)
    seen: list[str] = []

    def real(name, entry, source, **kw):
        seen.append(name)
        return ra.arms_mod.ArmOutcome(row=conclusive_row(entry, name))

    monkeypatch.setattr(ra.arms_mod, "run_file", real)
    assert go(monkeypatch, argv, sp, None) == 0, prov_of(out).get("error")
    assert len(seen) == 12 * 2 and len(json.loads((out / "rows.json").read_text())) == 24


# ------------------------------------------------------------------ the held-out and Fusion 360 boundaries

def test_a_tuning_purpose_cannot_run_the_heldout_split(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["heldout"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan, extra=("--purpose", "tuning"))
    assert go(monkeypatch, argv, sp, lambda e, a, s: (conclusive_row(e, a), None)) == 4
    assert "HeldOutInTuning" in prov_of(out)["error"] and not (out / "rows.json").exists()


def test_a_tuning_purpose_is_fine_on_the_dev_split(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["dev_deterministic"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan, extra=("--purpose", "tuning"))
    assert go(monkeypatch, argv, sp, lambda e, a, s: (conclusive_row(e, a), None)) == 0


def test_fusion_360_in_a_split_is_refused_before_any_file_runs(tmp_path, monkeypatch, world) -> None:
    wave = ev.WAVES["dev_deterministic"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan, source="f360")
    called: list[str] = []
    assert go(monkeypatch, argv, sp, lambda e, a, s: called.append(a) or (conclusive_row(e, a), None)) == 4
    assert called == [] and "DisallowedSource" in prov_of(out)["error"] and not (out / "rows.json").exists()


# ------------------------------------------------------------------ the launcher claims through plan_wave

_spec = importlib.util.spec_from_file_location("fleet_helpers_t24", Path(__file__).with_name("test_v4_fleet_launcher.py"))
assert _spec is not None and _spec.loader is not None
base = importlib.util.module_from_spec(_spec)
sys.modules["fleet_helpers_t24"] = base
_spec.loader.exec_module(base)
lf = base.lf
mp = ra.marker_probe
BUCKET = "dev-crsv3-transfer-224734058693"


@pytest.fixture
def live(monkeypatch):
    rec = base._Recorder()
    monkeypatch.setattr(lf.subprocess, "run", rec)
    monkeypatch.setattr(lf, "live_checks", lambda *a, **k: ([], []))
    monkeypatch.setattr(lf, "done_units", lambda *a: set())
    monkeypatch.setattr(lf, "running_units", lambda *a: (set(), []))
    return rec


def _gates(tmp_path):
    import time

    markers = ml.FileMarkerStore(tmp_path / "markers")
    mp.record_pass(markers, backend=f"gcs/{BUCKET}", run_id="r1", rounds=50, at=time.time())
    return markers, f"gcs/{BUCKET}"


def _arm_flags(wave_name: str) -> list[str]:
    """What an arms wave launch must carry in the merged launcher: the dispatchable arms of the wave, where its split
    json is staged, the element size, and a secret iff an arm calls a model."""
    names = [a for a in ev.WAVES[wave_name].arms if a in ev.DISPATCHABLE_ARMS]
    return ["--arms", ",".join(names), "--split-uri", "gs://bkt/splits/s.json", "--h-mm", "0.5",
            *(["--model-secret", "some-secret"] if any(ev.ARMS[a].uses_model for a in names) else [])]


def _launch(tmp_path, *extra, gates=None):
    if "--wave" in extra and extra[extra.index("--wave") + 1] in ev.WAVES:
        extra = (*extra, *_arm_flags(extra[extra.index("--wave") + 1]))
    return lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD, "--skip-preflight",
                    "--profile", "v4", "--max-run-hours", "8", *extra], gates=gates or _gates(tmp_path))


def _provenances(live) -> set[str]:
    out = set()
    for cmd in live.creates():
        meta = next(c for c in cmd if c.startswith("--metadata=")).split("|")
        out.update(m.split("=", 1)[1] for m in meta if m.startswith("v4-plan-provenance="))
    return out


def test_the_launcher_claims_the_wave_through_plan_wave_and_hands_every_vm_the_plan(live, tmp_path) -> None:
    gates = _gates(tmp_path)
    assert _launch(tmp_path, "--wave", "dev_checkpoint", "--wave-id", "cp-one", gates=gates) == 0
    assert len(live.creates()) == 12
    assert gates[0].get("slot/checkpoint/1") == "cp-one"  # the slot the REPORT checks, claimed by plan_wave
    (prov,) = _provenances(live)  # one plan, every VM
    plan = json.loads(prov)
    assert plan["slot"] == "slot/checkpoint/1" and plan["wave"] == "dev_checkpoint" and plan["n_shards"] == 96
    assert plan["wave_cap_usd"] == ev.wave_cap_usd("checkpoint") and plan["arms"] == list(ev.WAVES["dev_checkpoint"].arms)
    assert all("v4-wave=dev_checkpoint" in "|".join(c) for c in live.creates())


def test_a_dry_run_names_the_wave_but_claims_no_slot(live, tmp_path, capsys) -> None:
    gates = _gates(tmp_path)
    assert _launch(tmp_path, "--wave", "dev_checkpoint", "--wave-id", "cp-one", "--dry-run", gates=gates) == 0
    assert gates[0].get("slot/checkpoint/1") is None and live.creates() == []
    assert "UNCLAIMED-dry-run" in capsys.readouterr().out


def test_a_second_heldout_wave_is_refused_and_creates_no_vm(live, tmp_path, capsys) -> None:
    gates = _gates(tmp_path)
    assert _launch(tmp_path, "--wave", "heldout", "--wave-id", "ho-one", gates=gates) == 0
    first = len(live.creates())
    assert _launch(tmp_path, "--wave", "heldout", "--wave-id", "ho-two", gates=gates) == 1
    assert len(live.creates()) == first and gates[0].get("slot/held_out/1") == "ho-one"
    assert "held-out" in capsys.readouterr().err


def test_a_resume_of_the_same_wave_id_reuses_its_slot(live, tmp_path) -> None:
    gates = _gates(tmp_path)
    assert _launch(tmp_path, "--wave", "dev_checkpoint", "--wave-id", "cp-one", gates=gates) == 0
    assert _launch(tmp_path, "--wave", "dev_checkpoint", "--wave-id", "cp-one", gates=gates) == 0
    assert gates[0].get("slot/checkpoint/2") is None


def test_the_wave_type_comes_from_the_wave_and_a_contradiction_is_refused(live, tmp_path) -> None:
    with pytest.raises(SystemExit):
        _launch(tmp_path, "--wave", "dev_checkpoint", "--wave-type", "normal", "--wave-id", "cp-one")
    assert live.creates() == []


def test_a_wave_that_would_get_no_cap_is_not_nameable(tmp_path) -> None:
    with pytest.raises(ValueError, match="no spending cap"):
        ev.WaveDef("bad", "unpriced", "dev", ("input_only",))
    with pytest.raises(SystemExit):
        _launch(tmp_path, "--wave", "no_such_wave", "--wave-id", "x-1")


def test_v3_refuses_the_wave_flag(live, tmp_path) -> None:
    with pytest.raises(SystemExit):
        lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD, "--skip-preflight",
                 "--wave", "heldout", "--dry-run"], gates=_gates(tmp_path))


def test_the_startup_script_hands_an_arm_wave_its_plan_arms_and_element_size_and_fails_closed_without_it() -> None:
    text = (Path(__file__).resolve().parents[3] / "devtools/validation/v3_eval/vm_startup.sh").read_text()
    assert 'PLAN_PROV="$(attr v4-plan-provenance)"' in text
    # ONE dispatch: an arms wave is `--mode arms` with the plan, the arms and --h-mm, never a second harness mode
    assert 'mode=(--mode arms --wave "${WAVE}" --arms "${ARMS}" --split "${SPLIT}" --split-file /eval/split.json --h-mm "${HMM}")' in text
    assert 'planarg=(--plan-provenance "${PLAN_PROV}")' in text and "MODE=harness" not in text
    assert 'case "${PLAN_PROV}" in' in text and "refusing an arms wave whose slot was not claimed" in text


def test_a_dry_run_of_a_second_heldout_wave_is_refused_without_claiming(live, tmp_path, capsys) -> None:
    gates = _gates(tmp_path)
    assert gates[0].create("slot/held_out/1", "ho-one")  # somebody already ran the held-out wave
    assert _launch(tmp_path, "--wave", "heldout", "--wave-id", "ho-two", "--dry-run", gates=gates) == 1
    assert gates[0].get("slot/held_out/1") == "ho-one" and live.creates() == []
    # ONE slot ledger: the launcher's own count reads the very key plan_wave and the report read, so it refuses first
    seen = capsys.readouterr()
    assert "held-out wave was already launched as 'ho-one'" in seen.out + seen.err


def test_there_is_one_slot_ledger_the_launcher_and_plan_wave_name_the_same_keys() -> None:
    """The launcher's wave count (wave_limits) and plan_wave's claim (the keys the report reads) were two key spaces;
    a held-out split could be spent once in each. They must be the same markers."""
    wl = ra.wave_limits
    assert [wl.HELD_OUT_KEY] == ev.slot_keys("held_out")
    assert [wl.checkpoint_key(n) for n in range(1, wl.CHECKPOINT_LIMIT + 1)] == ev.slot_keys("checkpoint")


def test_a_slot_the_launcher_claimed_is_the_slot_plan_wave_returns(tmp_path) -> None:
    markers = ml.FileMarkerStore(tmp_path / "m")
    assert ra.wave_limits.check_wave_count(markers, "held_out", "ho-1", claim=True) is None
    assert ev.plan_wave(ev.WAVES["heldout"], wave_id="ho-1", n_shards=1, markers=markers).slot == "slot/held_out/1"
    assert ra.wave_limits.check_wave_count(markers, "held_out", "ho-2", claim=True) is not None


def test_a_harness_shard_of_a_search_wave_without_an_element_size_is_refused(tmp_path, monkeypatch, world) -> None:
    """`--mode harness` skips the parser's arm resolution (the plan names the arms), so this is the only guard on that
    path: there is no default element size, and a search without one is not a measurement."""
    wave = ev.WAVES["dev_search"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    argv, out, sp = run_shard(tmp_path, wave, runner=None, plan=plan)
    i = argv.index("--h-mm")
    del argv[i:i + 2]
    called: list[str] = []
    assert go(monkeypatch, argv, sp, lambda e, a, s: called.append(a) or (conclusive_row(e, a), None)) == 4
    assert called == [] and "--h-mm" in prov_of(out)["error"] and not (out / "rows.json").exists()


# ------------------------------------------------------------------ the file wall is honoured (snapshot v1: 1262 s > 900 s)

def _late_shard(tmp_path, world):
    wave = ev.WAVES["dev_deterministic"]
    plan = ev.plan_wave(wave, wave_id="wv1", n_shards=1, markers=world)
    return run_shard(tmp_path, wave, runner=None, plan=plan, n=1, extra=("--file-timeout-s", "0.3"))


def test_an_engine_that_swallows_exceptions_cannot_swallow_the_file_wall(tmp_path, monkeypatch, world) -> None:
    """Snapshot v1: search_det on bug32314 was a MEASURED row at 1262 s under a 900 s wall. The search loop's broad
    `except Exception` (one broken branch must not end the search) ate the alarm; the run went on to its own wall."""
    import time

    argv, out, sp = _late_shard(tmp_path, world)

    def runner(entry, arm, source):
        try:
            time.sleep(2.0)  # the alarm fires in here
        except Exception:  # noqa: BLE001 - what search.py does around a branch
            pass
        return conclusive_row(entry, arm, fixed=False), None

    assert go(monkeypatch, argv, sp, runner) == 0, prov_of(out).get("error")
    assert json.loads((out / "rows.json").read_text()) == []
    reasons = [x["reason"] for x in json.loads((out / "EXCLUDED.json").read_text())]
    assert len(reasons) == 2 and all(r.startswith("file_timeout") for r in reasons)


def test_a_row_that_finished_past_its_wall_is_excluded_even_if_the_alarm_was_lost(tmp_path, monkeypatch, world) -> None:
    import signal
    import time

    argv, out, sp = _late_shard(tmp_path, world)

    def runner(entry, arm, source):
        signal.setitimer(signal.ITIMER_REAL, 0)  # the alarm is gone (cancelled, or spent and swallowed earlier)
        time.sleep(0.6)
        return conclusive_row(entry, arm, fixed=False), None

    assert go(monkeypatch, argv, sp, runner) == 0, prov_of(out).get("error")
    assert json.loads((out / "rows.json").read_text()) == []
    reasons = [x["reason"] for x in json.loads((out / "EXCLUDED.json").read_text())]
    assert len(reasons) == 2 and all(r.startswith("file_timeout") and "past" in r for r in reasons)


def test_a_row_inside_its_wall_is_still_a_row(tmp_path, monkeypatch, world) -> None:
    argv, out, sp = _late_shard(tmp_path, world)
    assert go(monkeypatch, argv, sp, lambda e, a, s: (conclusive_row(e, a, fixed=False), None)) == 0
    assert len(json.loads((out / "rows.json").read_text())) == 2


def test_file_timeout_is_not_an_exception_a_broad_handler_can_eat() -> None:
    assert not issubclass(rp.FileTimeout, Exception) and issubclass(rp.FileTimeout, BaseException)
