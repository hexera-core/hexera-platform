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
    """Which fake files each arm repairs. The sets OVERLAP BUT ARE NOT NESTED, so a report that mixed up two arms'
    files, or took a union or an intersection where it should not, cannot land on the right count by accident.
    Tests compare the report with `expected_clean`, an independent count over this function, never with
    a number typed in beside it."""
    return {"input_only": False, "staged": i % 4 == 0, "search_det": i % 3 == 0,
            "search_ai": i % 2 == 0 or i % 7 == 1, "v3_meshability_pass_ai": i % 8 == 0}[arm]


def expected_clean(arm: str, idx) -> int:
    return sum(_fixed(i, arm) for i in idx)


def arm_row(base, i, arm, *, inconclusive=False, model=None, wall=10.0, **over):
    spend = (0.4 if arm == "search_ai" else 0.0) if model is None else model
    if inconclusive:
        return {"name": base.name, "sha256": base.sha256, "source": base.source, "arm": arm,
                "inconclusive": True, "model_usd": spend, "wall_s": wall, **over}
    in_clean = bool(base.verdict_summary["clean"])
    in_pass = PANEL if in_clean else 1
    fixed = in_clean or _fixed(i, arm)
    out_pass = PANEL if fixed else 1
    row = {"name": base.name, "sha256": base.sha256, "source": base.source, "arm": arm,
           "inconclusive": False, "model_usd": spend, "wall_s": wall, "in_clean": in_clean,
           "in_panel_passes": in_pass, "out_valid": True, "out_faithful_ok": True,
           "out_panel_passes": out_pass, "panel_size": PANEL, "out_tier": "full", "out_clean": fixed,
           "in_valid": True, "in_faithful_ok": True}
    if arm in report.SEARCH_ARMS:  # SearchResult's own words (Task 18b): a search that was measured
        row.update(reason="clean" if fixed else "patience", verdict_candidates=0 if in_clean else 5,
                   inconclusive_candidates=0)
    row.update(over)
    return row


def _call_wall() -> float:
    from meshpipeline.cad.repair_v4.limits import CHECKER_CALL_TIMEOUT_S

    return float(CHECKER_CALL_TIMEOUT_S)


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
             "arms": list(wave.arms), "wave_cap_usd": wave.cap_usd, "ledger": "shard",
             # the walls the shard ran with, as run_arm records them
             "file_timeout_s": ev.DEFAULT_FILE_TIMEOUT_S, "check_valid_timeout_s": _call_wall(),
             "slot": (ev.slot_keys(wave.wave_type) or [None])[0]}
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
    allf = range(40)
    assert rep["status"] == "OK" and rep["warnings"] == [] and rep["numbers_valid"] is True
    # every count is checked against an independent count over the fixture, and the sets are not nested
    for arm in ("input_only", "staged", "search_det", "search_ai"):
        assert a[arm]["clean_out"] == expected_clean(arm, allf)
        assert a[arm]["clean_rate"] == pytest.approx(expected_clean(arm, allf) / 40)
        assert a[arm]["paired"]["clean"] == expected_clean(arm, allf) and a[arm]["paired"]["n"] == 40
    assert len({a[x]["clean_out"] for x in ("staged", "search_det", "search_ai")}) == 3  # (10, 14, 23)
    ai_det = rep["marginals"]["ai_marginal (search_ai - search_det)"]
    assert ai_det["files_gained"] == expected_clean("search_ai", allf) - expected_clean("search_det", allf)
    assert ai_det["value"] == pytest.approx(ai_det["files_gained"] / 40)
    assert rep["marginals"]["deterministic_marginal (staged - input_only)"]["files_gained"] == expected_clean("staged", allf)
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
    # frozen commit checked, but the slot was only read from the shards' own provenance: NOT the primary artifact yet
    assert rep["frozen_commit_checked"] and rep["split"] == "heldout" and rep["heldout_split"]
    assert rep["primary_artifact"] is False
    assert "PRIMARY ARTIFACT" not in report.render_text(rep)


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
    """Each row is at the $1.00 per-file ceiling (so the per-file check passes); 100 files make ~$50 per shard
    against a $25 slice of the $50 normal-wave cap."""
    wave, split = ev.WAVES["dev_search"], split_rows(100)
    build(tmp_path, wave, split, rows_for=lambda b, i, a: arm_row(b, i, a, model=1.0 if a == "search_ai" else None))
    with pytest.raises(report.ReportRefused, match="did not bind"):
        load(tmp_path, split, wave)


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
    assert all("deterministic arms truly agree" in w for w in rep["warnings"] if "indistinguishable" in w)
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
    doc = json.loads(out.read_text())
    assert doc["arms"]["search_ai"]["clean_out"] == expected_clean("search_ai", range(40)) and doc["numbers_valid"] is True
    assert report.main([*args[:-2], "--shards", "3"]) == 1  # refused: exit 1, not a report
    assert "REFUSED" in capsys.readouterr().err


def test_cli_refuses_a_split_file_for_another_split(tmp_path) -> None:
    split = split_rows()
    build(tmp_path / "w", WAVE, split)
    sp = tmp_path / "heldout.json"
    sp.write_text(json.dumps({"split": "heldout", "files": [asdict(r) for r in split]}))
    assert report.main(["--from", str(tmp_path / "w"), "--wave", "dev_checkpoint", "--shards", "2", "--split", str(sp)]) == 1


def test_the_inconclusive_rate_is_printed_on_every_arm_even_under_the_limit(tmp_path) -> None:
    split = split_rows()
    inc = {split[0].sha256}  # 1 of 40 = 2.5%, well under the limit
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "staged" and b.sha256 in inc)))
    text = report.render_text(report.build_report(load(tmp_path, split)))
    rows = {ln.split("|")[1].strip(): ln.split("|") for ln in text.splitlines() if ln.startswith("| ") and "arm" not in ln[:8]}
    assert rows["staged"][4].strip() == "2.5%" and rows["search_ai"][4].strip() == "0.0%"
    assert rows["staged"][5].strip() != "withheld"  # under the limit: rate shown AND clean rate shown


def test_cli_refuses_the_heldout_split_for_a_tuning_purpose(tmp_path, capsys) -> None:
    """The firewall hook, exercised through its caller: it must refuse when it IS called."""
    split = split_rows(40, "heldout")
    build(tmp_path / "w", ev.WAVES["heldout"], split)
    sp = tmp_path / "heldout.json"
    sp.write_text(json.dumps({"split": "heldout", "files": [asdict(r) for r in split]}))
    rc = report.main(["--from", str(tmp_path / "w"), "--wave", "heldout", "--shards", "2", "--split", str(sp),
                      "--frozen-commit", COMMIT, "--purpose", "tuning"])
    assert rc == 1 and "held-out" in capsys.readouterr().err
    assert report.main(["--from", str(tmp_path / "w"), "--wave", "heldout", "--shards", "2", "--split", str(sp),
                        "--frozen-commit", COMMIT]) == 0


# ================================================================== fix round: the six Importants

def _kind_section(text: str, arm: str) -> list[list[str]]:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"## {arm}: by defect kind"))
    rows = []
    for ln in lines[start + 3:]:
        if not ln.startswith("|"):
            break
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    return rows


# ---- 1. NOT MEASURED withholds everything a rate is recoverable from

