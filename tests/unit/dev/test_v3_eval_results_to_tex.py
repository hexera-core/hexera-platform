# Responsibility: Hold the CRS v3 results renderers to their one promise: a number nobody measured
#                 renders as a visible [PENDING], never as 0, and a measured 0 renders as 0. Also
#                 pin the T29 verdict rule (including the degenerate deterministic-ceiling-0 case),
#                 the schema/example agreement, and the collect.py adapter.
# Boundaries: hermetic. No CAD, no cloud, no LaTeX run; collect.py output is written here.
from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
V3 = REPO / "devtools" / "validation" / "v3_eval"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rt = _load("results_to_tex_under_test", V3 / "results_to_tex.py")
pr = _load("render_pr_section_under_test", V3 / "render_pr_section.py")

PENDING = r"\textbf{[PENDING]}"


def _filled(over: dict | None = None) -> dict:
    """A complete result: deterministic 3 newly meshed, v3 12, no breaks, clean 100/100."""
    r = rt.blank_results()
    for arm_id in rt.ARMS:
        r["arms"][arm_id].update(
            commit="a" * 40, image_digest="sha256:" + "b" * 64, sample_sha256="c" * 64,
            n_files=120, newly_meshed=3, newly_broken=0, meshes_before=0, meshes_after=3,
            endings={"repaired": 3, "unmeshable": 117}, ops_kept={"sew_faces": 2},
            ops_rolled_back={"sew_faces": 5}, model_calls=0, cost_usd=1.5, wall_s=3600.0,
            cost_per_fixed_part=0.5)
    r["arms"]["t29_v3"].update(newly_meshed=12, meshes_after=12)
    r["arms"]["clean_control"].update(n_files=100, byte_unchanged=100, newly_broken=0)
    for (arm, field), value in (over or {}).items():
        r["arms"][arm][field] = value
    return r


# --------------------------------------------------------------------------- PENDING, never 0

def test_missing_value_renders_pending():
    assert rt.fmt(None) == PENDING
    r = rt.blank_results()
    del r["arms"]["t29_v3"]["newly_meshed"]
    assert rt.macros(r)["vThreeNewlyMeshed"] == PENDING


def test_zero_renders_zero_not_pending():
    assert rt.fmt(0) == "0"
    assert rt.fmt(0.0, "usd") == r"\$0.00"
    r = _filled({("t29_deterministic", "newly_meshed"): 0})
    assert rt.macros(r)["vThreeDetNewlyMeshed"] == "0"


@pytest.mark.parametrize("bad", [float("nan"), math.inf, -math.inf])
def test_nan_and_inf_render_pending(bad):
    assert rt.fmt(bad) == PENDING
    assert rt.fmt(bad, "usd") == PENDING
    assert rt.fmt(bad, md=True) == "**[PENDING]**"


def test_bool_is_not_a_count():
    assert rt.fmt(True) == PENDING


def test_not_applicable_is_its_own_answer():
    assert rt.fmt("n/a", "usd") == "n/a"


def test_pending_example_renders_every_number_pending():
    results = rt.load(rt.PENDING_JSON)
    m = rt.macros(results)
    numeric = [k for k in m if k.endswith(("NewlyMeshed", "NewlyBroken", "NFiles", "CostUsd",
                                           "CostPerFixed", "ByteUnchanged"))]
    assert numeric and all(m[k] == PENDING for k in numeric)
    assert m["vThreeVerdict"] == PENDING
    tex = rt.render_tex(results)
    assert "{0}" not in tex  # no macro body is a bare zero


def test_null_tally_pending_and_empty_tally_none():
    assert rt.fmt_tally(None) == PENDING
    assert rt.fmt_tally({}) == "none"
    assert rt.fmt_tally({"fix_small_edges": 0}) == r"\texttt{fix\_small\_edges}~0"


# --------------------------------------------------------------------------- the verdict

def test_verdict_pass_on_ratio():
    v = rt.compute_verdict(_filled())
    assert v["status"] == "PASS" and v["ratio"] == 4.0 and v["judged_on"] == "ratio"


def test_verdict_fails_below_4x():
    v = rt.compute_verdict(_filled({("t29_v3", "newly_meshed"): 11}))
    assert v["status"] == "FAIL" and not v["ratio_ok"]


def test_verdict_fails_on_any_break_or_clean_change():
    assert rt.compute_verdict(_filled({("t29_v3", "newly_broken"): 1}))["status"] == "FAIL"
    assert rt.compute_verdict(_filled({("t29_deterministic", "newly_broken"): 1}))["status"] == "FAIL"
    assert rt.compute_verdict(_filled({("clean_control", "byte_unchanged"): 99}))["status"] == "FAIL"


def test_verdict_degenerate_ceiling_judged_on_absolute_and_says_so():
    v = rt.compute_verdict(_filled({("t29_deterministic", "newly_meshed"): 0}))
    assert v["ratio"] is None and v["ratio_defined"] is False and v["judged_on"] == "absolute"
    assert v["status"] == "PASS"
    assert "undefined (deterministic ceiling 0)" in v["statement"]
    assert "not claimed" in v["statement"]
    both_zero = rt.compute_verdict(_filled({("t29_deterministic", "newly_meshed"): 0,
                                              ("t29_v3", "newly_meshed"): 0}))
    assert both_zero["status"] == "FAIL"


