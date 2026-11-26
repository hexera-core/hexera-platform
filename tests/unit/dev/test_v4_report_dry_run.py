# Responsibility: A REPORT DRY RUN. A fake re-probe (two shards) goes through collect.py's own CLI and cuts the
#                 splits; a fake held-out wave (two shards, every arm) goes through run_arm.py's own CLI in harness mode
#                 with the plan_wave provenance and a real ledger; report.py's own CLI renders it. Asserts that the
#                 held-out report carries every metric plan Task 21 names, with the numbers this scenario implies.
# Boundaries: no CAD, no model, no bucket. Faked: the per-file arm outcome (`run_arm.ARM_RUNNER`), the marker store
#             (a directory one with a passed probe record), and the v3 arm's rows (v4's run_arm cannot dispatch v3 and
#             the merge step that should supply them is NOT built: see `merge_v3_rows`). Real: collect.main,
#             run_arm.main (plan adoption, the per-file SIGALRM wall kill, write_arm_rows' row contract, PROVENANCE),
#             report.main (load_wave's gates, build_report, render_text), arms._usable's classification strings.
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.dev.test_v4_harness_rows import COMMIT, IMAGE, ev, ml, ra, report, rp

ARMS4 = ("input_only", "staged", "search_det", "search_ai")
V3 = "v3_meshability_pass_ai"
BUCKET = "dev-crsv3-transfer-224734058693"
N_SHARDS = 2
PANEL = 4
CELLS = report.CHEAP_FULL_CELLS


def sha(tag: str, i: int) -> str:
    return tag + f"{i:063x}"  # tag is one hex digit, so every file of a category shares a prefix


def panel_summary(passes: int, *, clean: bool = False) -> dict:
    return {"tier": "full", "clean": clean, "counts_for_scoreboard": True, "hard": [],
            "panel": [{"status": "pass" if m < passes else "fail"} for m in range(PANEL)]}


# ------------------------------------------------------------------ the re-probe (collect.py)

def build_probe(root: Path) -> tuple[Path, list[Any], dict[str, Any]]:
    """61 pool files: 30 panel failures, 10 valid-layer failures, 6 declined-stratum injects, 12 clean, one no_solids,
    one unbounded (out_of_scope) and one the probe could not judge (EXCLUDED). Two cheap/full disagreements."""
    pool_mod = ra.pool_mod
    rows: list[Any] = []
    entries: list[Any] = []

    def add(name: str, source: str, h: str, summary: dict, layer: str | None, family: str, *, kind: str | None = None,
            status: str | None = None) -> None:
        entries.append(pool_mod.PoolEntry(name, source, h, "u", None, "lic", None, kind, status))
        rows.append(rp.ProbeRow(name, h, source, summary, layer, family, None, kind, status))

    for i in range(30):
        add(f"p{i}.step", "abc_fail", sha("1", i), panel_summary(2), "panel", "plc_intersection")
    for i in range(10):
        add(f"v{i}.step", "abc_fail", sha("2", i), {**panel_summary(0), "panel": [], "hard": ["open_shell"]},
            "valid", "open_shell")
    for i in range(6):  # the declined stratum: face_delete_large, labelled by the injector; neutral file names
        add(f"d{i}.step", "inject", sha("3", i), {**panel_summary(0), "panel": [], "hard": ["open_shell"]},
            "valid", "open_shell", kind="face_delete_large", status="declined_stratum")
    for i in range(12):
        add(f"c{i}.step", "abc_pass", sha("4", i), panel_summary(PANEL, clean=True), None, "")
    add("ns.step", "abc_fail", sha("5", 0), {**panel_summary(0), "panel": [], "hard": [rp.NO_SOLIDS]}, "valid",
        rp.NO_SOLIDS)
    add("ub.step", "abc_fail", sha("5", 1), {**panel_summary(0), "panel": [], "hard": [rp.UNBOUNDED],
                                             "out_of_scope": True}, "valid", rp.UNBOUNDED)
    dead = rp.Excluded("dead.step", sha("6", 0), "abc_fail", "file_timeout: file wall clock 900s exceeded")
    entries.append(pool_mod.PoolEntry(dead.name, dead.source, dead.sha256, "u", None, "lic"))
    pool = root / "pool.json"
    pool_mod.write_pool(entries, pool, sources_included=["abc_fail", "abc_pass", "inject"])

    # the cheap tier: agrees everywhere except two clean files it calls failing and two panel files it calls passing
    def cheap_of(r: Any) -> Any:
        flip = r.sha256 in (sha("4", 0), sha("4", 1)) or r.sha256 in (sha("1", 0), sha("1", 1))
        failing = (r.first_failing_layer is not None) != flip
        return rp.ProbeRow(r.name, r.sha256, r.source, {**r.verdict_summary, "tier": "cheap"},
                           "panel" if failing else None, r.family, None, r.kind, r.kind_status)

    probe_root = root / "reprobe"
    for i in range(1, N_SHARDS + 1):
        mine = [r for r in rows if ev.shard_of(r.sha256, i, N_SHARDS)]
        d = probe_root / f"shard-{i}-of-{N_SHARDS}"
        d.mkdir(parents=True)
        (d / "rows.json").write_text(json.dumps([asdict(r) for r in mine]))
        rp.write_rows([cheap_of(r) for r in mine], d / "cheap.jsonl")
        gone = [asdict(dead)] if ev.shard_of(dead.sha256, i, N_SHARDS) else []
        (d / "EXCLUDED.json").write_text(json.dumps(gone))
        (d / "PROVENANCE.json").write_text(json.dumps(
            {"exit_code": 0, "commit": COMMIT, "image": IMAGE, "shard_files": len(mine) + len(gone),
             "full_rows": len(mine), "wall_seconds": 60.0, "machine_type": "c2d-standard-8"}))
    return pool, rows, {"probe_root": probe_root}


