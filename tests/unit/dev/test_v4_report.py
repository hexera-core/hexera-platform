"""v4_eval report: the arm-wave comparison. Hermetic: shard directories are built in tmp_path from fake rows;
no checker, model, bucket or subprocess is reached. Every test that says "refused" has a sibling that
proves the gate lets a good wave through, so none of the refusals is vacuous."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
import report  # noqa: E402

ev, rp, collect = report.ev, report.rp, report._collect
COMMIT, IMAGE = "a" * 40, "img@sha256:" + "b" * 64
PANEL = 3


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"a test reached a subprocess/network: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _sha(i: int) -> str:
    return f"{i:064x}"


KINDS = ["plc_intersection", "plc_open_shell", "valid_other", "gmsh_sliver"]


def split_rows(n: int = 40, split: str = "dev", source: str = "abc_fail") -> list:
    clean = split == "clean"
    return [rp.ProbeRow(f"f{i}.step", _sha(i), source, {"tier": "full", "clean": clean,
                                                        "counts_for_scoreboard": True},
                        None if clean else "panel", "" if clean else KINDS[i % 4], None) for i in range(n)]


def _fixed(i: int, arm: str) -> bool:
    """Which fake files each arm repairs. Distinct by construction: nested, not equal."""
    return {"input_only": False, "staged": i % 4 == 0, "search_det": i % 4 == 0 or i % 4 == 1,
            "search_ai": i % 2 == 0 or i % 4 == 1, "v3_meshability_pass_ai": i % 8 == 0}[arm]


def arm_row(base, i, arm, *, inconclusive=False, model=None, wall=10.0, **over):
    spend = (0.4 if arm == "search_ai" else 0.0) if model is None else model
    if inconclusive:
        return {"name": base.name, "sha256": base.sha256, "source": base.source, "arm": arm,
                "inconclusive": True, "model_usd": spend, "wall_s": wall}
    in_clean = bool(base.verdict_summary["clean"])
    in_pass = PANEL if in_clean else 1
    fixed = in_clean or _fixed(i, arm)
    out_pass = PANEL if fixed else 1
    row = {"name": base.name, "sha256": base.sha256, "source": base.source, "arm": arm,
           "inconclusive": False, "model_usd": spend, "wall_s": wall, "in_clean": in_clean,
           "in_panel_passes": in_pass, "out_valid": True, "out_faithful_ok": True,
           "out_panel_passes": out_pass, "panel_size": PANEL, "out_tier": "full", "out_clean": fixed}
    row.update(over)
    return row


def build(root: Path, wave, split, *, n_shards=2, rows_for=None, prov=None, excluded=None, skip=(), fail=()):
    """Write shard-I-of-N dirs the way the arm harness would, partitioned by hash. `rows_for(base, i, arm)`."""
    rows_for = rows_for or (lambda base, i, arm: arm_row(base, i, arm))
    for s in range(1, n_shards + 1):
        if s in skip:
            continue
        d = root / f"shard-{s}-of-{n_shards}"
        d.mkdir(parents=True)
        mine = [(i, b) for i, b in enumerate(split) if ev.shard_of(b.sha256, s, n_shards)]
        gone = {x["sha256"] for x in (excluded or [])}
        rows = [rows_for(b, i, a) for i, b in mine if b.sha256 not in gone for a in wave.arms]
        p = {"commit": COMMIT, "image": IMAGE, "exit_code": 1 if s in fail else 0, "wall_seconds": 100.0 * s,
             "wave": wave.name, "wave_id": "wv1", "wave_type": wave.wave_type, "split": wave.split,
             "arms": list(wave.arms), "wave_cap_usd": wave.cap_usd, "ledger": "shard"}
        p.update(prov or {})
        (d / "PROVENANCE.json").write_text(json.dumps(p))
        (d / "rows.json").write_text(json.dumps(rows))
        (d / "EXCLUDED.json").write_text(json.dumps([x for x in (excluded or []) if ev.shard_of(x["sha256"], s, n_shards)]))


WAVE = ev.WAVES["dev_checkpoint"]  # 4 arms, normal-ish: checkpoint cap 300


def load(root, split, wave=WAVE, **kw):
    kw.setdefault("n_shards", 2)
    return report.load_wave(root, wave, split, **kw)


@pytest.fixture
def good(tmp_path):
    split = split_rows()
    build(tmp_path, WAVE, split)
    return tmp_path, split


def test_the_good_wave_loads_and_the_arms_differ(good) -> None:
    root, split = good
    rep = report.build_report(load(root, split))
    a = rep["arms"]
    assert rep["status"] == "OK" and rep["warnings"] == []
    assert a["input_only"]["clean_out"] == 0
    # 40 files, nested repair sets: staged 10, search_det 20, search_ai 30
    assert (a["staged"]["clean_out"], a["search_det"]["clean_out"], a["search_ai"]["clean_out"]) == (10, 20, 30)
    assert a["search_ai"]["clean_rate"] == 0.75 and a["search_ai"]["clean_gain"] == 30
    assert rep["marginals"]["ai_marginal (search_ai - search_det)"]["value"] == pytest.approx(0.25)
    assert rep["marginals"]["deterministic_marginal (staged - input_only)"]["files_gained"] == 10
    assert a["search_ai"]["model_usd"]["total"] == pytest.approx(16.0)
    assert a["staged"]["model_usd"]["total"] == 0
    assert a["staged"]["wall_s"]["per_file_mean"] == 10.0
    assert set(a["staged"]["by_defect_kind"]) == set(KINDS)
    assert a["staged"]["by_defect_kind"]["plc_intersection"]["clean"] == 10  # i%4==0 is KINDS[0]
    assert a["staged"]["by_defect_kind"]["plc_open_shell"]["clean"] == 0
    assert a["staged"]["layers"] == {"valid": 40, "faithful_ok": 40, "panel_all_pass": 10, "clean": 10}


def test_inconclusive_is_counted_separately_never_as_pass_or_fail(tmp_path) -> None:
    split = split_rows()
    inc = {s.sha256 for s in split[:3]}  # 3 of 40 = 7.5%, under the limit
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "staged" and b.sha256 in inc)))
    rep = report.build_report(load(tmp_path, split))
    st = rep["arms"]["staged"]
    assert st["inconclusive"] == 3 and st["conclusive"] == 37 and st["measured"]
    # f0 (a staged-repaired file) is among the inconclusive: 10 repaired - 1 = 9 clean of 37 judged
    assert st["clean_out"] == 9 and st["clean_rate"] == pytest.approx(9 / 37)
    assert st["clean_rate"] != pytest.approx(9 / 40) and st["clean_rate"] != pytest.approx(10 / 37)  # neither folded
    assert st["regressions"]["either"] == 0  # an inconclusive file is not a regression either
    assert "3/40" in report.render_text(rep)


def test_thirty_percent_inconclusive_is_not_measured_and_loud(tmp_path) -> None:
    split = split_rows()
    inc = {s.sha256 for s in split[:12]}  # 30%
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "search_ai" and b.sha256 in inc)))
    rep = report.build_report(load(tmp_path, split))
    ai = rep["arms"]["search_ai"]
    assert not ai["measured"] and ai["clean_rate"] is None and ai["clean_gain"] is None
    assert rep["status"] == "UNRELIABLE"
    assert rep["marginals"]["ai_marginal (search_ai - search_det)"]["value"] is None
    text = report.render_text(rep)
    assert "NOT MEASURED" in text.split("\n\n")[1] + text.split("\n\n")[2]  # up in the warnings, not a footnote
    assert "withheld" in text


def test_an_all_inconclusive_arm_has_no_rate_not_zero(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "staged")))
    st = report.build_report(load(tmp_path, split))["arms"]["staged"]
    assert st["conclusive"] == 0 and st["clean_rate"] is None and not st["measured"]


def test_excluded_files_are_inconclusive_for_every_arm(tmp_path) -> None:
    split = split_rows()
    x = {"name": split[5].name, "sha256": split[5].sha256, "source": "abc_fail", "reason": "timeout: file wall"}
    build(tmp_path, WAVE, split, excluded=[x])
    rep = report.build_report(load(tmp_path, split))
    assert all(b["inconclusive"] == 1 and b["conclusive"] == 39 for b in rep["arms"].values())


def test_regression_flagged_when_panel_passes_drop(tmp_path) -> None:
    split = split_rows()

    def rows_for(b, i, a):
        if a == "search_det" and i in (3, 7):  # input passed 1 panel member; the output passes none
            return arm_row(b, i, a, out_panel_passes=0, out_clean=False)
        return arm_row(b, i, a)

    build(tmp_path, WAVE, split, rows_for=rows_for)
    reg = report.build_report(load(tmp_path, split))["arms"]["search_det"]["regressions"]
    assert reg == {"clean_to_unclean": 0, "fewer_panel_passes": 2, "either": 2}


def test_clean_input_made_unclean_is_the_other_regression_kind(tmp_path) -> None:
    split = split_rows(20, "clean", "occt")
    wave = ev.WAVES["clean_control"]

    def rows_for(b, i, a):
        if a == "staged" and i == 4:
            return arm_row(b, i, a, out_valid=False, out_clean=False)
        return arm_row(b, i, a)

    build(tmp_path, wave, split, rows_for=rows_for)
    rep = report.build_report(load(tmp_path, split, wave))
    assert rep["arms"]["staged"]["regressions"]["clean_to_unclean"] == 1
    assert rep["arms"]["search_det"]["regressions"]["clean_to_unclean"] == 0


# ------------------------------------------------------------------ the held-out contract

def test_heldout_report_requires_frozen_commit(tmp_path) -> None:
    wave, split = ev.WAVES["heldout"], split_rows(40, "heldout")
    build(tmp_path, wave, split)
    for bad in (None, "", "abc", "A" * 40):
        with pytest.raises(report.ReportRefused, match="frozen-commit"):
            report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=bad)
    with pytest.raises(report.ReportRefused, match="not the frozen"):
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit="c" * 40)
    rep = report.build_report(report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT))
    assert rep["primary_artifact"] and rep["frozen_commit_checked"] and rep["split"] == "heldout"
    assert "PRIMARY ARTIFACT" in report.render_text(rep)


def test_dev_report_is_the_same_code_path_and_needs_no_frozen_commit(good) -> None:
    root, split = good
    rep = report.build_report(load(root, split))
    assert not rep["primary_artifact"] and rep["split"] == "dev"


def test_heldout_is_refused_for_a_tuning_purpose(tmp_path) -> None:
    wave, split = ev.WAVES["heldout"], split_rows(40, "heldout")
    build(tmp_path, wave, split)
    with pytest.raises(ev.HeldOutInTuning):
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT, purpose="tuning")
    with pytest.raises(report.ReportRefused):
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT, purpose="memory")


# ------------------------------------------------------------------ collect's refusals reach here

def test_a_partial_wave_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, n_shards=3, skip=(3,))
    with pytest.raises(collect.CollectRefused, match="missing shard"):
        load(tmp_path, split, n_shards=3)


def test_a_failed_shard_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, fail=(2,))
    with pytest.raises(collect.CollectRefused, match="exit_code"):
        load(tmp_path, split)


def test_a_mixed_commit_and_a_mixed_image_are_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split)
    p = tmp_path / "shard-2-of-2/PROVENANCE.json"
    doc = json.loads(p.read_text())
    p.write_text(json.dumps({**doc, "commit": "d" * 40}))
    with pytest.raises(collect.CollectRefused, match="commit"):
        load(tmp_path, split)
    p.write_text(json.dumps({**doc, "image": "other"}))
    with pytest.raises(collect.CollectRefused, match="image"):
        load(tmp_path, split)


def test_the_planned_shard_count_must_match(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split)
    with pytest.raises(collect.CollectRefused, match="plan has 3"):
        load(tmp_path, split, n_shards=3)


def test_a_file_in_the_wrong_shard_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split)
    a, b = tmp_path / "shard-1-of-2/rows.json", tmp_path / "shard-2-of-2/rows.json"
    a.write_text(json.dumps(json.loads(a.read_text()) + json.loads(b.read_text())[:1]))
    with pytest.raises(collect.CollectRefused, match="partition"):
        load(tmp_path, split)


def test_report_cannot_be_built_from_raw_rows_or_a_hand_made_result() -> None:
    with pytest.raises(TypeError):
        report.build_report([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="load_wave"):
        report.WaveResult(WAVE, "w", COMMIT, IMAGE, (), {}, 0, (), False)


# ------------------------------------------------------------------ licence hold

def test_fusion_360_is_refused_in_a_row_and_in_a_split(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, source="f360" if i == 2 else b.source))
    with pytest.raises(rp.DisallowedSource):
        load(tmp_path, split)
    with pytest.raises(rp.DisallowedSource):
        load(tmp_path, split_rows(source="f360"))


def test_fusion_360_in_an_excluded_entry_is_refused(tmp_path) -> None:
    split = split_rows()
    x = {"name": "f.step", "sha256": split[5].sha256, "source": "f360", "reason": "timeout"}
    build(tmp_path, WAVE, split, excluded=[x])
    with pytest.raises(rp.DisallowedSource):
        load(tmp_path, split)


# ------------------------------------------------------------------ rows that cannot be trusted

@pytest.mark.parametrize("over,match", [
    ({"out_clean": True, "out_panel_passes": 2}, "stored clean"),            # claims clean, panel says no
    ({"out_clean": True, "out_faithful_ok": False, "out_panel_passes": 3}, "stored clean"),
    ({"out_clean": False, "out_panel_passes": 3}, "stored clean"),           # all parts pass, says unclean
    ({"out_tier": "cheap"}, "full panel"),
    ({"out_panel_passes": 4}, "impossible"),
    ({"out_valid": None}, "missing"),
])
def test_a_row_whose_parts_do_not_support_its_clean_flag_is_refused(tmp_path, over, match) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, **over) if (a, i) == ("staged", 6) else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match=match):
        load(tmp_path, split)


def test_model_spend_on_a_deterministic_arm_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, model=0.5 if a == "search_det" else None))
    with pytest.raises(report.ReportRefused, match="no metered model"):
        load(tmp_path, split)


def test_a_model_arm_without_a_shard_ledger_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, prov={"ledger": "unset"})
    with pytest.raises(report.ReportRefused, match="ledger"):
        load(tmp_path, split)


def test_spend_over_the_shard_slice_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, model=8.0 if a == "search_ai" else None))
    # 40 files x $8 = $320 > the $300 checkpoint cap; each 2-shard slice is $150
    with pytest.raises(report.ReportRefused, match="did not bind"):
        load(tmp_path, split)


def test_a_wave_run_under_another_wave_type_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, prov={"wave_type": "normal", "wave_cap_usd": 50.0})
    with pytest.raises(report.ReportRefused, match="wave type"):
        load(tmp_path, split)


def test_a_missing_file_arm_pair_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split)
    p = tmp_path / "shard-1-of-2/rows.json"
    rows = json.loads(p.read_text())
    p.write_text(json.dumps(rows[:-1]))
    with pytest.raises(report.ReportRefused, match="no row"):
        load(tmp_path, split)


def test_a_duplicate_row_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split)
    p = tmp_path / "shard-1-of-2/rows.json"
    rows = json.loads(p.read_text())
    p.write_text(json.dumps(rows + rows[:1]))
    with pytest.raises(report.ReportRefused, match="two rows"):
        load(tmp_path, split)


# ------------------------------------------------------------------ fakes that look wrong must look wrong

def test_a_constant_all_clean_verdict_cannot_pass_the_input_check(tmp_path) -> None:
    """A fake checker that says 'clean' for everything: the split says these inputs are NOT clean."""
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, in_clean=True, in_panel_passes=3,
                                                                 out_clean=True, out_panel_passes=3))
    with pytest.raises(report.ReportRefused, match="split says clean"):
        load(tmp_path, split)


def test_input_only_that_fixes_things_is_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, out_clean=True, out_panel_passes=3)
          if (a, i) == ("input_only", 1) else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match="did not pass the input through"):
        load(tmp_path, split)


def test_arms_that_saw_different_inputs_are_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, in_panel_passes=2)
          if (a, i) == ("staged", 1) else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match="different inputs"):
        load(tmp_path, split)


def test_a_fake_that_gives_every_arm_the_same_result_is_flagged(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, out_clean=(i % 4 == 0), out_panel_passes=3 if i % 4 == 0 else 1)
          if a != "input_only" else arm_row(b, i, a))
    rep = report.build_report(load(tmp_path, split))
    assert rep["status"] == "UNRELIABLE"
    assert any("indistinguishable" in w for w in rep["warnings"])
    assert "DO NOT READ" in report.render_text(rep)


def test_a_constant_unclean_arm_is_flagged(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, out_clean=False, out_panel_passes=1)
          if a == "search_ai" else arm_row(b, i, a))
    rep = report.build_report(load(tmp_path, split))
    assert any("search_ai" in w and "same outcome" in w for w in rep["warnings"])
    assert not any(w.startswith("input_only:") for w in rep["warnings"])  # the floor may be constant
    assert any("input_only and search_ai are indistinguishable" in w for w in rep["warnings"])


# ------------------------------------------------------------------ the command line

def test_cli_writes_json_and_exit_codes(tmp_path, capsys) -> None:
    split = split_rows()
    build(tmp_path / "w", WAVE, split)
    sp = tmp_path / "dev.json"
    sp.write_text(json.dumps({"split": "dev", "files": [asdict(r) for r in split]}))
    out = tmp_path / "r.json"
    args = ["--from", str(tmp_path / "w"), "--wave", "dev_checkpoint", "--shards", "2", "--split", str(sp), "--json", str(out)]
    assert report.main(args) == 0
    assert json.loads(out.read_text())["arms"]["search_ai"]["clean_out"] == 30
    assert report.main([*args[:-2], "--shards", "3"]) == 1  # refused: exit 1, not a report
    assert "REFUSED" in capsys.readouterr().err


def test_cli_refuses_a_split_file_for_another_split(tmp_path) -> None:
    split = split_rows()
    build(tmp_path / "w", WAVE, split)
    sp = tmp_path / "heldout.json"
    sp.write_text(json.dumps({"split": "heldout", "files": [asdict(r) for r in split]}))
    assert report.main(["--from", str(tmp_path / "w"), "--wave", "dev_checkpoint", "--shards", "2", "--split", str(sp)]) == 1
