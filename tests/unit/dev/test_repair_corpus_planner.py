# Responsibility: Verify the corpus tool's planner instrument: the reply cache key, shadow's
#                 geometry guarantee and live's side-by-side aggregate.
# Boundaries: hermetic - the model seam is a stub router and the kernel is a stub; no CAD is read.
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.contracts import model_inference

REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load()

REPLY = json.dumps({"proposals": [{"op": "fix_small_edges", "defect_fingerprint": "fa",
                                   "targets": ["fa"], "confidence": 0.9, "rationale": "r"}]})


class _Inner:
    def __init__(self, reply=REPLY):
        self.reply, self.calls = reply, 0

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        self.calls += 1
        return SimpleNamespace(ok=True, assistant_text=self.reply)


def _entities(extra=""):
    return [{"code": "small_edge", "fingerprint": "fa", "entity_type": "edge", "entity_index": 1,
             "severity": "error", "message": "m" + extra, "measurements": {}}]


@pytest.fixture(autouse=True)
def _reset():
    yield
    model_inference.set_model_router(None)


def test_the_cache_key_covers_the_prompt_content(tmp_path):
    inner = _Inner()
    cache = rc.CachingRouter(tmp_path / "c.json", inner)
    model_inference.set_model_router(cache)
    first = rc.plan_for(_entities())
    again = rc.plan_for(_entities())
    assert inner.calls == 1 and cache.hits == 1
    assert [d["op"] for d in again.directives] == [d["op"] for d in first.directives] == [
        "fix_small_edges"]
    rc.plan_for(_entities(extra=" changed"))  # one word of the defect text differs
    assert inner.calls == 2
    # the key is over every message's role and content, system included
    base = [{"role": "system", "content": "a"}, {"role": "user", "content": "b"}]
    assert rc.prompt_key(base) != rc.prompt_key([{**base[0], "content": "a2"}, base[1]])
    assert rc.prompt_key(base) != rc.prompt_key([base[0], {**base[1], "content": "b2"}])
    # and it survives a restart: a fresh cache on the same file answers without a call
    fresh = rc.CachingRouter(tmp_path / "c.json", _Inner())
    model_inference.set_model_router(fresh)
    rc.plan_for(_entities())
    assert fresh.inner.calls == 0 and fresh.hits == 1


@pytest.fixture
def kernel(monkeypatch, tmp_path):
    """A stub kernel: the output bytes depend only on whether directives were handed in."""
    import meshpipeline.cad.repair.conservative as cons
    import meshpipeline.cad.repair.contracts as contracts
    import meshpipeline.cad.repair.triage as triage

    seen = []

    def repair(source, destination, *, profile=None, directives=()):
        seen.append(tuple(directives))
        Path(destination).write_bytes(b"llm" if directives else b"rules")
        ops = [{"name": "fix_shape_global", "status": "rolled_back", "targets": ["fz"]}]
        if directives:
            ops = [{"name": d["op"], "status": "kept", "targets": d["targets"],
                    "source": d["source"], "directive_id": d["directive_id"]} for d in directives]
        return SimpleNamespace(operations=ops, measurements=[
            SimpleNamespace(name="operations_attempted", value=len(ops))])

    def inspect(path):
        fixed = Path(path).name.endswith("-repaired.step") and Path(path).read_bytes() == b"llm"
        located = {} if fixed else {"small_edge": 2}
        return {"report": SimpleNamespace(defects=[], to_dict=lambda: {}),
                "defects": [], "entities": [] if fixed else _entities(), "valid": True,
                "counts": {}, "located": {"by_code": located}}

    monkeypatch.setattr(cons, "repair_step_file", repair)
    monkeypatch.setattr(contracts, "status_from_defects",
                        lambda defects: SimpleNamespace(value="repairable"))
    monkeypatch.setattr(triage, "recommend", lambda **kw: SimpleNamespace(
        route=triage.ROUTE_CONSERVATIVE_REPAIR, confidence=1.0, targets=[], abstain_reason=""))
    monkeypatch.setattr(rc, "_inspect", inspect)
    part = tmp_path / "part.step"
    part.write_text("x")
    return SimpleNamespace(seen=seen, part=part)


