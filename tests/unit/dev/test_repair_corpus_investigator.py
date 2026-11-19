# Responsibility: Verify the corpus runner's investigator wiring - the transcript sink, the
#                 verdict join, the per-row spend block, the replay load and the fail-closed
#                 posture - and that none of it touches a row when the flags are unset.
# Boundaries: hermetic - the repair, the inspection and the triage are stubbed, the records are
#             synthetic dicts, and no CAD is read. No gmsh, OCP or fTetWild process is started,
#             and no corpus pass is run.
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_investigator_under_test",
        REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load()


def _record(*, index=0, attempt=1, end="concluded", tool_calls=3, model_calls=2, seconds=4.5,
            input_tokens=1200, output_tokens=300, fingerprints=("edge:a", "face:b")):
    """A gym record as the investigator writes one (investigation/v1): the conversation, the
    region's identity, the spend and how it ended. Never geometry."""
    return {
        "schema": "investigation/v1",
        "part_sha256": "a" * 64,
        "posture": "live",
        "label": "investigator",
        "region": {"index": index, "fingerprints": sorted(fingerprints)},
        "attempt": attempt,
        "budget": {"tool_calls_max": 8, "model_calls_max": 4, "seconds_max": 60.0},
        "messages": [{"role": "user", "content": "look at region 0"},
                     {"role": "assistant", "content": "{\"plan\": []}"}],
        "turns": 2,
        "edits_kept": 1,
        "conclusion": {"proposal": {"plan": [], "predicate": {}}},
        "end": end,
        "spent": {"tool_calls": tool_calls, "model_calls": model_calls, "seconds": seconds,
                  "input_tokens": input_tokens, "output_tokens": output_tokens},
    }


# ---------------------------------------------------------------- the posture, fail-closed

def test_investigator_posture_is_fail_closed():
    assert rc.investigator_posture("live") == "live"
    assert rc.investigator_posture("shadow") == "shadow"
    for junk in ("", "on", "true", "LIVE", None, "1"):
        assert rc.investigator_posture(junk) == "off", junk


# ---------------------------------------------------------------- the sink

def test_sink_gets_one_record_per_author_call(tmp_path):
    out = tmp_path / "transcripts.jsonl"
    sink = rc.TranscriptSink(out)
    sink(_record(index=0))
    sink(_record(index=1, attempt=2))
    sink.close()
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2 == len(sink.records)
    assert [r["region"]["index"] for r in lines] == [0, 1]


def test_sink_since_returns_only_this_file_s_records(tmp_path):
    sink = rc.TranscriptSink(tmp_path / "t.jsonl")
    sink(_record(index=0))
    start = len(sink.records)
    sink(_record(index=7))
    assert [r["region"]["index"] for r in sink.since(start)] == [7]


def test_record_is_json_safe_and_carries_no_geometry(tmp_path):
    hostile = _record()
    hostile["shape"] = object()
    hostile["brep_bytes"] = b"CASCADE Topology V3"
    hostile["conclusion"]["step_bytes"] = b"ISO-10303-21;"
    hostile["messages"].append({"role": "tool", "content": b"\x00\x01binary"})
    hostile["spent"]["seconds"] = float("nan")
    hostile["budget"]["seconds_max"] = math.inf
    clean = rc.scrub_record(hostile)
    text = json.dumps(clean, allow_nan=False)  # must not raise
    assert "shape" not in clean and "brep_bytes" not in clean
    assert "step_bytes" not in clean["conclusion"]
    assert "CASCADE" not in text and "ISO-10303-21" not in text
    assert clean["spent"]["seconds"] is None and clean["budget"]["seconds_max"] is None
    assert clean["region"]["fingerprints"] == ["edge:a", "face:b"]


def test_sink_writes_only_scrubbed_records(tmp_path):
    out = tmp_path / "t.jsonl"
    sink = rc.TranscriptSink(out)
    sink({**_record(), "brep": b"CASCADE Topology V3"})
    sink.close()
    assert "CASCADE" not in out.read_text(encoding="utf-8")
    assert "brep" not in sink.records[0]


# ---------------------------------------------------------------- the verdict join