def test_not_measured_withholds_every_figure_a_rate_can_be_recovered_from(tmp_path) -> None:
    split = split_rows()
    # 12 inconclusive search_ai files, all in kinds 1-3: kind 0 (plc_intersection) has NONE, so the per-kind 10%
    # test alone would still print its rate beside the NOT MEASURED warning
    inc = {b.sha256 for i, b in enumerate(split) if i % 4 != 0 and i < 16}
    assert len(inc) == 12
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "search_ai" and b.sha256 in inc)))
    rep = report.build_report(load(tmp_path, split))
    ai = rep["arms"]["search_ai"]
    assert not ai["measured"] and rep["numbers_valid"] is False and rep["status"] == "UNRELIABLE"
    assert ai["clean_in"] is None and ai["clean_out"] is None and ai["clean_rate"] is None
    assert ai["clean_gain"] is None and ai["clean_gain_rate"] is None
    assert set(ai["layers"].values()) == {None} and set(ai["regressions"].values()) == {None}
    assert ai["paired"]["clean_rate"] is None and ai["paired"]["clean"] is None
    assert all(k["clean"] is None and k["clean_rate"] is None for k in ai["by_defect_kind"].values())
    assert ai["by_defect_kind"]["plc_intersection"]["inconclusive"] == 0  # the kind that WOULD have passed its own test
    text = report.render_text(rep)
    assert {r[-1] for r in _kind_section(text, "search_ai")} == {"withheld"} and {r[-2] for r in _kind_section(text, "search_ai")} == {"withheld"}
    # the sibling: an arm that WAS measured keeps its figures, and the document is valid
    assert rep["arms"]["staged"]["clean_out"] == expected_clean("staged", range(40))
    assert all(r[-1] != "withheld" for r in _kind_section(text, "staged"))


def test_numbers_valid_is_true_only_when_there_are_no_warnings(good) -> None:
    root, split = good
    assert report.build_report(load(root, split))["numbers_valid"] is True


# ---- 2. the paired marginal equalises the denominator

def test_paired_column_equalises_denominators_when_dropouts_differ(tmp_path) -> None:
    wave, split = ev.WAVES["dev_search"], split_rows()
    det_drop = {split[i].sha256 for i in (1, 2, 4)}   # search_det loses files it would NOT have fixed (flattered)
    ai_drop = {split[i].sha256 for i in (6, 10, 12)}  # search_ai loses files it WOULD have fixed (depressed)
    build(tmp_path, wave, split, rows_for=lambda b, i, a: arm_row(
        b, i, a, inconclusive=(a == "search_det" and b.sha256 in det_drop) or (a == "search_ai" and b.sha256 in ai_drop)))
    rep = report.build_report(load(tmp_path, split, wave))
    det, ai = rep["arms"]["search_det"], rep["arms"]["search_ai"]
    shared = [i for i in range(40) if i not in (1, 2, 4, 6, 10, 12)]
    assert rep["paired_set"] == {"arms": ["search_det", "search_ai"], "n_files": 34, "of": 40,
                              "fraction": 34 / 40, "min_fraction": report.MIN_PAIRED_FRACTION,
                              "excluded_not_measured": []}
    assert det["conclusive"] == ai["conclusive"] == 37 and det["paired"]["n"] == ai["paired"]["n"] == 34
    # own denominators are 37 for both, but the files behind them differ; the paired column uses the same 34
    assert det["paired"]["clean"] == expected_clean("search_det", shared)
    assert ai["paired"]["clean"] == expected_clean("search_ai", shared)
    assert det["paired"]["clean_rate"] == pytest.approx(expected_clean("search_det", shared) / 34)
    assert ai["paired"]["clean_rate"] == pytest.approx(expected_clean("search_ai", shared) / 34)
    assert det["clean_rate"] != pytest.approx(det["paired"]["clean_rate"])  # flattered
    assert ai["clean_rate"] != pytest.approx(ai["paired"]["clean_rate"])    # depressed
    text = report.render_text(rep)
    assert "Only the PAIRED columns" in text and "NOT comparable" in text and "PAIRED clean rate" in text
    assert "PER-ARM clean rate" in text and "(n=34)" in text


def test_a_not_measured_arm_does_not_shrink_the_paired_set(tmp_path) -> None:
    split = split_rows()
    inc = {b.sha256 for b in split[:12]}
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "search_ai" and b.sha256 in inc)))
    rep = report.build_report(load(tmp_path, split))
    assert rep["paired_set"]["n_files"] == 40 and "search_ai" not in rep["paired_set"]["arms"]


# ---- 3. an arm that calls a model but is not metered is not "$0.00"

def test_v3_baseline_spend_is_not_metered_never_zero(tmp_path) -> None:
    wave, split = ev.WAVES["clean_control"], split_rows(20, "clean", "occt")
    build(tmp_path, wave, split)
    rep = report.build_report(load(tmp_path, split, wave))
    v3 = rep["arms"]["v3_meshability_pass_ai"]
    assert v3["spend_metered"] is False and v3["model_usd"]["total"] is None
    assert rep["spend"]["unmetered_arms"] == ["v3_meshability_pass_ai"]
    text = report.render_text(rep)
    line = next(ln for ln in text.splitlines() if ln.startswith("| v3_meshability_pass_ai"))
    assert "not metered" in line and "0.00" not in line
    assert "EXCLUDES v3_meshability_pass_ai" in text
    staged = next(ln for ln in text.splitlines() if ln.startswith("| staged"))
    assert "0.00" in staged  # the sibling: a deterministic arm really is $0


# ---- 4. the once-only slot is read back at report time

def _heldout(tmp_path, **prov):
    wave, split = ev.WAVES["heldout"], split_rows(40, "heldout")
    build(tmp_path, wave, split, prov=prov)
    return wave, split


class _Store:
    def __init__(self, d):
        self.d = d

    def get(self, k):
        return self.d.get(k)


def test_a_heldout_report_requires_the_slot_plan_wave_claimed(tmp_path) -> None:
    wave, split = _heldout(tmp_path)
    ok = report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT)
    assert ok.slot == "slot/held_out/1" and not ok.slot_verified  # present, store not consulted: said so
    assert "marker store NOT consulted" in report.render_text(report.build_report(ok))


@pytest.mark.parametrize("slot", [None, "", "slot/held_out/2", "slot/checkpoint/1"])
def test_a_heldout_wave_launched_around_plan_wave_is_refused(tmp_path, slot) -> None:
    wave, split = _heldout(tmp_path, slot=slot)
    with pytest.raises(report.ReportRefused, match="plan_wave"):
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT)


def test_a_checkpoint_wave_needs_its_slot_too(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, prov={"slot": None})
    with pytest.raises(report.ReportRefused, match="plan_wave"):
        load(tmp_path, split)


def test_shards_that_disagree_on_the_slot_are_refused(tmp_path) -> None:
    wave, split = _heldout(tmp_path)
    p = tmp_path / "shard-2-of-2/PROVENANCE.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "slot": "slot/held_out/9"}))
    with pytest.raises(report.ReportRefused):
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT)


