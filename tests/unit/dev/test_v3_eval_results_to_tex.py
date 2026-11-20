# Responsibility: Hold the CRS v3 results renderers to their one promise: a number nobody measured
#                 renders as a visible [PENDING], never as 0, and a measured 0 renders as 0. Also
#                 pin that there is NO pass/fail anywhere (no verdict, no ratio), the headline
#                 "files fixed", the clean-control rule (still meshes, no new defect), the
#                 tolerant-mesher column kept apart, the schema/example agreement, and the
#                 collect.py adapter.
# Boundaries: hermetic. No CAD, no cloud, no LaTeX run; collect.py output is written here.
from __future__ import annotations

import copy
import importlib.util
import json
import math
import re
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
    """A complete result: deterministic 3 newly meshed, v3 12, no breaks, clean 100/100 good."""
    r = rt.blank_results()
    for arm_id in rt.ARMS:
        r["arms"][arm_id].update(
            commit="a" * 40, image_digest="sha256:" + "b" * 64, sample_sha256="c" * 64,
            n_files=120, newly_meshed=3, newly_broken=0, meshes_before=0, meshes_after=3,
            endings={"repaired": 3, "unmeshable": 117}, ops_kept={"sew_faces": 2},
            ops_rolled_back={"sew_faces": 5}, model_calls=0, cost_usd=1.5, wall_s=3600.0,
            cost_per_fixed_part=0.5)
    r["arms"]["t29_v3"].update(newly_meshed=12, meshes_after=12)
    r["arms"]["clean_control"].update(n_files=100, still_meshes_no_new_defects=100, changed=0,
                                      newly_broken=0)
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
                                           "CostPerFixed", "FilesFixed", "StillGood",
                                           "CleanChanged", "TolerantMesher"))]
    assert numeric and all(m[k] == PENDING for k in numeric)
    tex = rt.render_tex(results)
    assert "{0}" not in tex  # no macro body is a bare zero


def test_null_tally_pending_and_empty_tally_none():
    assert rt.fmt_tally(None) == PENDING
    assert rt.fmt_tally({}) == "none"
    assert rt.fmt_tally({"fix_small_edges": 0}) == r"\texttt{fix\_small\_edges}~0"


# --------------------------------------------------------------------------- no pass/fail

#: Words and macros of the withdrawn T29 4x rule. None may appear in anything rendered.
VERDICT_WORDS = ("Verdict", "verdict", "PASS", "FAIL", "4x", "ratio", "Ratio", "ByteUnchanged",
                 "unchanged")


def test_there_is_no_verdict_or_ratio_anywhere():
    assert not hasattr(rt, "compute_verdict") and not hasattr(rt, "RATIO")
    evalspec = _load("evalspec_no_verdict", V3 / "evalspec.py")
    assert not hasattr(evalspec, "t29_verdict") and not hasattr(evalspec, "RATIO")
    for r in (_filled(), rt.load(rt.PENDING_JSON),
              _filled({("t29_deterministic", "newly_meshed"): 0})):
        assert "verdict" not in r
        m = rt.macros(r)
        assert not [k for k in m if any(w in k for w in ("Verdict", "Ratio", "ByteUnchanged"))]
        tex = rt.render_tex(r)
        body = tex.split("\n", 5)[-1]  # past the generated-file header comments
        md = pr.render(r).split("## What this PR fixes")[0]
        for text in (body, md):
            for word in VERDICT_WORDS:
                assert not re.search(rf"\b{re.escape(word)}\b", text), word


def test_validate_refuses_a_verdict_block_and_schema_version_1():
    r = _filled()
    r["verdict"] = {"status": "PASS"}
    with pytest.raises(ValueError, match="verdict"):
        rt.validate(r)
    r = _filled()
    r["schema_version"] = 1
    with pytest.raises(ValueError, match="schema_version"):
        rt.validate(r)
    rt.validate(_filled())


def test_schema_has_no_verdict_and_is_version_2():
    schema = json.loads((V3 / "results.schema.json").read_text())
    assert schema["properties"]["schema_version"] == {"const": 2}
    assert "verdict" not in schema["properties"]
    assert json.loads(rt.PENDING_JSON.read_text())["schema_version"] == 2
    assert "verdict" not in json.loads(rt.PENDING_JSON.read_text())


def test_files_fixed_is_the_headline_per_arm_and_equals_newly_meshed():
    m = rt.macros(_filled())
    assert m["vThreeFilesFixed"] == "12" == m["vThreeNewlyMeshed"]
    assert m["vThreeDetFilesFixed"] == "3"
    for infix, _ in rt.ARMS.values():
        assert f"vThree{infix}FilesFixed" in m
    assert "Files fixed" in rt.render_tex(_filled())
    assert "files fixed" in pr.render(_filled())


def test_files_fixed_never_includes_tolerant_mesher_only_parts():
    base = rt.macros(_filled())
    m = rt.macros(_filled({("t29_deterministic", "delivered_via_tolerant_mesher"): 40,
                           ("t29_v3", "delivered_via_tolerant_mesher"): 41}))
    assert m["vThreeFilesFixed"] == base["vThreeFilesFixed"] == "12"
    assert m["vThreeDetFilesFixed"] == base["vThreeDetFilesFixed"] == "3"
    assert m["vThreeTolerantMesher"] == "41" and m["vThreeDetTolerantMesher"] == "40"