# ------------------------------------------------------------------ the arm wave (run_arm.py)

def stage_inputs(root: Path, split: list[Any]) -> Path:
    inputs = root / "inputs"
    for r in split:
        (inputs / r.sha256).mkdir(parents=True, exist_ok=True)
        (inputs / r.sha256 / r.name).write_text("ISO-10303-21;")
    return inputs


class Scenario:
    """What each (file, arm) does in the fake wave, and so what the report must say. Specials, on the first two
    non-declined files X and Y:
      X: input_only CRASHES (an exception in the arm), search_det is an INCONCLUSIVE row (no_verdict), search_ai
         TIMES OUT (the real per-file SIGALRM kill fires: the runner sleeps past --file-timeout-s).
      Y: staged loses a panel member for POOL reasons (arms._usable: excluded), v3 output is OUT_OF_SCOPE.
      R: staged passes FEWER panel members than its input (regression kind 2).
    Every other file: clean outcomes by index, so every arm differs from every other."""

    def __init__(self, split: list[Any], *, clean_control: bool = False) -> None:
        self.split = sorted(split, key=lambda r: r.sha256)
        self.clean_control = clean_control
        self.declined = [r for r in self.split if r.kind_status == report.DECLINED_STATUS]
        self.main = [r for r in self.split if r.kind_status != report.DECLINED_STATUS]
        self.idx = {r.sha256: k for k, r in enumerate(self.main)}
        if clean_control:
            self.x = self.y = self.r = None
        else:
            self.x, self.y = self.main[0].sha256, self.main[1].sha256
            self.r = next(r.sha256 for r in self.main[2:] if r.first_failing_layer == "panel")

    # --- the fate of one (file, arm)
    def special(self, r: Any, arm: str) -> str | None:
        return {(self.x, "input_only"): "crash", (self.x, "search_det"): "inconclusive",
                (self.x, "search_ai"): "timeout", (self.y, "staged"): "pool_member",
                (self.y, V3): "out_of_scope"}.get((r.sha256, arm))

    def clean_out(self, r: Any, arm: str) -> bool:
        if self.clean_control:
            return not ((arm == "staged" and r.sha256 == self.split[0].sha256)
                        or (arm == "search_det" and r.sha256 == self.split[1].sha256))
        if r.kind_status == report.DECLINED_STATUS:
            return arm == "staged" and r.sha256 == self.declined[0].sha256  # one "unexpectedly clean"
        if r.sha256 == self.r and arm == "staged":
            return False
        k = self.idx[r.sha256]
        return {"input_only": False, "staged": k % 4 == 0, "search_det": k % 4 in (0, 1),
                "search_ai": k % 4 in (0, 1, 2), V3: k % 3 == 0}[arm]

    def in_layers(self, r: Any) -> tuple[bool, int]:
        """(input valid, input panel passes) as the split says."""
        passes = sum(m.get("status") == "pass" for m in r.verdict_summary.get("panel", []))
        return r.first_failing_layer != "valid", passes

    def row(self, r: Any, arm: str) -> dict:
        in_valid, in_pass = self.in_layers(r)
        in_clean = bool(r.verdict_summary.get("clean"))
        out = in_clean if arm == "input_only" else self.clean_out(r, arm)
        out_pass = PANEL if out else (in_pass - 1 if (arm == "staged" and r.sha256 == self.r) else
                                      min(in_pass, PANEL - 1))
        out_valid = True if out else (in_valid if arm != "search_det" else in_valid)
        if self.clean_control and not out and arm == "search_det":
            out_valid, out_pass = False, PANEL  # clean -> unclean on layer 1 alone: regression kind 1 only
        if arm == "input_only":
            out_valid, out_pass = in_valid, in_pass
        row = {"name": r.name, "sha256": r.sha256, "source": r.source, "arm": arm, "inconclusive": False,
               "model_usd": 0.25 if arm == "search_ai" else 0.0, "wall_s": 2.0, "in_clean": in_clean,
               "in_panel_passes": in_pass, "in_valid": in_valid, "in_faithful_ok": True, "out_valid": out_valid,
               "out_faithful_ok": True, "out_panel_passes": out_pass, "panel_size": PANEL, "out_tier": "full",
               "out_clean": bool(out and out_valid and out_pass == PANEL)}
        if arm in report.SEARCH_ARMS:
            row.update(reason="clean" if out else "patience", verdict_candidates=0 if in_clean else 3,
                       inconclusive_candidates=0, infrastructure_failed=False, cheap_full=self.cheap_full(r, arm, out))
        return row

    def cheap_full(self, r: Any, arm: str, out: bool) -> dict:
        cells = dict.fromkeys(CELLS, 0)
        if self.clean_control:
            return cells
        cells["cheap_pass_full_pass" if out else "cheap_fail_full_fail"] = 1
        if arm == "search_ai" and self.idx.get(r.sha256, -1) % 5 == 0:
            cells["cheap_pass_full_fail"] += 1  # the cheap tier's blind spot, on every fifth file
        return cells

    def runner(self, entry: Any, arm: str, source: str) -> tuple[dict | None, str | None]:
        r = next(x for x in self.split if x.sha256 == entry.sha256)
        assert Path(source).is_file() and arm != V3
        fate = self.special(r, arm)
        if fate == "crash":
            raise RuntimeError("worker crashed: SIGSEGV in a panel child")
        if fate == "timeout":
            time.sleep(5)  # the shard's --file-timeout-s 1 fires first: run_arm's own SIGALRM wall kill
            raise AssertionError("the wall kill did not fire")
        if fate == "pool_member":
            return None, ra.arms_mod._usable(SimpleNamespace(counts_for_scoreboard=False), "the output")
        if fate == "inconclusive":
            return {"name": r.name, "sha256": r.sha256, "source": r.source, "arm": arm, "inconclusive": True,
                    "model_usd": 0.0, "wall_s": 3.0, "reason": "no_verdict", "verdict_candidates": 0,
                    "inconclusive_candidates": 3, "infrastructure_failed": True}, None
        return self.row(r, arm), None