def test_the_slot_is_verified_against_the_marker_store_when_given(tmp_path) -> None:
    wave, split = _heldout(tmp_path)
    mine = report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT,
                            markers=_Store({"slot/held_out/1": "wv1"}))
    assert mine.slot_verified and "held by this wave_id in the marker store" in report.render_text(report.build_report(mine))
    with pytest.raises(report.ReportRefused, match="held by"):  # someone else holds it: this is a second run
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT, markers=_Store({"slot/held_out/1": "other"}))
    with pytest.raises(report.ReportRefused, match="held by"):  # nobody claimed it at all
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT, markers=_Store({}))


def test_a_real_plan_wave_provenance_loads_end_to_end(tmp_path) -> None:
    store: dict[str, str] = {}

    class Markers:
        def get(self, k):
            return store.get(k)

        def create(self, k, v):
            if k in store:
                return False
            store[k] = v
            return True

    plan = ev.plan_wave(ev.WAVES["heldout"], wave_id="wv1", n_shards=2, markers=Markers())
    assert plan.provenance()["slot"] == "slot/held_out/1"
    wave, split = ev.WAVES["heldout"], split_rows(40, "heldout")
    build(tmp_path, wave, split, prov=plan.provenance())
    res = report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT, markers=Markers())
    assert res.slot_verified


def test_cli_verifies_the_slot_against_a_directory_marker_store(tmp_path) -> None:
    wave, split = ev.WAVES["heldout"], split_rows(40, "heldout")
    build(tmp_path / "w", wave, split)
    sp = tmp_path / "heldout.json"
    sp.write_text(json.dumps({"split": "heldout", "files": [asdict(r) for r in split]}))
    mk = tmp_path / "markers"
    args = ["--from", str(tmp_path / "w"), "--wave", "heldout", "--shards", "2", "--split", str(sp), "--frozen-commit", COMMIT,
            "--markers-dir", str(mk)]
    assert report.main(args) == 1  # an empty store: the slot was never claimed
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    assert FileMarkerStore(mk).create("slot/held_out/1", "wv1")
    assert report.main(args) == 0



# ---- 5. the row carries 18b's fields; "never measured" cannot be written down as "measured and failed"

def _search_rows(tmp_path, **over):
    split = split_rows()
    wave = ev.WAVES["dev_search"]
    build(tmp_path, wave, split, rows_for=lambda b, i, a: arm_row(b, i, a, **over)
          if (a, i) == ("search_ai", 6) else arm_row(b, i, a))
    return split, wave


@pytest.mark.parametrize("over", [
    # the exact path the brief calls worse than a pass: no verdict, start returned unchanged, written as a failure
    {"reason": "no_verdict", "out_clean": False, "out_panel_passes": 1, "verdict_candidates": 0, "inconclusive_candidates": 4},
    {"reason": "no_verdict", "inconclusive": False},
    {"reason": "patience", "verdict_candidates": 0, "inconclusive_candidates": 3, "out_clean": False, "out_panel_passes": 1},
    {"reason": "patience", "infrastructure_failed": True, "out_clean": False, "out_panel_passes": 1},
    {"reason": "no_verdict", "infrastructure_failed": False, "out_clean": False, "out_panel_passes": 1},
])
def test_a_search_that_measured_nothing_cannot_be_a_conclusive_failure(tmp_path, over) -> None:
    split, wave = _search_rows(tmp_path, **over)
    with pytest.raises(report.ReportRefused, match="nothing was measured|contradictory"):
        load(tmp_path, split, wave)


def test_the_same_search_marked_inconclusive_is_excluded_not_counted(tmp_path) -> None:
    split = split_rows()
    wave = ev.WAVES["dev_search"]
    build(tmp_path, wave, split, rows_for=lambda b, i, a: arm_row(
        b, i, a, inconclusive=True, reason="no_verdict", verdict_candidates=0, inconclusive_candidates=4,
        infrastructure_failed=True) if (a, i) == ("search_ai", 6) else arm_row(b, i, a))
    ai = report.build_report(load(tmp_path, split, wave))["arms"]["search_ai"]
    assert ai["inconclusive"] == 1 and ai["conclusive"] == 39
    assert ai["clean_out"] == expected_clean("search_ai", [i for i in range(40) if i != 6])  # not folded in as a failure


def test_a_real_search_failure_with_verdicts_is_still_counted_as_a_failure(tmp_path) -> None:
    """The sibling: patience with 5 usable verdicts and some inconclusive candidates IS a measured failure."""
    split, wave = _search_rows(tmp_path, reason="patience", verdict_candidates=5, inconclusive_candidates=2,
                               out_clean=False, out_panel_passes=1)
    rep = report.build_report(load(tmp_path, split, wave))
    assert rep["arms"]["search_ai"]["inconclusive"] == 0  # i=6 was fixed by the fixture; now a failure
    assert rep["arms"]["search_ai"]["clean_out"] == expected_clean("search_ai", range(40)) - 1


def test_a_conclusive_search_row_without_the_search_fields_is_refused(tmp_path) -> None:
    for drop in ("reason", "verdict_candidates", "inconclusive_candidates"):
        split, wave = _search_rows(tmp_path / drop, **{drop: None})
        with pytest.raises(report.ReportRefused, match="must carry"):
            load(tmp_path / drop, split, wave)


@pytest.mark.parametrize("over", [{"verdict_candidates": -1}, {"verdict_candidates": True}, {"inconclusive_candidates": 1.5},
                                  {"reason": 3}, {"infrastructure_failed": "no"}])
def test_malformed_search_fields_are_refused(tmp_path, over) -> None:
    split, wave = _search_rows(tmp_path, **over)
    with pytest.raises(report.ReportRefused):
        load(tmp_path, split, wave)


# ---- the minors

def test_a_files_model_spend_over_the_arms_ceiling_is_refused(tmp_path) -> None:
    wave, split = ev.WAVES["dev_search"], split_rows()
    build(tmp_path / "bad", wave, split, rows_for=lambda b, i, a: arm_row(b, i, a, model=1.5) if (a, i) == ("search_ai", 3) else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match="per-file ceiling"):
        load(tmp_path / "bad", split, wave)
    build(tmp_path / "ok", wave, split, rows_for=lambda b, i, a: arm_row(b, i, a, model=1.0) if (a, i) == ("search_ai", 3) else arm_row(b, i, a))
    load(tmp_path / "ok", split, wave)  # exactly at the ceiling is allowed


def test_input_only_that_changed_validity_or_faithfulness_is_refused(tmp_path) -> None:
    split = split_rows()
    for field in ("out_valid", "out_faithful_ok"):
        d = tmp_path / field
        build(d, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, out_clean=False, **{field: False})
              if (a, i) == ("input_only", 2) else arm_row(b, i, a))
        with pytest.raises(report.ReportRefused, match="did not pass the input through"):
            load(d, split)


def test_input_only_must_carry_the_input_layers(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, in_valid=None) if (a, i) == ("input_only", 2) else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match="in_valid"):
        load(tmp_path, split)


def test_arms_that_saw_different_input_layers_are_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, in_valid=False) if (a, i) == ("staged", 2) else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match="input layers"):
        load(tmp_path, split)


def test_the_detector_banner_says_what_a_false_positive_looks_like(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, out_clean=False, out_panel_passes=1)
          if a == "search_ai" else arm_row(b, i, a))
    text = report.render_text(report.build_report(load(tmp_path, split)))
    assert "deterministic arms truly agree" in text and "clean-control split" in text


