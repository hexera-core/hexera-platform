# Responsibility: Prove triage.py explains a wave's not-clean outputs from the rows alone: exact aggregates from rows
#                 whose triage blocks are written by hand, and a wave whose rows (and triage blocks) are written by the
#                 REAL arm dispatch and search over a fake world, read back through triage.py's own CLI.
# Boundaries: hermetic; the fake world, pool and model wire are test_v4_arms'.
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.unit.dev.test_v4_arms import (  # noqa: F401  (world is an autouse fixture; shard, wire are fixtures)
    CLEAN_CANDIDATE,
    FakeEngine,
    go,
    ledgered,
    report,
    shard,
    wire,
    world,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"))
import triage  # noqa: E402

PANEL = 4


def row(i: int, arm: str, *, clean: bool = False, inconclusive: bool = False, t: dict | None = None) -> Any:
    sha = f"{i:064x}"
    d: dict = {"name": f"f{i}.step", "sha256": sha, "source": "abc_fail", "arm": arm, "inconclusive": inconclusive,
               "model_usd": 0.0, "wall_s": 1.0, "triage": t}
    if not inconclusive:
        d.update(in_clean=False, in_panel_passes=1, in_valid=True, in_faithful_ok=True, out_valid=True,
                 out_faithful_ok=True, out_panel_passes=PANEL if clean else 2, panel_size=PANEL, out_tier="full",
                 out_clean=clean)
    if arm in report.SEARCH_ARMS:
        d.update(reason="clean" if clean else ("no_verdict" if inconclusive else "patience"),
                 verdict_candidates=0 if inconclusive else 3, inconclusive_candidates=2 if inconclusive else 0,
                 infrastructure_failed=inconclusive)
    return report._row_from_dict(d)


def verdict_part(failing: list[tuple[str, str]], used: float, *, ok: bool = True, reasons=()) -> dict:
    return {"panel_failing": [{"name": n, "status": "fail", "error_kind": k} for n, k in failing],
            "deviation": {"ok": ok, "max_dev_mm": used * 0.1, "bound_mm": 0.1, "used": used, "reasons": list(reasons)},
            "valid": {"ok": True, "hard": []}}


def search_part(reason: str, sites: dict, ops: dict, *, ai: dict | None = None, not_proposed=None) -> dict:
    return {"search": {"stop": {"reason": reason, "ended_by": reason, "spent": {}}, "open_sites": sites,
                       "open_sites_at": "best", "ops": ops, "not_proposed": not_proposed or {}, "ai": ai}}


def op(det=0, ai=0, evaluated=0, declined=None, failed=None, invalid=None, ops_applied=0, **kw) -> dict:
    return {"det": det, "ai": ai, "evaluated": evaluated, "ops_applied": ops_applied, "duplicate": 0,
            "inconclusive": 0, "skipped_budget": 0,
            "declined": declined or {}, "failed": failed or {}, "invalid": invalid or {}, **kw}


def test_triage_aggregates_why_outputs_are_not_clean() -> None:
    # ops_applied is the BUDGET cost (applied ops), not the candidate count: a run_stage chain charges more
    fill = op(det=2, evaluated=1, ops_applied=1, declined={"fill_too_large: span 9 mm": 1})
    rows = [
        row(1, "search_ai", t={**verdict_part([("netgen", "overlapping_facets"), ("gmsh_alt", "plc")], 0.4),
                                **search_part("patience", {"open": 2, "proximity": 1}, {"fill_loop": fill},
                                              ai={"turns": 4, "tool_errors": 1, "candidate_errors": 2,
                                                  "declared_unfixable": 1, "truncated_rounds": 0, "spent_usd": 0.3,
                                                  "stop_reason": "max_turns", "ai_capped": False})}),
        row(2, "search_ai", t={**verdict_part([("netgen", "overlapping_facets")], 1.5, ok=False,
                                              reasons=["deviation_exceeds_bound"]),
                                **search_part("ops", {"open": 1}, {"fill_loop": fill, "sew": op(ai=1, failed={"timeout": 1})},
                                              ai={"turns": 2, "tool_errors": 0, "candidate_errors": 0,
                                                  "declared_unfixable": 0, "truncated_rounds": 1, "spent_usd": 0.2,
                                                  "stop_reason": None, "ai_capped": True},
                                              not_proposed={"no_solid": 3})}),
        row(3, "search_ai", clean=True, t={**verdict_part([], 0.1), **search_part("clean", {}, {})}),
        row(4, "search_ai", inconclusive=True, t=search_part("no_verdict", {}, {})),
        row(5, "search_ai"),  # an older row with no triage block
        row(1, "staged", t={**verdict_part([("netgen", "boundary_recovery")], 0.0),
                            "stages": {"kept": ["normalise", "shells"], "rolled_back": ["condition"]}}),
    ]
    excluded = [{"name": "f6.step", "sha256": f"{6:064x}", "source": "abc_fail", "arm": "search_ai",
                 "reason": "file_timeout: file wall clock 900s exceeded"}]
    t = triage.triage_wave(rows, excluded, kinds={f"{1:064x}": "geometry"}, source="fake")
    ai = t["arms"]["search_ai"]
    assert (ai["files"], ai["clean"], ai["not_clean"], ai["inconclusive_rows"], ai["excluded"]) == (6, 1, 3, 1, 1)
    assert ai["not_clean_without_triage"] == 1 and ai["excluded_reasons"] == {"file_timeout": 1}
    assert ai["unmeasured_stop_reasons"] == {"no_verdict": 1}
    assert ai["stop_reasons"] == {"ops": 1, "patience": 2}  # f5 has no triage block: its row's own `reason` counts
    assert ai["open_sites"] == {"files_with_kind": {"open": 2, "proximity": 1}, "total_sites": 4}
    f = ai["ops"]["fill_loop"]
    assert (f["files"], f["det"], f["evaluated"], f["declined"]) == (2, 4, 2, 2)
    assert f["declined_reasons"] == {"fill_too_large: span 9 mm": 2} and ai["ops"]["sew"]["failed_reasons"] == {"timeout": 1}
    assert ai["not_proposed"] == {"no_solid": 3}
    assert ai["panel"]["by_member"]["netgen"] == {"files": 2, "status": {"fail": 2},
                                                  "error_kind": {"overlapping_facets": 2}}
    assert ai["panel"]["members_failing_per_file"] == {"1": 1, "2": 1}
    d = ai["deviation"]
    assert (d["faithful_failed"], d["over_bound"], d["reasons"]) == (1, 1, {"deviation_exceeds_bound": 1})
    assert d["budget_used"]["max"] == 1.5 and d["budget_used"]["n"] == 2
    assert ai["ai"] == {"files": 2, "turns": 6, "tool_errors": 1, "candidate_errors": 2, "declared_unfixable": 1,
                        "truncated_rounds": 1, "spent_usd": pytest.approx(0.5), "stop_reasons": {"None": 1, "max_turns": 1},
                        "ai_capped": 1}
    assert ai["not_clean_by_defect_kind"] == {"?": 2, "geometry": 1}
    assert t["arms"]["staged"]["stages"] == {"kept": {"normalise": 1, "shells": 1}, "rolled_back": {"condition": 1}}
    leads = {(x["arm"], x["what"]): x for x in t["leads"]}
    assert leads[("search_ai", "panel member still failing")]["key"] == "netgen: overlapping_facets"
    assert leads[("search_ai", "op declined")]["key"] == "fill_loop: fill_too_large: span 9 mm"
    md = triage.render_markdown(t)
    assert ai["ops"]["fill_loop"]["ops_applied"] == 2  # one applied op per file, summed over the two files
    for needle in ("## Leads", "## search_ai", "| fill_loop | 2 | 4 | 0 | 2 | 2 | 2 |", "ops charged",
                   "netgen", "1 not-clean row(s) carry NO",
                   "AI on 2 not-clean files: 6 turns, 1 tool errors", "stages kept"):
        assert needle in md, needle


def _shard_dir(out: Path, dest: Path) -> Path:
    d = dest / "shard-1-of-1"
    d.mkdir(parents=True)
    for name in ("rows.json", "EXCLUDED.json"):
        (d / name).write_text((out / name).read_text())
    (d / "PROVENANCE.json").write_text(json.dumps({"exit_code": 0, "commit": "a" * 40, "image": "img"}))
    return d.parent


def test_triage_reads_a_wave_the_real_dispatch_wrote(shard: Any, wire: Any, tmp_path: Path) -> None:  # noqa: F811
    wire.candidate = CLEAN_CANDIDATE
    rows, _ = go(shard, "input_only,staged,search_det,search_ai", ledgered(), FakeEngine())
    assert all(r.get("triage") is not None for r in rows)
    root = _shard_dir(shard.out, tmp_path / "wave")
    out_json, out_md = tmp_path / "t.json", tmp_path / "t.md"
    assert triage.main(["--from", str(root), "--json", str(out_json), "--md", str(out_md)]) == 0
    t = json.loads(out_json.read_text())
    assert t["complete_wave"] is True
    det, ai = t["arms"]["search_det"], t["arms"]["search_ai"]
    assert det["not_clean"] == 1 and det["stop_reasons"] == {"exhausted": 1}
    assert det["open_sites"]["files_with_kind"] == {"no_such_kind": 1}
    assert det["panel"]["by_member"].keys() == {"m1", "m2"}  # the staged start passes m0
    assert ai["clean"] == 1 and ai["not_clean"] == 0  # the model's candidate repairs it: nothing to explain
    assert t["arms"]["input_only"]["panel"]["by_member"].keys() == {"m0", "m1", "m2"}
    assert "## search_det" in out_md.read_text()


def test_per_file_lists_each_not_clean_search_row_with_its_sites_ops_and_deltas() -> None:
    fill = op(det=2, evaluated=1, declined={"fill_too_large: span 9 mm": 1})
    t = {**verdict_part([("netgen", "overlapping_facets")], 0.4),
         **search_part("exhausted", {"open": 1, "unlocated": 1}, {"fill_loop": fill})}
    t["search"]["deltas"] = [{"op": "fill_loop", "source": "det", "parent": [0, 1, 0.0, 0.0, 0], "score": [0, 1, 0.0, 0.0, -1]}]
    rows = [row(1, "search_det", t=t), row(2, "search_det", clean=True, t={**verdict_part([], 0.0)}),
            row(3, "staged", t=verdict_part([("netgen", "x")], 0.0))]
    out = triage.triage_wave(rows, [], per_file=True)["per_file"]
    assert [x["name"] for x in out] == ["f1.step"]  # search arms only, not-clean only
    (x,) = out
    assert x["arm"] == "search_det" and x["stop"] == "exhausted" and x["open_sites"] == {"open": 1, "unlocated": 1}
    assert x["ops"] == {"fill_loop": "det 2, evaluated 1, declined 1 (fill_too_large: span 9 mm)"}
    assert x["deltas"] == ["fill_loop [0, 1, 0.0, 0.0, 0] -> [0, 1, 0.0, 0.0, -1]"]
    assert x["panel_failing"] == ["netgen: overlapping_facets"]
    assert "## Per file" in triage.render_markdown(triage.triage_wave(rows, [], per_file=True))


def test_a_member_capped_after_doom_is_counted_apart_not_as_a_mesher_failure() -> None:
    """Lane C16: a member that ran after the panel was already doomed and hit the cap is not a defect the mesher
    found; it must not inflate 'panel member still failing' or members-failing-per-file."""
    rows = [row(1, "search_det", t={**verdict_part([("gmsh_default", "low_quality"),
                                                    ("netgen_occ", triage.CAPPED_KIND)], 0.1),
                                    **search_part("exhausted", {}, {})})]
    t = triage.triage_wave(rows, [])
    p = t["arms"]["search_det"]["panel"]
    assert list(p["by_member"]) == ["gmsh_default"] and p["capped_after_doom"] == {"netgen_occ": 1}
    assert p["members_failing_per_file"] == {"1": 1}
    assert "capped after doom" in triage.render_markdown(t)