def merge_v3_rows(wave_root: Path, sc: Scenario) -> None:
    """THE UNBUILT MERGE STEP (task-32 report, item 3): v3_meshability_pass_ai runs in v3_eval, and its results must
    be converted to ArmRows and added to each shard before load_wave. Simulated here so the dry run can continue."""
    for i in range(1, N_SHARDS + 1):
        d = wave_root / f"shard-{i}-of-{N_SHARDS}"
        rows = json.loads((d / "rows.json").read_text())
        excluded = json.loads((d / "EXCLUDED.json").read_text())
        for r in sc.split:
            if not ev.shard_of(r.sha256, i, N_SHARDS):
                continue
            if sc.special(r, V3) == "out_of_scope":
                why = ra.arms_mod._usable(SimpleNamespace(counts_for_scoreboard=True, out_of_scope=True, panel=[]),
                                          "the output")
                excluded.append({"name": r.name, "sha256": r.sha256, "source": r.source, "arm": V3, "reason": why})
            else:
                rows.append(sc.row(r, V3))
        (d / "rows.json").write_text(json.dumps(rows, sort_keys=True))
        (d / "EXCLUDED.json").write_text(json.dumps(excluded))


def run_wave(tmp: Path, monkeypatch: pytest.MonkeyPatch, markers: Any, *, wave_name: str, wave_id: str,
             split_dir: Path, pool: Path, sc: Scenario) -> Path:
    wave = ev.WAVES[wave_name]
    plan = ev.plan_wave(wave, wave_id=wave_id, n_shards=N_SHARDS, markers=markers)
    inputs = stage_inputs(tmp, sc.split)
    wave_root = tmp / f"wave-{wave_name}"
    monkeypatch.setattr(ra, "ARM_RUNNER", sc.runner)
    monkeypatch.setattr(rp, "SPLITS_DIR", split_dir)
    for i in range(1, N_SHARDS + 1):
        out = wave_root / f"shard-{i}-of-{N_SHARDS}"
        argv = ["--mode", "harness", "--shard", f"{i}/{N_SHARDS}", "--inputs", str(inputs), "--out", str(out),
                "--pool", str(pool), "--commit", COMMIT, "--image", IMAGE, "--sources", "abc_fail,abc_pass,inject",
                "--wave-type", wave.wave_type, "--wave-id", wave_id, "--n-shards", str(N_SHARDS),
                "--ledger-path", str(tmp / f"ledger-{wave_name}-{i}.jsonl"), "--marker-bucket", BUCKET,
                "--h-mm", "0.5", "--file-timeout-s", "1", *plan.provenance_argv()]
        rc = ra.main(argv)
        prov = json.loads((out / "PROVENANCE.json").read_text())
        assert rc == 0, prov.get("error")
        assert prov["slot"] == plan.slot and prov["ledger"] == "shard" and prov["wave_id"] == wave_id
    return wave_root


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    store = ml.FileMarkerStore(tmp_path / "markers")
    ra.marker_probe.record_pass(store, backend=f"gcs/{BUCKET}", run_id="r", rounds=50, at=time.time())
    monkeypatch.setattr(ra, "make_marker_store", lambda bucket: (store, f"gcs/{bucket}"))
    return store