def test_the_tolerant_column_carries_its_exact_label_everywhere():
    label = "tolerant-mesher meshable (not counted as CAD repair)"
    assert rt.TOLERANT_LABEL == label
    r = _filled({("t29_v3", "delivered_via_tolerant_mesher"): 41})
    assert rt.macros(r)["vThreeTolerantMesherLabel"] == rt.tex_escape(label)
    assert rt.tex_escape(label) in rt.results_table(r, rt.macros(r))
    assert f"| {label} |" in pr.render(r)
    schema = json.loads((V3 / "results.schema.json").read_text())
    assert label in schema["$defs"]["arm"]["properties"]["delivered_via_tolerant_mesher"][
        "description"]


def test_clean_control_reports_still_good_and_changed_as_information():
    r = _filled({("clean_control", "still_meshes_no_new_defects"): 97,
                 ("clean_control", "changed"): 5})
    m = rt.macros(r)
    assert m["vThreeCleanStillGood"] == "97" and m["vThreeCleanChanged"] == "5"
    assert "vThreeCleanByteUnchanged" not in m
    table = rt.results_table(r, m)
    assert "97" in table and "still mesh" in table and "no new defect" in table


def test_rule_of_three_bound():
    assert rt.zero_break_bound(_filled()) == pytest.approx(3 / 220)
    assert rt.fmt(rt.zero_break_bound(_filled()), "pct") == r"1.4\%"
    assert rt.zero_break_bound(rt.blank_results()) is None


@pytest.mark.parametrize("arm", ["t29_v3", "clean_control"])
def test_rule_of_three_bound_refused_unless_breaks_measured_zero(arm):
    assert rt.zero_break_bound(_filled({(arm, "newly_broken"): 5})) is None
    assert rt.zero_break_bound(_filled({(arm, "newly_broken"): None})) is None


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
    for name, nm in (("t29_deterministic", 2), ("v3_full", 9), ("t19_tournament_off", 5)):
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
    for name, kept, good in (("t29_deterministic", [], 100), ("v3_full", ["x.step"], 98)):
        arms[f"{name}/clean"] = {"files": 100, "clean": {
            "still_meshes_no_new_defects": good, "changed": len(kept),
            "files_with_kept_ops": kept, "newly_broken": 0}}
    (out / "verdict.json").write_text(json.dumps({
        "checked": {"commit": "d" * 40, "image": "sha256:" + "e" * 64,
                    "samples": {"failing": "f" * 64, "clean": "0" * 64}},
        "comparisons": {}, "arms": arms}))
    return out


def test_collect_arm_names_are_evalspecs():
    evalspec = _load("evalspec_under_test", V3 / "evalspec.py")
    assert set(rt.COLLECT_ARMS) == set(evalspec.ARMS)


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
    assert a["clean_control"]["still_meshes_no_new_defects"] == 98  # min over the two arms
    assert a["clean_control"]["changed"] == 1  # information: the most over the two arms
    assert a["t6_rules_only_local"]["newly_meshed"] is None  # not run by collect: stays pending
    assert "verdict" not in r


def test_from_collect_clean_breaks_are_the_worst_arm_as_collect_reports(tmp_path):
    # collect.py's own results.json takes the MAX over clean arms; the adapter must agree, or the
    # two documented paths print different clean-break counts for the same run.
    out = _collect_dir(tmp_path)
    v = json.loads((out / "verdict.json").read_text())
    v["arms"]["t29_deterministic/clean"]["clean"]["newly_broken"] = 1
    v["arms"]["v3_full/clean"]["clean"]["newly_broken"] = 2
    (out / "verdict.json").write_text(json.dumps(v))
    assert rt.from_collect(out)["arms"]["clean_control"]["newly_broken"] == 2


def test_from_collect_takes_the_clean_count_collect_decided(tmp_path):
    # collect.py decides per part (a crashed part is never good); the adapter only moves numbers.
    out = _collect_dir(tmp_path)
    v = json.loads((out / "verdict.json").read_text())
    for key in ("t29_deterministic/clean", "v3_full/clean"):
        v["arms"][key]["clean"].update(still_meshes_no_new_defects=99, changed=1,
                                       files_with_kept_ops=[], crashed_files=["y.step"])
    (out / "verdict.json").write_text(json.dumps(v))
    r = rt.from_collect(out)
    assert r["arms"]["clean_control"]["still_meshes_no_new_defects"] == 99
    assert r["arms"]["clean_control"]["changed"] == 1


# --------------------------------------------------------------------------- PR section

def test_pr_section_pending_and_filled():
    pending = pr.render(rt.load(rt.PENDING_JSON))
    assert "**[PENDING]**" in pending and "| 0 |" not in pending
    filled = pr.render(_filled({(a, "delivered_via_tolerant_mesher"): 40 for a in rt.ARMS}))
    body = filled.split("### Files fixed")[1].split("## What this PR fixes")[0]
    assert "**[PENDING]**" not in body


def test_blank_results_is_independent():
    a, b = rt.blank_results(), rt.blank_results()
    a["arms"]["t29_v3"]["n_files"] = 1
    assert b["arms"]["t29_v3"]["n_files"] is None
    assert copy.deepcopy(a) == a


# THE TOLERANT ROUTE: its own column, never part of files fixed.

def test_the_tolerant_column_renders_and_reads_pending_when_unmeasured():
    r = _filled({("t29_v3", "delivered_via_tolerant_mesher"): 41})
    assert rt.macros(r)["vThreeTolerantMesher"] == "41"
    assert rt.macros(_filled())["vThreeTolerantMesher"] == PENDING