def test_the_clean_control_wave_is_a_normal_wave_with_no_slot() -> None:
    """Confirmed against the plan: T4 (held-out + clean control) says only the held-out half is the $250 once-only
    run; the clean control carries no count limit of its own, so it is a normal $50 wave. If the owner means
    otherwise, WAVES['clean_control'] is the one line to change and this test names the assumption."""
    w = ev.WAVES["clean_control"]
    assert w.wave_type == "normal" and ev.slot_keys(w.wave_type) == []


def test_an_inconclusive_row_that_contradicts_its_own_infrastructure_flag_is_refused(tmp_path) -> None:
    split = split_rows()
    wave = ev.WAVES["dev_search"]
    build(tmp_path, wave, split, rows_for=lambda b, i, a: arm_row(
        b, i, a, inconclusive=True, reason="no_verdict", infrastructure_failed=False)
        if (a, i) == ("search_ai", 6) else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match="contradictory"):
        load(tmp_path, split, wave)


def test_the_paired_footnote_names_exactly_the_arms_that_were_included(tmp_path) -> None:
    split = split_rows()
    inc = {b.sha256 for b in split[:12]}  # search_ai NOT MEASURED, so it must be named as left out
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "search_ai" and b.sha256 in inc)))
    rep = report.build_report(load(tmp_path, split))
    text = report.render_text(rep)
    foot = next(ln for ln in text.splitlines() if ln.startswith("PAIRED columns cover"))
    listed = foot.split("arm(s): ")[1].split(";")[0].rstrip(".").split(", ")
    measured = [a for a, b in rep["arms"].items() if b["measured"]]
    assert listed == measured == rep["paired_set"]["arms"] and "search_ai" not in listed
    assert "left out because NOT MEASURED: search_ai" in foot and "40 of 40" in foot
    assert f"{len(measured)} measured" in foot
    # every included arm's paired n is that stated population; the left-out arm has none
    assert all(rep["arms"][a]["paired"]["n"] == 40 for a in measured) and rep["arms"]["search_ai"]["paired"]["clean_rate"] is None


def test_the_refusal_for_a_missing_slot_says_what_to_wire(tmp_path) -> None:
    wave, split = _heldout(tmp_path, slot=None)
    with pytest.raises(report.ReportRefused, match=r"copy\s+plan_wave\(\.\.\.\)\.provenance\(\)"):
        report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT)


# ================================================================== Task 24: the carry-ins from Task 21's review

def mixed_split(n: int = 40) -> list:
    """A dev-shaped split in which every fifth INPUT is already clean, so `in_clean` is True for some files and
    `clean_gain` is no longer equal to `clean_out` (every other fixture here has in_clean=False throughout)."""
    out = []
    for i in range(n):
        c = i % 5 == 0
        out.append(rp.ProbeRow(f"f{i}.step", _sha(i), "abc_fail", {"tier": "full", "clean": c, "counts_for_scoreboard": True},
                               None if c else "panel", "" if c else KINDS[i % 4], None))
    return out


def _gain(arm: str, idx) -> int:
    """Files the arm FIXED (clean out, not clean in), counted over the fixture's own definition, not the report's."""
    return sum(_fixed(i, arm) and i % 5 != 0 for i in idx)


def test_clean_gain_and_paired_clean_in_are_asserted_numerically_on_a_split_with_clean_inputs(tmp_path) -> None:
    split = mixed_split()
    dropped = {split[i].sha256 for i in (5, 6, 7)}  # staged loses one CLEAN input (f5) and two dirty ones
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "staged" and b.sha256 in dropped)))
    rep = report.build_report(load(tmp_path, split))
    allf, shared = range(40), [i for i in range(40) if i not in (5, 6, 7)]
    clean_inputs_all, clean_inputs_shared = 8, 7
    for arm in ("input_only", "staged", "search_det", "search_ai"):
        a = rep["arms"][arm]
        own = [i for i in allf if not (arm == "staged" and i in (5, 6, 7))]
        out_n = sum(i % 5 == 0 or _fixed(i, arm) for i in own)
        in_n = sum(i % 5 == 0 for i in own)
        assert (a["clean_out"], a["clean_in"]) == (out_n, in_n)
        assert a["clean_gain"] == out_n - in_n == _gain(arm, own)
        # the gain is neither clean_out (which is what every dev fixture reduces it to) nor clean_out + clean_in
        if arm != "input_only":
            assert a["clean_gain"] not in (a["clean_out"], a["clean_out"] + a["clean_in"])
        p = a["paired"]
        assert p["n"] == 37 and p["clean_in"] == clean_inputs_shared
        assert p["clean"] == sum(i % 5 == 0 or _fixed(i, arm) for i in shared)
        assert p["clean_gain"] == p["clean"] - p["clean_in"] == _gain(arm, shared)
    assert rep["arms"]["input_only"]["clean_gain"] == 0 and rep["arms"]["input_only"]["paired"]["clean_gain"] == 0
    assert clean_inputs_all - clean_inputs_shared == 1  # the dropped clean input really did change the paired count
    assert "+0" in report.render_text(rep)


def test_primary_artifact_needs_a_slot_the_marker_store_confirmed(tmp_path) -> None:
    wave, split = _heldout(tmp_path)
    unverified = report.build_report(report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT))
    assert unverified["primary_artifact"] is False and unverified["heldout_split"] is True
    text = report.render_text(unverified)
    assert "PRIMARY ARTIFACT" not in text.replace("NOT the primary artifact", "") and "NOT the primary artifact" in text
    verified = report.build_report(report.load_wave(tmp_path, wave, split, n_shards=2, frozen_commit=COMMIT,
                                                     markers=_Store({"slot/held_out/1": "wv1"})))
    assert verified["primary_artifact"] is True
    assert "[PRIMARY ARTIFACT, frozen commit verified, once-only slot verified]" in report.render_text(verified)


def test_a_dev_report_is_never_the_primary_artifact_even_with_a_verified_slot(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, prov={"slot": "slot/checkpoint/1"})
    rep = report.build_report(load(tmp_path, split, markers=_Store({"slot/checkpoint/1": "wv1"})))
    assert rep["slot_verified_against_store"] is True and rep["primary_artifact"] is False
    assert "PRIMARY ARTIFACT" not in report.render_text(rep)


def _same_outcome(b, i, a, **over):
    """search_det and search_ai made EXACTLY the same repairs (a legitimate result: see FALSE_POSITIVE_NOTE)."""
    ok = i % 3 == 0
    return arm_row(b, i, a, out_clean=ok, out_panel_passes=PANEL if ok else 1,
                   reason="clean" if ok else "patience", **over)


def test_the_indistinguishable_warning_never_names_an_arm_that_is_not_measured(tmp_path) -> None:
    wave, split = ev.WAVES["dev_search"], split_rows()
    inc = {b.sha256 for b in split[:12]}  # 30%: search_ai is NOT MEASURED; its 28 surviving files equal search_det's
    build(tmp_path, wave, split, rows_for=lambda b, i, a: _same_outcome(b, i, a) if a != "search_ai" or b.sha256 not in inc
          else arm_row(b, i, a, inconclusive=True))
    rep = report.build_report(load(tmp_path, split, wave))
    assert rep["arms"]["search_ai"]["measured"] is False
    leaked = [w for w in rep["warnings"] if "indistinguishable" in w]
    assert leaked == [], leaked
    assert any(w.startswith("search_ai: NOT MEASURED") for w in rep["warnings"])
    # the sibling: the same two arms, BOTH measured and identical, ARE flagged, so the filter is not a blanket silence
    sib = tmp_path / "sib"
    build(sib, wave, split, rows_for=_same_outcome)
    flagged = report.build_report(load(sib, split, wave))["warnings"]
    assert any("search_det and search_ai are indistinguishable on all 40 files" in w for w in flagged)