def test_verdict_pending_when_any_input_missing():
    assert rt.compute_verdict(_filled({("t29_v3", "newly_broken"): None}))["status"] == "PENDING"
    assert rt.compute_verdict(_filled({("clean_control", "byte_unchanged"): float("nan")}))[
        "status"] == "PENDING"


def test_rule_of_three_bound():
    assert rt.zero_break_bound(_filled()) == pytest.approx(3 / 220)
    assert rt.fmt(rt.zero_break_bound(_filled()), "pct") == r"1.4\%"
    assert rt.zero_break_bound(rt.blank_results()) is None


# --------------------------------------------------------------------------- schema and labels

def test_example_matches_schema_field_lists():
    schema = json.loads((V3 / "results.schema.json").read_text())
    arm_def, clean_def = schema["$defs"]["arm"], schema["$defs"]["clean"]
    assert tuple(arm_def["required"]) == rt.ARM_FIELDS
    assert tuple(clean_def["required"]) == rt.CLEAN_FIELDS
    assert set(schema["properties"]["arms"]["required"]) == set(rt.ARMS) | {rt.CLEAN}
    example = json.loads(rt.PENDING_JSON.read_text())
    for arm_id, arm in example["arms"].items():
        allowed = set((clean_def if arm_id == rt.CLEAN else arm_def)["properties"])
        assert set(arm) <= allowed, arm_id


def test_t6_label_is_exact():
    r = rt.blank_results()
    assert r["arms"]["t6_rules_only_local"]["label"] == "rules-only baseline"
    r["arms"]["t6_rules_only_local"]["label"] = "everything on"
    with pytest.raises(ValueError, match="rules-only baseline"):
        rt.validate(r)


def test_validate_refuses_a_missing_field():
    r = rt.blank_results()
    del r["arms"]["t29_v3"]["cost_usd"]
    with pytest.raises(ValueError, match="cost_usd"):
        rt.validate(r)


def test_latex_specials_are_escaped():
    r = _filled()
    r["arms"]["t29_v3"]["ops_kept"] = {"fix_100%_&_more": 1}
    assert r"fix\_100\%\_\&\_more" in rt.macros(r)["vThreeOpsKept"]


# --------------------------------------------------------------------------- collect.py adapter

def _collect_dir(tmp_path: Path) -> Path:
    out = tmp_path / "verdict"
    out.mkdir()
    arms = {}
    for name, nm in (("deterministic", 2), ("v3-full", 9), ("v3-no-tournament", 5)):
        arms[f"{name}/failing"] = {"files": 120, "shards": 4,
                                   "model": {"calls": 7 * nm, "cost_usd": 1.0},
                                   "vm": {"shard_seconds": 1000.0, "container_cost_usd": 0.2},
                                   "total_cost_usd": 1.2, "cost_per_fixed_part_usd": 1.2 / nm}
        bundle = out / f"bundle-{name}-failing"
        bundle.mkdir()
        (bundle / "summary.json").write_text(json.dumps({
            "endings": {"repaired": nm, "unmeshable": 120 - nm},
            "catalogue": {"tally": {"sew_faces": {"kept": nm, "rolled_back": 1, "declined": 0,
                                                  "failed": 0}}},
            "meshing": {"files": 120, "meshed_before": 0, "meshed_after": nm,
                        "newly_meshed": nm, "newly_broken": 0}}))
    for name, kept in (("deterministic", []), ("v3-full", ["x.step"])):
        arms[f"{name}/clean"] = {"files": 100, "clean": {"untouched": not kept,
                                                         "files_with_kept_ops": kept,
                                                         "newly_broken": 0}}
    (out / "verdict.json").write_text(json.dumps({
        "checked": {"commit": "d" * 40, "image": "sha256:" + "e" * 64,
                    "samples": {"failing": "f" * 64, "clean": "0" * 64}},
        "verdicts": {}, "arms": arms}))
    return out


def test_from_collect_maps_arms_and_clean(tmp_path):
    r = rt.from_collect(_collect_dir(tmp_path))
    rt.validate(r)
    a = r["arms"]
    assert a["t29_deterministic"]["newly_meshed"] == 2
    assert a["t29_v3"]["newly_meshed"] == 9 == a["t19_tournament_on"]["newly_meshed"]
    assert a["t19_tournament_on"]["same_run_as"] == "t29_v3"
    assert a["t19_tournament_off"]["newly_meshed"] == 5
    assert a["t29_v3"]["ops_kept"] == {"sew_faces": 9}
    assert a["t29_v3"]["model_calls"] == 63 and a["t29_v3"]["cost_usd"] == 1.2
    assert a["clean_control"]["byte_unchanged"] == 99  # the minimum over the two arms
    assert a["t6_rules_only_local"]["newly_meshed"] is None  # not run by collect: stays pending
    v = rt.compute_verdict(r)
    assert v["status"] == "FAIL" and v["ratio"] == 4.5  # ratio met, clean control was touched


# --------------------------------------------------------------------------- PR section

def test_pr_section_pending_and_filled():
    pending = pr.render(rt.load(rt.PENDING_JSON))
    assert "**[PENDING]**" in pending and "| 0 |" not in pending
    filled = pr.render(_filled())
    body = filled.split("### Verdict (T29)")[1].split("## What this PR fixes")[0]
    assert "**[PENDING]**" not in body
    assert "PASS" in filled


def test_blank_results_is_independent():
    a, b = rt.blank_results(), rt.blank_results()
    a["arms"]["t29_v3"]["n_files"] = 1
    assert b["arms"]["t29_v3"]["n_files"] is None
    assert copy.deepcopy(a) == a