@pytest.fixture
def probe(tmp_path: Path) -> SimpleNamespace:
    """collect.py's CLI over the fake re-probe: it must accept the wave and write the three splits."""
    pool, rows, ctx = build_probe(tmp_path)
    split_dir = tmp_path / "splits"
    rc = ra_collect().main(["--from", str(ctx["probe_root"]), "--shards", str(N_SHARDS), "--dev", "20",
                            "--heldout", "20", "--clean", "10", "--out-dir", str(split_dir), "--pool", str(pool)])
    assert rc == 0
    collected = json.loads((ctx["probe_root"] / "collect-report.json").read_text())
    return SimpleNamespace(pool=pool, rows=rows, split_dir=split_dir, collected=collected)


def ra_collect() -> Any:
    import collect  # loaded beside run_arm by test_v4_harness_rows' sys.path entry

    return collect


# ------------------------------------------------------------------ the dry run

def test_collect_cuts_the_splits_keeps_unmeasurable_files_out_and_reports_reprobe_agreement(probe) -> None:
    rep = probe.collected["report"]
    assert probe.collected["accepted"] is True and probe.collected["problems"] == []
    assert rep["excluded_inconclusive"] == 1 and rep["no_solids_not_repairable"] == 1
    held = rp.read_split(probe.split_dir / "heldout.json")
    dev = rp.read_split(probe.split_dir / "dev.json")
    clean = rp.read_split(probe.split_dir / "clean.json")
    assert (len(dev), len(held), len(clean)) == (20, 20, 10)
    every = {r.sha256 for r in (*dev, *held, *clean)}
    assert sha("5", 0) not in every and sha("5", 1) not in every and sha("6", 0) not in every, \
        "no_solids, out_of_scope (unbounded) and an excluded file never reach a split"
    assert any(r.kind_status == report.DECLINED_STATUS for r in held), "the held-out split carries declined files"
    agree = rep["agreement"]
    assert agree["compared"] == 60 and agree["cheap_pass_full_fail"] == 2 and agree["cheap_fail_full_pass"] == 2