def test_per_kind_cells_on_a_measured_arm_are_withheld_when_that_kind_is_over_ten_percent_inconclusive(tmp_path) -> None:
    split = split_rows()
    inc = {split[0].sha256, split[4].sha256}  # two of plc_intersection's ten files: 20% of the KIND, 5% of the arm
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=(a == "staged" and b.sha256 in inc)))
    rep = report.build_report(load(tmp_path, split))
    st = rep["arms"]["staged"]
    assert st["measured"] is True and st["clean_rate"] is not None
    k = st["by_defect_kind"]["plc_intersection"]
    assert k["inconclusive"] == 2 and k["measured"] is False
    assert k["clean"] is None and k["clean_rate"] is None  # the count recovers the rate: it is withheld too
    assert st["by_defect_kind"]["plc_open_shell"]["clean"] == 0 and st["by_defect_kind"]["plc_open_shell"]["measured"] is True
    row = next(r for r in _kind_section(report.render_text(rep), "staged") if r[0] == "plc_intersection")
    assert row[-2:] == ["withheld", "withheld"]
    # the sibling kind, and the same kind on an arm that dropped nothing, still print their figures
    other = next(r for r in _kind_section(report.render_text(rep), "staged") if r[0] == "plc_open_shell")
    assert other[-2] != "withheld"
    full = next(r for r in _kind_section(report.render_text(rep), "search_det") if r[0] == "plc_intersection")
    assert full[-2] != "withheld" and full[-1] != "withheld"


def test_a_search_row_whose_start_is_clean_is_measured_even_with_inconclusive_candidates_and_no_verdict(tmp_path) -> None:
    """verdict_candidates=0, inconclusive_candidates>0, out_clean=True: the `not out_clean` clause is what keeps this
    file MEASURED (the start was clean and stays clean), so it is the clause under test, not the other two."""
    split = mixed_split()
    clean_start = {split[i].sha256 for i in range(0, 40, 5)}

    def rows(b, i, a):
        if a == "search_det" and b.sha256 in clean_start:
            return arm_row(b, i, a, reason="patience", verdict_candidates=0, inconclusive_candidates=3,
                           infrastructure_failed=False)
        return arm_row(b, i, a)

    build(tmp_path, WAVE, split, rows_for=rows)
    rep = report.build_report(load(tmp_path, split))
    det = rep["arms"]["search_det"]
    assert det["inconclusive"] == 0 and det["conclusive"] == 40  # measured: counted, not dropped
    # the same fields on a file whose start is NOT clean say nothing was measured, and a row calling it conclusive is refused
    dirty = tmp_path / "dirty"
    build(dirty, WAVE, split, rows_for=lambda b, i, a: arm_row(
        b, i, a, reason="patience", verdict_candidates=0, inconclusive_candidates=3, infrastructure_failed=False)
        if a == "search_det" and b.sha256 not in clean_start else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused, match="never got a verdict"):
        load(dirty, split)


def test_four_measured_arms_that_each_drop_different_files_warn_that_the_paired_set_shrank(tmp_path) -> None:
    split = split_rows()
    drops = {"input_only": (0, 1, 2), "staged": (3, 4, 5), "search_det": (6, 7, 8), "search_ai": (9, 10, 11)}
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=i in drops[a]))
    rep = report.build_report(load(tmp_path, split))
    assert all(rep["arms"][a]["measured"] for a in drops)  # each arm is under the per-arm limit (7.5%)
    assert rep["paired_set"]["n_files"] == 28 < report.MIN_PAIRED_FRACTION * 40
    assert any("PAIRED columns cover only 28 of 40" in w for w in rep["warnings"])
    assert rep["status"] == "UNRELIABLE" and rep["numbers_valid"] is False
    # the sibling: the same arms dropping the SAME three files leave 37 of 40 and no such warning
    sib = tmp_path / "sib"
    build(sib, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a, inconclusive=i in (0, 1, 2)))
    assert not any("PAIRED columns cover only" in w for w in report.build_report(load(sib, split))["warnings"])


# ---- the row contract is the search driver's, not a copy

def test_the_row_carries_exactly_the_fields_the_search_driver_defines() -> None:
    import dataclasses

    from meshpipeline.cad.repair_v4 import measurement, search

    fields = {"reason", "verdict_candidates", "inconclusive_candidates", "infrastructure_failed"}
    assert fields <= {f.name for f in dataclasses.fields(search.SearchResult)}
    assert fields <= {f.name for f in dataclasses.fields(report.ArmRow)}
    for name in fields:  # the same type on both sides, so a field retyped on one side fails here
        assert str(report.ArmRow.__dataclass_fields__[name].type).replace(" | None", "") == \
            str(search.SearchResult.__dataclass_fields__[name].type).replace(" | None", ""), name
    assert report.SEARCH_ARMS == tuple(search.ARMS)  # the second hard-coded list, pinned to the driver's own
    src = __import__("inspect").getsource(report.ArmRow.derived_inconclusive.fget)
    assert "file_unmeasured" in src and "no_verdict" not in src  # delegated, not restated
    assert measurement.NO_VERDICT == "no_verdict"


def test_a_real_search_result_and_the_report_row_agree_on_whether_a_file_was_measured() -> None:
    from meshpipeline.cad.repair_v4 import search

    for reason in ("clean", "patience", "no_verdict"):
        for inc in (0, 2):
            for ver in (0, 5):
                for failed in (False, True):
                    for clean in (False, True):
                        res = search.SearchResult(best=None, best_verdict=None, clean=clean, trace=None, reason=reason,
                                                  inconclusive_candidates=inc, verdict_candidates=ver,
                                                  infrastructure_failed=failed)
                        row = report.ArmRow("f", "0" * 64, "abc_fail", "search_det", False, 0.0, 1.0, out_clean=clean,
                                            reason=reason, inconclusive_candidates=inc, verdict_candidates=ver,
                                            infrastructure_failed=failed)
                        assert res.unmeasured == row.derived_inconclusive, (reason, inc, ver, failed, clean)


# ---- Task 39: the declined stratum is a third category: not a failure, not unmeasured, in no rate ----------------------

N_MAIN, N_DECL = 40, 8


def _stratum_split(declined_status="declined_stratum", n_decl=N_DECL):
    main = split_rows(N_MAIN)
    decl = [rp.ProbeRow(f"d{i}.step", _sha(1000 + i), "inject", {"tier": "full", "clean": False,
                                                                 "counts_for_scoreboard": True},
                        "panel", "", None, "face_delete_large", declined_status) for i in range(n_decl)]
    return main + decl


