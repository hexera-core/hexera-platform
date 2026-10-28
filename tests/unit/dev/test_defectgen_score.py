# Responsibility: Pin the generated-defect scorer: the answer join, the detection and specificity
#                 arithmetic, the declared taxonomy split, triage reachability, repair outcomes,
#                 and the CLI envelope.
# Boundaries: hermetic - synthetic rows JSON and synthetic answer directories in tmp_path, built
#             with the real field names from the campaign's answer.json and the corpus runner's
#             --json rows. No CAD is read and no engine code is imported.
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MODULE = REPO / "devtools" / "validation" / "defectgen_score.py"


def _load():
    spec = importlib.util.spec_from_file_location("defectgen_score_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ds = _load()


# -- fixtures built with the real field names ---------------------------------------------------

def _answer(case_id: str, operators: list[str], categories: list[str] | None = None) -> dict:
    """An answer.json shaped like the campaign's: per-defect category, causal_edit.operator,
    geometric_witness and expected_repair. Only the fields the scorer reads carry real values."""
    categories = categories or ["wall_or_clearance"] * len(operators)
    return {
        "campaign_id": "c_test",
        "case_id": case_id,
        "defects": [
            {
                "id": f"edit_{i}",
                "category": cat,
                "causal_edit": {"operator": op, "order": i, "site_class": "face",
                                "realized_parameters": {"radius_mm": 1.0}},
                "expected_repair": "Restore the local solid-region ligament.",
                "geometric_witness": {"changed_probe_count": 3, "changed_probes": []},
            }
            for i, (op, cat) in enumerate(zip(operators, categories))
        ],
        "label_scope": "injected_changes_only",
        "schema_version": 1,
    }


def _write_answers(root: Path, answers: dict[str, dict]) -> Path:
    campaign = root / "answers" / "c_test"
    for case_id, answer in answers.items():
        case_dir = campaign / case_id
        case_dir.mkdir(parents=True)
        (case_dir / "answer.json").write_text(json.dumps(answer))
        (case_dir / "evidence.json").write_text("{}")
    (campaign / "manifest.json").write_text(json.dumps({"campaign_id": "c_test"}))
    return campaign


def _row(stem: str, *, defects=("small_face",), route="manual_cleanup",
         outcome="not_attempted:manual_cleanup", **extra) -> dict:
    """A row shaped like repair_corpus.py --json output."""
    row = {
        "file": f"{stem}.step",
        "size_bytes": 1000,
        "valid_before": True,
        "counts_before": {"solids": 1, "shells": 1, "faces": 6, "edges": 12, "vertices": 24},
        "defects_before": sorted(defects),
        "located_before": dict.fromkeys(defects, 1),
        "regions_before": {"interior": 1},
        "status": "clean",
        "route": route,
        "confidence": 0.6,
        "targets": [{"entity": "face:5", "code": code, "region": "interior"} for code in defects],
        "abstain_reason": "",
        "outcome": outcome,
        "seconds": 0.5,
    }
    row.update(extra)
    return row


def _run(tmp_path: Path, rows: list[dict], answers: dict[str, dict]) -> dict:
    campaign = _write_answers(tmp_path, answers)
    rows_path = tmp_path / "rows.json"
    rows_path.write_text(json.dumps(rows))
    return ds.score(ds.load_rows(rows_path), ds.load_answers(campaign))


# -- integrity ----------------------------------------------------------------------------------

def test_the_scorer_imports_nothing_from_the_engine_or_runner():
    import ast
    tree = ast.parse(MODULE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(name.startswith(("meshpipeline", "repair_corpus")) for name in imported), imported
    # The rule itself is printed where a reader will see it.
    assert "after" in (ds.__doc__ or "").lower() and "import" in (ds.__doc__ or "").lower()


def test_the_taxonomy_names_every_generator_operator_and_only_real_inspector_codes():
    from meshpipeline.cad.repair.contracts import DefectCode
    operators = {"thin_wall", "extreme_thin_ligament", "short_edge_sliver", "residual_flash",
                 "residual_boss", "pin_hole", "micro_slot", "internal_void", "isolated_debris",
                 "fluid_obstruction", "missing_face", "duplicate_face", "tolerance_inflation"}
    assert operators <= set(ds.TAXONOMY)
    valid = {c.value for c in DefectCode}
    for op, codes in ds.TAXONOMY.items():
        assert set(codes) <= valid, f"{op} maps to codes the inspector does not emit: {codes}"


def test_fluid_and_interface_families_fall_back_to_their_prefix_entry():
    assert ds.expected_codes("fluid_leak_path") == ds.expected_codes("fluid_obstruction")
    assert ds.expected_codes("interface_shift") == frozenset()


# -- detection and specificity ------------------------------------------------------------------

def test_a_damaged_case_counts_detected_only_when_inspection_reported_something(tmp_path):
    result = _run(tmp_path,
                  [_row("aaa"), _row("bbb", defects=(), route="mesh_as_is",
                        outcome="nothing_to_repair")],
                  {"aaa": _answer("aaa", ["thin_wall"]), "bbb": _answer("bbb", ["thin_wall"])})
    detection = result["aggregate"]["detection"]
    assert detection["damaged_cases"] == 2
    assert detection["detected"] == 1
    assert detection["detection_rate"] == 0.5


def test_specificity_counts_false_positives_only_over_zero_defect_answers(tmp_path):
    clean = _answer("ccc", [])
    result = _run(tmp_path,
                  [_row("aaa"), _row("ccc")],  # ccc reports small_face on an undamaged case
                  {"aaa": _answer("aaa", ["thin_wall"]), "ccc": clean})
    detection = result["aggregate"]["detection"]
    assert detection["clean_cases"] == 1
    assert detection["false_positives"] == 1
    assert detection["false_positive_rate"] == 1.0


def test_with_no_clean_cases_the_false_positive_rate_is_null_not_zero(tmp_path):
    result = _run(tmp_path, [_row("aaa")], {"aaa": _answer("aaa", ["thin_wall"])})
    assert result["aggregate"]["detection"]["false_positive_rate"] is None


# -- taxonomy split: missed-detectable versus not-claimed ----------------------------------------

def test_an_undetected_sliver_is_a_miss_but_an_undetected_thin_wall_is_not_claimed(tmp_path):
    result = _run(tmp_path,
                  [_row("sli", defects=()), _row("wal", defects=())],
                  {"sli": _answer("sli", ["short_edge_sliver"]),
                   "wal": _answer("wal", ["thin_wall"])})
    ops = result["aggregate"]["operators"]
    assert ops["short_edge_sliver"]["expected_detectable"] is True
    assert ops["short_edge_sliver"]["missed_detectable"] == 1
    assert ops["thin_wall"]["expected_detectable"] is False
    assert ops["thin_wall"]["missed_detectable"] == 0


def test_expected_detection_requires_a_mapped_code_not_just_any_code(tmp_path):
    # open_shell on a sliver case is detection of something, but not of the mapped codes.
    result = _run(tmp_path, [_row("sli", defects=("open_shell",))],
                  {"sli": _answer("sli", ["short_edge_sliver"])})
    op = result["aggregate"]["operators"]["short_edge_sliver"]
    assert op["detected_any"] == 1
    assert op["detected_expected"] == 0
    assert op["missed_detectable"] == 1


def test_an_operator_outside_the_table_is_reported_as_unknown_not_scored_detectable(tmp_path):
    result = _run(tmp_path, [_row("xxx")], {"xxx": _answer("xxx", ["brand_new_operator"])})
    assert "brand_new_operator" in result["aggregate"]["unknown_operators"]
    assert result["aggregate"]["operators"]["brand_new_operator"]["expected_detectable"] is False


# -- triage reachability ------------------------------------------------------------------------

def test_routes_bucket_into_repair_review_manual_cleanup_and_no_op(tmp_path):
    rows = [
        _row("a", route="conservative_repair", outcome="repaired",
             defects_after=[], located_after={}, repaired_defects={"small_face": 1},
             operations=[{"op": "drop_small_faces", "status": "kept"}]),
        _row("b", route="ask_customer", outcome="not_attempted:ask_customer"),
        _row("c", route="abstain", outcome="not_attempted:abstain"),
        _row("d", route="manual_cleanup"),
        _row("e", route="mesh_as_is", outcome="nothing_to_repair", defects=()),
    ]
    answers = {s: _answer(s, ["pin_hole"]) for s in "abcde"}
    routes = _run(tmp_path, rows, answers)["aggregate"]["operators"]["pin_hole"]["routes"]
    assert routes == {"repair": 1, "review": 2, "manual_cleanup": 1, "no_op": 1}


# -- repair outcomes per operator ---------------------------------------------------------------

def test_repair_outcomes_split_repaired_rolled_back_damaged_failed(tmp_path):
    rows = [
        # residual_flash claims small_face, and the re-inspection proves small_face went: credit.
        _row("a", route="conservative_repair", outcome="repaired",
             repaired_defects={"small_face": 1}),
        _row("b", route="conservative_repair", outcome="refused"),
        _row("c", route="conservative_repair", outcome="no_change"),
        _row("d", route="conservative_repair", outcome="changed_without_fixing"),
        _row("e", route="conservative_repair", outcome="repair_failed"),
    ]
    answers = {s: _answer(s, ["residual_flash"]) for s in "abcde"}
    repair = _run(tmp_path, rows, answers)["aggregate"]["operators"]["residual_flash"]["repair"]
    assert repair == {"attempted": 5, "repaired": 1, "not_repaired": 0, "unknown": 0,
                      "rolled_back": 2, "damaged": 1, "failed": 1}


def test_an_operator_whose_own_damage_survived_the_repair_is_not_credited(tmp_path):
    """A kept repair is not a repair of everything the case carried.

    The case is reported `repaired` because the written file is emptier, but the only code cleared
    is `open_shell` - which `residual_flash` does not claim. Crediting it here would say the engine
    repairs flash when what it actually did was sew a shell.
    """
    rows = [_row("a", route="conservative_repair", outcome="repaired",
                 repaired_defects={"open_shell": 2})]
    repair = _run(tmp_path, rows, {"a": _answer("a", ["residual_flash"])})[
        "aggregate"]["operators"]["residual_flash"]["repair"]
    assert repair["repaired"] == 0
    assert repair["not_repaired"] == 1


def test_an_operator_we_claim_no_codes_for_reports_unknown_rather_than_credit(tmp_path):
    """`thin_wall` has no inspector code, so no cleared code can prove its damage was repaired.

    Unknown is the honest answer; counting it as repaired would overstate the engine against
    exactly the damage the taxonomy admits it cannot see.
    """
    rows = [_row("a", route="conservative_repair", outcome="repaired",
                 repaired_defects={"small_face": 3})]
    repair = _run(tmp_path, rows, {"a": _answer("a", ["thin_wall"])})[
        "aggregate"]["operators"]["thin_wall"]["repair"]
    assert repair["unknown"] == 1
    assert repair["repaired"] == 0


def test_a_case_never_routed_to_repair_counts_no_attempt(tmp_path):
    result = _run(tmp_path, [_row("a")], {"a": _answer("a", ["pin_hole"])})
    assert result["aggregate"]["operators"]["pin_hole"]["repair"]["attempted"] == 0


# -- the join ------------------------------------------------------------------------------------

def test_unmatched_rows_and_answers_are_reported_not_silently_dropped(tmp_path):
    result = _run(tmp_path, [_row("aaa"), _row("zzz")],
                  {"aaa": _answer("aaa", ["thin_wall"]), "yyy": _answer("yyy", ["pin_hole"])})
    assert result["aggregate"]["rows_without_answers"] == ["zzz"]
    assert result["aggregate"]["answers_without_rows"] == ["yyy"]
    assert result["aggregate"]["cases_scored"] == 1


def test_per_case_records_carry_the_fields_a_bundle_needs(tmp_path):
    result = _run(tmp_path, [_row("aaa")], {"aaa": _answer("aaa", ["short_edge_sliver"])})
    case = result["cases"]["aaa"]
    assert case["detected"] is True
    assert case["detected_codes"] == ["small_face"]
    assert case["route"] == "manual_cleanup"
    assert case["outcome"] == "not_attempted:manual_cleanup"
    assert case["defects"][0]["operator"] == "short_edge_sliver"
    assert case["defects"][0]["category"] == "wall_or_clearance"
    assert case["defects"][0]["expected_detectable"] is True
    assert case["defects"][0]["detected_expected"] is True


# -- CLI ----------------------------------------------------------------------------------------

def test_cli_scores_writes_json_and_prints_the_operator_table(tmp_path, capsys):
    campaign = _write_answers(tmp_path, {"aaa": _answer("aaa", ["thin_wall"]),
                                         "bbb": _answer("bbb", ["short_edge_sliver"])})
    rows_path = tmp_path / "rows.json"
    rows_path.write_text(json.dumps([_row("aaa"), _row("bbb")]))
    out_path = tmp_path / "score.json"

    code = ds.main([str(rows_path), str(campaign), "--json", str(out_path)])

    assert code == 0
    written = json.loads(out_path.read_text())
    assert written["aggregate"]["detection"]["detected"] == 2
    assert set(written["cases"]) == {"aaa", "bbb"}
    printed = capsys.readouterr().out
    assert "thin_wall" in printed and "short_edge_sliver" in printed
    assert "detection" in printed