def test_verdicts_join_by_region_index_and_attempt():
    measured = {"packages": [
        {"region_index": 0, "status": "committed", "reason": ""},
        {"region_index": 0, "status": "reverted", "reason": "predicate_failed"},
        {"region_index": 1, "status": "escalated", "reason": "budget"},
    ]}
    verdicts = rc.package_verdicts(measured)
    joined = rc.join_verdicts([_record(index=0, attempt=2), _record(index=1, attempt=1),
                               _record(index=9, attempt=1)], verdicts)
    assert joined[0]["verdict"]["status"] == "reverted"
    assert joined[1]["verdict"]["status"] == "escalated"
    assert "verdict" not in joined[2]  # no package for that region: absent, never invented


def test_join_leaves_the_caller_s_records_alone():
    record = _record(index=0, attempt=1)
    rc.join_verdicts([record], rc.package_verdicts(
        {"packages": [{"region_index": 0, "status": "committed"}]}))
    assert "verdict" not in record


# ---------------------------------------------------------------- the row block

def test_row_block_aggregates_the_spend_and_the_endings():
    block = rc.investigator_row([
        _record(index=0, tool_calls=3, model_calls=2, seconds=4.5, input_tokens=1200,
                output_tokens=300),
        _record(index=0, attempt=2, end="replay_miss", tool_calls=0, model_calls=0, seconds=0.0,
                input_tokens=0, output_tokens=0),
        _record(index=1, end="tool_budget", tool_calls=8, model_calls=1, seconds=11.25,
                input_tokens=800, output_tokens=90),
    ])
    assert block["regions"] == 2
    assert block["tool_calls"] == 11
    assert block["model_calls"] == 3
    assert block["seconds"] == 15.75
    assert block["input_tokens"] == 2000
    assert block["output_tokens"] == 390
    assert block["ends"] == {"concluded": 1, "replay_miss": 1, "tool_budget": 1}


def test_row_block_reads_top_level_counters_too():
    flat = _record()
    flat.update(flat.pop("spent"))
    block = rc.investigator_row([flat])
    assert block["tool_calls"] == 3 and block["model_calls"] == 2
    assert block["input_tokens"] == 1200


def test_no_records_means_no_block():
    assert rc.investigator_row([]) == {}


def test_summary_counts_the_rows_that_carry_a_block():
    rows = [{"investigator": rc.investigator_row([_record()])},
            {"investigator": rc.investigator_row([_record(end="replay_miss")])},
            {"outcome": "nothing_to_repair"}]
    summary = rc.investigator_summary(rows)
    assert summary["files"] == 2
    assert summary["model_calls"] == 4
    assert summary["ends"] == {"concluded": 1, "replay_miss": 1}
    assert rc.investigator_summary([{"outcome": "x"}]) == {}


# ---------------------------------------------------------------- the replay input

def test_load_replay_reads_records_and_ignores_blank_lines(tmp_path):
    path = tmp_path / "in.jsonl"
    path.write_text("\n".join([json.dumps(_record(index=0)), "", "  ",
                               json.dumps(_record(index=1))]) + "\n", encoding="utf-8")
    records = rc.load_replay(path)
    assert [r["region"]["index"] for r in records] == [0, 1]