def _stratum_rows(base, i, arm, *, how="poison"):
    """Declined files are built to CHANGE every figure if they leak into it: input unclean at 2 panel passes, output
    at 0 (a fewer-panel-passes 'regression' if counted), and one file the search_det arm repairs (a 'gain' if counted)."""
    if not base.name.startswith("d"):
        return arm_row(base, i, arm)
    if how == "inconclusive":
        return arm_row(base, i, arm, inconclusive=True)
    repaired = arm == "search_det" and base.name == "d0.step"
    row = arm_row(base, i, arm, in_panel_passes=2, out_panel_passes=PANEL if repaired else 0,
                  out_clean=repaired, out_valid=repaired, out_faithful_ok=repaired,
                  model=0.2 if arm == "search_ai" else 0.0)
    if arm == "input_only":
        row.update(out_panel_passes=2, out_clean=False, out_valid=True, out_faithful_ok=True)
    if arm in report.SEARCH_ARMS:
        row.update(reason="clean" if repaired else "patience")
    return row


def _stratum_report(tmp_path, **kw):
    split = _stratum_split()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: _stratum_rows(b, i, a, **kw))
    return report.build_report(load(tmp_path, split))


def test_the_declined_stratum_is_in_no_rate_gain_or_regression(tmp_path) -> None:
    rep = _stratum_report(tmp_path)
    for arm in ("input_only", "staged", "search_det", "search_ai"):
        b = rep["arms"][arm]
        assert b["files"] == N_MAIN and b["conclusive"] == N_MAIN, arm
        assert b["clean_out"] == expected_clean(arm, range(N_MAIN)), arm     # the independent count over the 40 only
        assert b["clean_rate"] == pytest.approx(expected_clean(arm, range(N_MAIN)) / N_MAIN), arm
        assert b["regressions"] == {"clean_to_unclean": 0, "fewer_panel_passes": 0, "either": 0}, arm
        assert b["layers"]["valid"] == N_MAIN and b["paired"]["n"] == N_MAIN, arm
        assert "face_delete_large" not in b["by_defect_kind"] and "" not in b["by_defect_kind"], arm
    assert rep["status"] == "OK" and rep["files"] == N_MAIN + N_DECL and rep["files_measured_strata"] == N_MAIN
    assert rep["marginals"]["ai_marginal (search_ai - search_det)"]["n_paired"] == N_MAIN


def test_the_declined_stratum_is_reported_on_its_own_line_and_is_not_a_failure(tmp_path) -> None:
    rep = _stratum_report(tmp_path)
    ds = rep["declined_stratum"]
    assert ds["expected_outcome"] == "correctly_declined" and ds["files"] == N_DECL
    assert ds["arms"]["staged"] == {"files": N_DECL, "inconclusive": 0, "declined": N_DECL, "unexpectedly_clean": 0}
    # the one file search_det repaired is SHOWN as unexpected, not credited as gain and not hidden
    assert ds["arms"]["search_det"] == {"files": N_DECL, "inconclusive": 0, "declined": N_DECL - 1,
                                        "unexpectedly_clean": 1}
    text = report.render_text(rep)
    assert "Declined stratum (8 files; expected outcome: correctly_declined)" in text
    assert "NOT a failure" in text


def test_inconclusive_declined_files_do_not_push_an_arm_over_the_inconclusive_limit(tmp_path) -> None:
    # 8 of 48 = 16.7% > 10%: folded in, every arm would read NOT MEASURED. Kept out, all 40 real files are measured.
    rep = _stratum_report(tmp_path, how="inconclusive")
    assert all(b["measured"] and b["inconclusive"] == 0 for b in rep["arms"].values())
    assert rep["declined_stratum"]["arms"]["staged"]["inconclusive"] == N_DECL
    assert rep["status"] == "OK"


def test_declined_stratum_spend_is_still_in_the_bill(tmp_path) -> None:
    rep = _stratum_report(tmp_path)
    assert rep["arms"]["search_ai"]["model_usd"]["total"] == pytest.approx(0.4 * N_MAIN + 0.2 * N_DECL)


def test_a_declined_kind_whose_status_was_dropped_is_refused_not_counted_as_a_failure(tmp_path) -> None:
    split = _stratum_split(declined_status=None)
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: _stratum_rows(b, i, a))
    with pytest.raises(report.ReportRefused, match="declined stratum.*would be counted as a failing file"):
        load(tmp_path, split)


def test_a_split_of_only_declined_files_has_nothing_to_report_a_rate_on(tmp_path) -> None:
    split = [r for r in _stratum_split() if r.name.startswith("d")]
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: _stratum_rows(b, i, a))
    with pytest.raises(report.ReportRefused, match="every file in the split is declined-stratum"):
        report.build_report(load(tmp_path, split))


# ------------------------------------------------------------------ the excluded stratum (task 55)

def _wall_excluded(base, wall: str = "file_timeout: file wall clock 1800s exceeded") -> dict:
    return {"name": base.name, "sha256": base.sha256, "source": base.source, "reason": wall}


def test_the_excluded_stratum_appears_in_the_report_named_with_the_walls_that_made_it(tmp_path) -> None:
    from meshpipeline.cad.repair_v4.limits import CHECKER_CALL_TIMEOUT_S

    split = split_rows()
    xs = [_wall_excluded(split[5]),
          _wall_excluded(split[9], "checker_unavailable: check_valid: timeout: check_valid exceeded 900.0s")]
    build(tmp_path, WAVE, split, excluded=xs)
    rep = report.build_report(load(tmp_path, split))
    block = rep["excluded_stratum"]

    assert block["files"] == 2
    assert sorted(n["name"] for n in block["names"]) == sorted(x["name"] for x in xs)
    assert block["by_reason"] == {"file_timeout": 1, "checker_unavailable": 1}
    assert block["fraction_of_split"] == pytest.approx(2 / 40)
    # the shards of this wave recorded today's constants, because that is what they ran
    assert block["walls"]["file_timeout_s"] == ev.DEFAULT_FILE_TIMEOUT_S
    assert block["walls"]["check_valid_timeout_s"] == CHECKER_CALL_TIMEOUT_S
    assert not block["walls"]["disagreed"] and not block["walls"]["unrecorded"]
    # collect's pool limit is about the re-probe pool, not a split: it is not quoted here
    assert "limit" not in block and "over_limit" not in block
    text = report.render_text(rep)
    assert "## Excluded stratum (2 files, 5.0% of the split" in text
    assert "file_timeout 1" in text and "checker_unavailable 1" in text


def test_a_wall_excluded_file_is_neither_a_failure_nor_a_regression(tmp_path) -> None:
    """The point of the stratum: the file is reported, and it moves no rate. Same wave twice - once with two
    files killed by a wall, once without - and every count the comparison rests on is the same over the
    files that WERE judged."""
    split = split_rows()
    victims = [split[5], split[9]]
    build(tmp_path / "with", WAVE, split, excluded=[_wall_excluded(b) for b in victims])
    # the shorter split renumbers the files, so pin each row to its ORIGINAL index: the two waves must
    # repair the same files, else the comparison below proves nothing
    build(tmp_path / "without", WAVE, [b for b in split if b not in victims],
          rows_for=lambda b, _i, a: arm_row(b, int(b.name.removeprefix("f").removesuffix(".step")), a))
    killed = report.build_report(load(tmp_path / "with", split))
    kept = report.build_report(load(tmp_path / "without", [b for b in split if b not in victims]))

    judged = [i for i, b in enumerate(split) if b not in victims]
    for arm, block in killed["arms"].items():
        other = kept["arms"][arm]
        # not a failure: the clean counts and rates are those of the files that were judged
        assert block["clean_out"] == other["clean_out"] == expected_clean(arm, judged)
        assert block["clean_rate"] == pytest.approx(other["clean_rate"])
        assert block["clean_gain"] == other["clean_gain"]
        # not a regression either: a file with no verdict cannot have lost one
        assert block["regressions"] == other["regressions"]
        assert block["regressions"]["either"] == 0
        assert block["conclusive"] == other["conclusive"] == len(judged)
        assert block["inconclusive"] == 2  # counted as unmeasured, which is what it is
    assert killed["excluded_stratum"]["files"] == 2 and kept["excluded_stratum"]["files"] == 0