def test_heldout_report_dry_run_renders_every_task21_metric(tmp_path, monkeypatch, markers, probe) -> None:
    split_path = probe.split_dir / "heldout.json"
    sc = Scenario(rp.read_split(split_path))
    assert len(sc.main) >= 10, "each arm's one inconclusive file must stay under the 10% limit"
    wave_root = run_wave(tmp_path, monkeypatch, markers, wave_name="heldout", wave_id="ho-dry",
                         split_dir=probe.split_dir, pool=probe.pool, sc=sc)

    # what the harness wrote: the crash, the wall kill and the pool-member loss are EXCLUDED, never rows
    excluded = [x for i in (1, 2) for x in json.loads(
        (wave_root / f"shard-{i}-of-{N_SHARDS}" / "EXCLUDED.json").read_text())]
    reasons = {(x["sha256"], x["arm"]): x["reason"] for x in excluded}
    assert reasons[(sc.x, "input_only")].startswith("error: RuntimeError:") and "worker crashed" in reasons[(sc.x, "input_only")]
    assert reasons[(sc.x, "search_ai")].startswith("file_timeout: file wall clock 1s exceeded")
    assert reasons[(sc.y, "staged")].startswith("inconclusive:")
    assert len(reasons) == 3

    argv = ["--from", str(wave_root), "--wave", "heldout", "--shards", str(N_SHARDS), "--split", str(split_path),
            "--frozen-commit", COMMIT, "--markers-dir", str(tmp_path / "markers"), "--json", str(tmp_path / "r.json")]
    assert report.main(argv) == 1, "without the v3 rows the held-out report refuses (the merge step is not built)"
    merge_v3_rows(wave_root, sc)
    rc = report.main(argv)
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rc == 0 and rep["status"] == "OK" and rep["numbers_valid"], rep["warnings"]
    assert rep["primary_artifact"] and rep["slot_verified_against_store"] and rep["frozen_commit_checked"]

    # ---- expected numbers, counted here from the scenario (not from report.py)
    n_main = len(sc.main)
    unmeasured = {("input_only", sc.x), ("search_det", sc.x), ("search_ai", sc.x), ("staged", sc.y), (V3, sc.y)}
    arms = (*ARMS4, V3)
    for a in arms:
        mine = [r for r in sc.main if (a, r.sha256) not in unmeasured]
        b = rep["arms"][a]
        clean = sum(sc.clean_out(r, a) for r in mine) if a != "input_only" else 0
        assert b["measured"] and b["inconclusive"] == 1 and b["files"] == n_main, a
        assert (b["clean_out"], b["clean_in"], b["clean_gain"]) == (clean, 0, clean), a  # clean gain
        assert b["regressions"]["clean_to_unclean"] == 0, a  # the held-out set is all failing inputs
        assert b["regressions"]["fewer_panel_passes"] == (1 if a == "staged" else 0), a
    # cost: model dollars per arm (search_ai metered, v3 not metered) and wall per file
    ai_rows = [r for r in sc.split if (("search_ai", r.sha256) not in unmeasured)]
    assert rep["arms"]["search_ai"]["model_usd"]["total"] == pytest.approx(0.25 * len(ai_rows))
    assert rep["arms"][V3]["model_usd"]["total"] is None and rep["spend"]["unmetered_arms"] == [V3]
    assert rep["spend"]["model_usd_total"] == pytest.approx(0.25 * len(ai_rows)) and rep["spend"]["cap_usd"] == 250
    # an EXCLUDED file is billed 0 s of wall (it has no row to carry its time): staged lost Y, so 2 s x the rest
    assert rep["arms"]["staged"]["wall_s"]["total"] == pytest.approx(2.0 * (len(sc.split) - 1))
    # the paired set and its fraction, with the threshold beside it (amendment 3)
    paired = [r for r in sc.main if r.sha256 not in (sc.x, sc.y)]
    ps = rep["paired_set"]
    assert (ps["n_files"], ps["of"]) == (len(paired), n_main)
    assert ps["fraction"] == pytest.approx(len(paired) / n_main) and ps["min_fraction"] == report.MIN_PAIRED_FRACTION
    # the marginals: paired over the files BOTH arms judged
    ai = rep["marginals"]["ai_marginal (search_ai - search_det)"]
    both = [r for r in sc.main if r.sha256 != sc.x]
    gained = sum(sc.clean_out(r, "search_ai") for r in both) - sum(sc.clean_out(r, "search_det") for r in both)
    assert (ai["files_gained"], ai["n_paired"]) == (gained, len(both)) and gained > 0
    det = rep["marginals"]["deterministic_marginal (staged - input_only)"]
    both = [r for r in sc.main if r.sha256 not in (sc.x, sc.y)]
    assert (det["files_gained"], det["n_paired"]) == (sum(sc.clean_out(r, "staged") for r in both), len(both))
    # the declined stratum: out of every rate, on its own line, one unexpectedly clean
    ds = rep["declined_stratum"]
    assert ds["files"] == len(sc.declined) and ds["arms"]["staged"]["unexpectedly_clean"] == 1
    assert ds["arms"]["staged"]["declined"] == len(sc.declined) - 1
    # cheap/full agreement of the search's ranking tier
    cf = rep["cheap_full_agreement"]
    assert cf["measured"] and cf["rows_without_data"] == 0
    expect = dict.fromkeys(CELLS, 0)
    for a in report.SEARCH_ARMS:
        for r in sc.split:
            if sc.special(r, a) is None:
                for k, v in sc.cheap_full(r, a, sc.clean_out(r, a)).items():
                    expect[k] += v
    assert {k: cf["pooled"][k] for k in CELLS} == expect and expect["cheap_pass_full_fail"] > 0

    # ---- the rendered text carries each metric
    text = report.render_text(rep)
    for needle in ("PER-ARM gain", "PAIRED gain", "regress (c->u / fewer panel)", "$ total", "wall/file s",
                   "ai_marginal (search_ai - search_det)", "deterministic_marginal (staged - input_only)",
                   "## Cheap/full agreement", f"paired fraction {len(paired) / n_main:.1%} (minimum 80%)",
                   "## Declined stratum", "not metered", "PRIMARY ARTIFACT", "of the $250 wave cap"):
        assert needle in text, needle