def test_shadow_touches_no_geometry(kernel, tmp_path):
    model_inference.set_model_router(rc.CachingRouter(None, _Inner()))
    off_dir, shadow_dir = tmp_path / "off", tmp_path / "shadow"
    off_dir.mkdir(), shadow_dir.mkdir()
    off = rc._one(kernel.part, off_dir)
    shadow = rc._one(kernel.part, shadow_dir, mode="shadow")
    assert (off_dir / "part-repaired.step").read_bytes() == (
        shadow_dir / "part-repaired.step").read_bytes()
    assert all(d == () for d in kernel.seen)  # the kernel was never handed a directive
    assert shadow["plan"]["proposals"][0]["op"] == "fix_small_edges"
    assert shadow["repaired_defects"] == off["repaired_defects"]
    summary = rc.planner_summary([shadow], "shadow")
    assert summary["coverage"] == 1 and summary["compared"] == 1 and summary["agreed"] == 0


def test_live_reports_aggregate_against_rules_only(kernel, tmp_path):
    model_inference.set_model_router(rc.CachingRouter(None, _Inner()))
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_row = rc._one(kernel.part, rules_dir)
    live_row = rc._one(kernel.part, tmp_path, mode="live")
    live_row["rules_only"] = rules_row
    assert kernel.seen[-1] and kernel.seen[-1][0]["source"] == "llm-planner"
    summary = rc.planner_summary([live_row], "live")
    assert summary["rules_only_total"] == 0 and summary["live_total"] == 2
    assert summary["llm_ops"] == {"fix_small_edges": {"kept": 1, "rolled_back": 0, "declined": 0, "failed": 0}}


def test_per_op_outcomes_keep_declined_and_failed_apart():
    ops = [{"op": "sew_faces", "status": s, "reason": r, "source": "llm-planner"}
           for s, r in (("kept", ""), ("rolled_back", "no gain"),
                        ("rolled_back", "declined: not applicable"), ("failed", "boom"))]
    row = {"plan": {"proposals": [], "declines": 0, "call_failures": 0, "called": 1},
           "operations": ops, "rules_only": {}}
    got = rc.planner_summary([row], "live")["llm_ops"]["sew_faces"]
    assert got == {"kept": 1, "rolled_back": 1, "declined": 1, "failed": 1}


def test_live_worse_than_rules_only_is_reported_truthfully():
    rules = {"repaired_defects": {"small_edge": 3}}
    live = {"repaired_defects": {"small_edge": 1}, "operations": [], "rules_only": rules}
    s = rc.planner_summary([live], "live")
    assert (s["rules_only_total"], s["live_total"]) == (3, 1)
    assert s["rules_only_repaired"] == {"small_edge": 3} and s["live_repaired"] == {"small_edge": 1}


def test_agreement_true_path_and_mismatch():
    props = [{"op": "sew_faces", "targets": ["b", "a"]}]
    assert rc.agreement(props, [{"op": "sew_faces", "targets": ["a", "b"]}]) is True
    assert rc.agreement(props, [{"op": "sew_faces", "targets": ["a"]}]) is False
    row = {"plan": {"proposals": props, "declines": 0, "call_failures": 0, "called": 1},
           "operations": [{"op": "sew_faces", "targets": ["a", "b"]}]}
    assert rc.planner_summary([row], "shadow")["agreed"] == 1


def test_no_model_path_never_raises_and_says_so(capsys):
    model_inference.set_model_router(None)
    plan = rc.plan_for(_entities())
    assert plan.directives == [] and plan.called == 0
    rc._print_planner(rc.planner_summary([{"plan": {"proposals": [], "declines": 0,
                      "call_failures": 0, "called": 0}}], "shadow"), None)
    assert "no model was available" in capsys.readouterr().out


def test_corrupt_cache_is_a_one_line_error_naming_the_path(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(rc.CacheError, match=str(bad)):
        rc.CachingRouter(bad)