# ------------------------------------------------------------------ repeated member timeouts, counted and watched

def _timed_out(cause: str = "timeout_twice") -> dict:
    return {"panel_failing": [{"name": "gmsh_alt", "status": "timeout", "error_kind": "timeout", "cause": cause}]}


def _timeout_rows(timeouts_by_arm: dict[str, set[int]]):
    """`rows_for` putting a repeated-timeout member on the files listed for each arm (unfixed files only, so the row's
    own clean/panel fields stay consistent with a non-pass member)."""
    def rows_for(base, i, arm):
        row = arm_row(base, i, arm)
        if i in timeouts_by_arm.get(arm, set()) and not row["out_clean"]:
            row["triage"] = _timed_out("after_timeout_fail" if i % 2 else "timeout_twice")
        return row
    return rows_for


UNFIXED = [i for i in range(40) if not any(_fixed(i, a) for a in ("staged", "search_det", "search_ai"))]


def test_repeated_member_timeouts_are_counted_per_arm_and_for_the_wave(tmp_path) -> None:
    split = split_rows()
    two = set(UNFIXED[:2])
    build(tmp_path, WAVE, split, rows_for=_timeout_rows(dict.fromkeys(WAVE.arms, two)))
    rep = report.build_report(load(tmp_path, split))
    t = rep["timeouts"]
    assert all(t["arms"][a]["files"] == 2 and t["arms"][a]["members"] == 2 for a in WAVE.arms)
    assert t["wave"]["members"] == 2 * len(WAVE.arms)
    assert set(t["arms"]["staged"]["by_cause"]) <= {"timeout_twice", "after_timeout_fail"}
    assert rep["status"] == "OK" and not [w for w in rep["warnings"] if "timeout" in w]  # level with input_only
    assert "repeated member timeouts" in report.render_text(rep)


def test_a_jump_against_input_only_of_the_same_wave_warns(tmp_path) -> None:
    split = split_rows()
    rows_for = _timeout_rows({"input_only": set(UNFIXED[:1]), "staged": set(UNFIXED[:7])})
    build(tmp_path, WAVE, split, rows_for=rows_for)
    rep = report.build_report(load(tmp_path, split))
    jumps = [w for w in rep["warnings"] if "repeated member timeouts" in w]
    assert len(jumps) == 1 and jumps[0].startswith("staged") and "input_only" in jumps[0]
    assert rep["status"] == "UNRELIABLE"


def test_a_small_difference_is_not_a_jump(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=_timeout_rows({"input_only": set(UNFIXED[:1]), "staged": set(UNFIXED[:2])}))
    assert not [w for w in report.build_report(load(tmp_path, split))["warnings"] if "timeout" in w]


def test_without_input_only_the_previous_snapshot_is_the_reference(tmp_path) -> None:
    wave = ev.WAVES["dev_search"]  # search_det, search_ai: no input_only in the wave
    split = split_rows()
    build(tmp_path, wave, split, rows_for=_timeout_rows({"search_det": set(UNFIXED[:6])}))
    result = load(tmp_path, split, wave)
    alone = report.build_report(result)
    assert not [w for w in alone["warnings"] if "timeout" in w]  # no reference, no claim
    previous = {"timeouts": {"arms": {"search_det": {"files": 0, "conclusive": 40}}}}
    jumped = report.build_report(result, previous=previous)
    assert [w for w in jumped["warnings"] if w.startswith("search_det") and "previous snapshot" in w]


# ------------------------------------------------------------------ A7: one file is one file; the shards' own walls

def test_a_file_excluded_on_every_arm_is_one_file_not_four(tmp_path) -> None:
    """An arm wave writes one EXCLUDED entry per (file, arm). The stratum counts FILES."""
    split = split_rows()
    xs = [{**_wall_excluded(split[5]), "arm": a} for a in WAVE.arms]
    build(tmp_path, WAVE, split, excluded=xs)
    block = report.build_report(load(tmp_path, split))["excluded_stratum"]
    assert block["files"] == 1 and block["entries"] == len(WAVE.arms)
    assert block["fraction_of_split"] == pytest.approx(1 / 40)
    assert [n["sha256"] for n in block["names"]] == [split[5].sha256]
    assert block["names"][0]["arms"] == sorted(WAVE.arms)
    assert block["by_reason"] == {"file_timeout": 1}
    assert "## Excluded stratum (1 file" in report.render_text(report.build_report(load(tmp_path, split)))


def test_the_arm_report_quotes_the_walls_its_shards_ran_with(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, excluded=[_wall_excluded(split[5])],
          prov={"file_timeout_s": 900.0, "check_valid_timeout_s": 300.0})
    block = report.build_report(load(tmp_path, split))["excluded_stratum"]
    assert block["walls"]["file_timeout_s"] == 900.0 and block["walls"]["check_valid_timeout_s"] == 300.0
    assert block["walls"]["file_timeout_s"] != ev.DEFAULT_FILE_TIMEOUT_S


def test_a_shard_that_recorded_no_walls_says_unrecorded_never_a_constant(tmp_path) -> None:
    """An older shard has no wall keys. Saying `unrecorded` is honest; filling in today's constant would claim a
    wall nobody ran."""
    split = split_rows()
    build(tmp_path, WAVE, split, excluded=[_wall_excluded(split[5])],
          prov={"file_timeout_s": None, "check_valid_timeout_s": None})
    block = report.build_report(load(tmp_path, split))["excluded_stratum"]
    assert block["walls"]["file_timeout_s"] is None
    assert sorted(block["walls"]["unrecorded"]) == ["check_valid_timeout_s", "file_timeout_s"]
    assert "file_timeout_s unrecorded" in report.render_text(report.build_report(load(tmp_path, split)))


# ------------------------------------------------------------------ A10: clean = valid + panel, and the gaming flag

def test_the_report_derives_clean_from_valid_and_panel_only(tmp_path) -> None:
    """Owner decision 2026-11-24. A row that is valid, meshes everywhere and moved the part IS clean here."""
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: _moved(b, i, a))
    rep = report.build_report(load(tmp_path, split))
    assert rep["arms"]["staged"]["clean_out"] == 40
    assert rep["status"] in ("OK", "UNRELIABLE")   # not refused: the stored clean agrees with the new rule


def test_a_row_claiming_clean_without_the_whole_panel_is_still_refused(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split,
          rows_for=lambda b, i, a: {**arm_row(b, i, a), "out_valid": True, "out_panel_passes": 1,
                                    "out_clean": True} if a != "input_only" else arm_row(b, i, a))
    with pytest.raises(report.ReportRefused):
        load(tmp_path, split)