def test_load_replay_names_the_bad_line_rather_than_dying_obscurely(tmp_path):
    path = tmp_path / "in.jsonl"
    path.write_text(json.dumps(_record()) + "\n{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        rc.load_replay(path)
    assert "line 2" in str(exc.value)


# ---------------------------------------------------------------- the recorded settings

def test_settings_record_all_three_flags_when_set():
    assert rc.investigator_settings("live", "out.jsonl", "in.jsonl") == {
        "investigator": "live", "transcripts": "out.jsonl", "investigator_replay": "in.jsonl"}


def test_settings_gain_nothing_when_no_flag_was_given():
    assert rc.investigator_settings("off", "", "") == {}
    assert rc.investigator_settings("", "", "") == {}


# ---------------------------------------------------------------- the install seam

def test_off_installs_nothing():
    sink, note = rc.install_investigator("off")
    assert sink is None and note == ""


def test_install_offers_the_sink_to_the_investigator_module(monkeypatch, tmp_path):
    module = SimpleNamespace(sink=None, replay_records=None)
    monkeypatch.setattr(rc, "_investigator_module", lambda: module)
    records_in = tmp_path / "in.jsonl"
    records_in.write_text(json.dumps(_record()) + "\n", encoding="utf-8")
    sink, note = rc.install_investigator("live", transcripts=str(tmp_path / "out.jsonl"),
                                         replay=str(records_in))
    assert module.sink is sink is not None
    assert len(module.replay_records) == 1
    assert "seam" not in note


def test_install_reports_the_seam_when_the_build_has_no_investigator(monkeypatch):
    monkeypatch.setattr(rc, "_investigator_module", lambda: None)
    sink, note = rc.install_investigator("live", transcripts="")
    assert sink is not None  # the runner still collects what it is handed
    assert "seam" in note and "investigator" in note


def test_install_reports_the_seam_when_the_module_takes_no_replay(monkeypatch, tmp_path):
    module = SimpleNamespace(sink=None)  # no replay_records attribute
    monkeypatch.setattr(rc, "_investigator_module", lambda: module)
    records_in = tmp_path / "in.jsonl"
    records_in.write_text(json.dumps(_record()) + "\n", encoding="utf-8")
    sink, note = rc.install_investigator("live", replay=str(records_in))
    assert module.sink is sink
    assert "replay_records" in note and "seam" in note


def test_install_never_invents_an_attribute_on_another_module(monkeypatch):
    module = SimpleNamespace()  # declares neither
    monkeypatch.setattr(rc, "_investigator_module", lambda: module)
    rc.install_investigator("live")
    assert not hasattr(module, "sink")


# ---------------------------------------------------------------- flags off: the row is unchanged

@pytest.fixture
def kernel(monkeypatch, tmp_path):
    """A stub repair whose report carries a region run, so the join has packages to read."""
    import meshpipeline.cad.repair.conservative as cons
    import meshpipeline.cad.repair.contracts as contracts
    import meshpipeline.cad.repair.triage as triage

    state = SimpleNamespace(seen=[])

    def repair(source, destination, **kwargs):
        state.seen.append(dict(kwargs))
        Path(destination).write_text("repaired")
        return SimpleNamespace(operations=[], measurements=[
            SimpleNamespace(name="operations_attempted", value=1),
            SimpleNamespace(name="regions", value={"packages": [
                {"region_index": 0, "status": "committed", "reason": ""}]}),
        ])

    def inspect(path):
        return {"report": SimpleNamespace(defects=[], to_dict=lambda: {}),
                "defects": [], "entities": [{"entity": "f1", "code": "small_edge"}],
                "valid": True, "counts": {}, "located": {"by_code": {"small_edge": 1}}}

    monkeypatch.setattr(cons, "repair_step_file", repair)
    monkeypatch.setattr(contracts, "status_from_defects",
                        lambda defects: SimpleNamespace(value="repairable"))
    monkeypatch.setattr(triage, "recommend", lambda **kw: SimpleNamespace(
        route=triage.ROUTE_CONSERVATIVE_REPAIR, confidence=1.0, targets=[], abstain_reason=""))
    monkeypatch.setattr(rc, "_inspect", inspect)
    part = tmp_path / "part.step"
    part.write_text("x")
    state.part = part
    return state


def test_corpus_flags_off_rows_unchanged(kernel, tmp_path):
    plain = rc._repair_row(kernel.part, tmp_path)
    explicit = rc._repair_row(kernel.part, tmp_path, sink=None)
    assert plain.keys() == explicit.keys()
    assert "investigator" not in plain


def test_a_sink_with_records_adds_the_block_and_the_verdict(kernel, tmp_path):
    sink = rc.TranscriptSink(None)

    def repair(source, destination, **kwargs):
        sink(_record(index=0, attempt=1))
        return _ORIGINAL(source, destination, **kwargs)

    import meshpipeline.cad.repair.conservative as cons
    _ORIGINAL = cons.repair_step_file
    cons.repair_step_file = repair
    try:
        row = rc._repair_row(kernel.part, tmp_path, sink=sink)
    finally:
        cons.repair_step_file = _ORIGINAL
    assert row["investigator"]["regions"] == 1
    assert row["investigator"]["model_calls"] == 2
    assert sink.records[0]["verdict"]["status"] == "committed"