def test_clean_control_dry_run_counts_both_regression_kinds(tmp_path, monkeypatch, markers, probe) -> None:
    split_path = probe.split_dir / "clean.json"
    sc = Scenario(rp.read_split(split_path), clean_control=True)
    wave_root = run_wave(tmp_path, monkeypatch, markers, wave_name="clean_control", wave_id="cc-dry",
                         split_dir=probe.split_dir, pool=probe.pool, sc=sc)
    merge_v3_rows(wave_root, sc)
    out = tmp_path / "c.json"
    rc = report.main(["--from", str(wave_root), "--wave", "clean_control", "--shards", str(N_SHARDS),
                      "--split", str(split_path), "--json", str(out)])
    rep = json.loads(out.read_text())
    staged, det = rep["arms"]["staged"]["regressions"], rep["arms"]["search_det"]["regressions"]
    assert (staged["clean_to_unclean"], staged["fewer_panel_passes"]) == (1, 1)
    assert (det["clean_to_unclean"], det["fewer_panel_passes"]) == (1, 0)
    assert rep["arms"]["search_ai"]["regressions"]["either"] == 0
    assert rep["spend"]["cap_usd"] == ev.wave_cap_usd("normal")  # amendment 4: the normal cap, reported
    # FINDING (handed to the lead): on the clean control the BEST possible outcome - an arm that keeps every file
    # clean - trips the constant-outcome warning, so the clean-control report is UNRELIABLE exactly when it passes.
    assert rc == 2 and rep["status"] == "UNRELIABLE"
    assert any("every conclusive file has the same outcome (10/10 clean)" in w for w in rep["warnings"])