#: arms that actually produce an output; input_only ships its input unchanged, so it can never be a "mover" and
#: the report refuses a row where its output differs from its input
MOVER_ARMS = tuple(a for a in WAVE.arms if a != "input_only")


def _moved(base, i, arm, **over):
    """A clean output that moved the part, for the repairing arms; input_only keeps the fixture's pass-through."""
    if arm == "input_only":
        return arm_row(base, i, arm)
    row = {**arm_row(base, i, arm), "out_valid": True, "out_faithful_ok": False,
           "out_panel_passes": PANEL, "out_clean": True}
    row.update(over)
    return row


def test_a_clean_output_that_tripled_the_volume_is_flagged_and_still_clean(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split,
          rows_for=lambda b, i, a: _moved(b, i, a, out_volume_ratio=3.0 if i == 0 else 1.0,
                                          out_bbox_ratio=1.0))
    rep = report.build_report(load(tmp_path, split))
    flags = rep["gaming_flags"]
    assert flags["limits"] == {"volume_change": 0.25, "bbox_shrink": 0.10}
    for arm in MOVER_ARMS:
        block = flags["arms"][arm]
        assert block["files"] == 1, arm
        (f,) = block["names"]
        assert f["sha256"] == split[0].sha256 and f["volume_ratio"] == 3.0 and "volume" in f["why"]
    assert flags["arms"]["input_only"]["files"] == 0   # it never changes its input, so it can never move it
    assert rep["arms"]["staged"]["clean_out"] == 40          # still counted clean
    text = report.render_text(rep)
    assert f"Gaming flags ({len(MOVER_ARMS)} flagged clean row(s) over 1 file(s)" in text
    assert "volume x3" in text


def test_a_clean_output_whose_bbox_shrank_is_flagged(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split,
          rows_for=lambda b, i, a: _moved(b, i, a, out_bbox_ratio=0.5 if i == 1 else 0.95))
    flags = report.build_report(load(tmp_path, split))["gaming_flags"]
    (f,) = flags["arms"]["staged"]["names"]
    assert f["sha256"] == split[1].sha256 and f["bbox_ratio"] == 0.5 and "bbox" in f["why"]
    # 0.95 is a 5% shrink: inside the 10% limit, not flagged
    assert flags["arms"]["staged"]["files"] == 1


@pytest.mark.parametrize("ratio,flagged", [(1.25, False), (1.26, True), (0.7, True), (0.75, False), (0.8, False)])
def test_the_volume_limit_is_a_change_in_either_direction(tmp_path, ratio, flagged) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: _moved(b, i, a, out_volume_ratio=ratio if i == 0 else 1.0))
    flags = report.build_report(load(tmp_path, split))["gaming_flags"]
    assert flags["arms"]["staged"]["files"] == (1 if flagged else 0)


def test_an_unclean_output_is_never_flagged_and_a_missing_measurement_is_counted(tmp_path) -> None:
    split = split_rows()
    def rows(b, i, a):
        if a == "input_only":
            return arm_row(b, i, a)
        if i == 0:
            return {**arm_row(b, i, a), "out_clean": False, "out_valid": False, "out_volume_ratio": 9.0}
        return _moved(b, i, a, out_volume_ratio=None)

    build(tmp_path, WAVE, split, rows_for=rows)
    flags = report.build_report(load(tmp_path, split))["gaming_flags"]
    assert flags["arms"]["staged"]["files"] == 0            # the mover was not clean: not the flag's business
    assert flags["arms"]["staged"]["unmeasured"] == 39      # clean rows with no fidelity numbers, counted not hidden


def test_an_unfaithful_row_that_claims_clean_is_accepted_now(tmp_path) -> None:
    """The case that used to be a contradiction: valid + whole panel + faithful_ok False IS clean since the owner's
    2026-11-24 decision, so the row is accepted and the file is flagged for review instead."""
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: _moved(b, i, a, out_volume_ratio=1.0, out_bbox_ratio=1.0))
    rep = report.build_report(load(tmp_path, split))
    assert rep["arms"]["staged"]["clean_out"] == 40
    assert rep["gaming_flags"]["arms"]["staged"]["files"] == 0   # unfaithful, but inside both review limits


# ------------------------------------------------------------------ C12: gaming flags on VALID-NOT-CLEAN outputs

def test_a_valid_unclean_output_that_destroyed_the_part_is_flagged_with_its_numbers_in_its_own_group(tmp_path) -> None:
    """Lane C12 on v4: search_ai DELIVERED a VALID state with 0.07% of the part left (0051_00510567). It was not clean,
    so the clean-only gaming rule never saw it. Now it is named with its numbers, apart from clean-rate gaming."""
    split = split_rows()

    def rows(b, i, a):
        if a == "input_only" or i != 0:
            return arm_row(b, i, a)
        return {**arm_row(b, i, a), "out_valid": True, "out_faithful_ok": False, "out_panel_passes": 1,
                "out_clean": False, "out_volume_ratio": 0.00069, "out_area_ratio": 0.0036, "out_bbox_ratio": 0.281,
                "out_max_dev_mm": 40.0, "out_bodies_before": 8, "out_bodies_after": 4}

    build(tmp_path, WAVE, split, rows_for=rows)
    rep = report.build_report(load(tmp_path, split))
    assert all(rep["gaming_flags"]["arms"][a]["files"] == 0 for a in WAVE.arms)   # NOT clean-rate gaming
    gv = rep["gaming_flags_valid_unclean"]
    assert "input_only" not in gv["arms"]                                          # its output is its input
    for arm in MOVER_ARMS:
        (f,) = gv["arms"][arm]["names"]
        assert f["sha256"] == split[0].sha256 and f["volume_ratio"] == 0.00069 and f["bodies"] == [8, 4]
    text = report.render_text(rep)
    assert "Gaming flags on VALID-NOT-CLEAN delivered outputs" in text and "0051_00510567" in text
    assert "volume x0.00069, area x0.0036, bbox x0.281, max dev 40 mm, bodies 8->4" in text
    base = tmp_path / "base"
    base.mkdir()
    build(base, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a) if a == "input_only" or i != 0 else {
        **arm_row(b, i, a), "out_valid": True, "out_panel_passes": 1, "out_clean": False})
    baseline = report.build_report(load(base, split))
    for arm in MOVER_ARMS:   # the flag moved NO count: same clean rate as the same row without the numbers
        assert rep["arms"][arm]["clean_out"] == baseline["arms"][arm]["clean_out"]


def test_a_valid_unclean_output_inside_the_limits_is_not_flagged(tmp_path) -> None:
    split = split_rows()
    build(tmp_path, WAVE, split, rows_for=lambda b, i, a: arm_row(b, i, a) if a == "input_only" else {
        **arm_row(b, i, a), "out_valid": True, "out_panel_passes": 1, "out_clean": False,
        "out_volume_ratio": 0.99, "out_bbox_ratio": 1.0})
    gv = report.build_report(load(tmp_path, split))["gaming_flags_valid_unclean"]
    assert all(b["files"] == 0 for b in gv["arms"].values())
    assert "none in this wave" in report.render_text(report.build_report(load(tmp_path, split)))
