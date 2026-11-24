"""v4_eval evalspec: the arms, the waves and the cost policy. Hermetic: markers are a dict, ledgers live
in tmp_path, nothing reaches a subprocess, a bucket or a model."""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
import run_arm  # noqa: E402  (loads evalspec under a stable name)

ev = run_arm.evalspec


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"a test reached a subprocess/network: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


class Markers:
    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, k):
        return self.d.get(k)

    def create(self, k, v):
        if k in self.d:
            return False
        self.d[k] = v
        return True

    def put(self, k, v):
        self.d[k] = v


def _caps():
    from meshpipeline.cad.repair_v4 import model_ledger

    return model_ledger.WAVE_CAPS_USD


def test_arms_share_budget_except_model() -> None:
    shared = {(a.budget.wall_s, a.budget.ops) for a in ev.ARMS.values()}
    assert shared == {(600.0, 60)}
    assert {n for n, a in ev.ARMS.items() if a.budget.model_usd} == {"search_ai"}
    assert ev.ARMS["search_ai"].budget.model_usd == 1.00
    # B24 adds search_seq on the SHARED budget: the arms are compared on method, never on budget, and it is
    # det-only for now (no model ceiling), so both assertions above still hold for it.
    assert set(ev.ARMS) == {"input_only", "staged", "search_det", "search_ai", "search_seq",
                            "v3_meshability_pass_ai"}


def test_aliases_resolve_to_one_arm_not_a_second() -> None:
    assert ev.arm("baseline") is ev.ARMS["v3_meshability_pass_ai"]
    assert ev.arm("heal_det") is ev.ARMS["staged"]
    with pytest.raises(ValueError, match="unknown arm"):
        ev.arm("search_gpt")


def test_an_arm_cannot_claim_a_model_without_a_ceiling_or_the_reverse() -> None:
    with pytest.raises(ValueError, match="model"):
        ev.Arm("x", "d", ev.Budget(model_usd=1.0), uses_model=False)
    with pytest.raises(ValueError, match="model"):
        ev.Arm("x", "d", ev.Budget(), uses_model=True)


@pytest.mark.parametrize("bad", [math.inf, math.nan, -1.0])
def test_budget_refuses_inf_nan_negative_model_usd(bad) -> None:
    with pytest.raises(ValueError):
        ev.Budget(model_usd=bad)


def test_every_wave_cap_is_the_ledgers_and_finite() -> None:
    caps = _caps()
    for w in ev.WAVES.values():
        assert w.cap_usd == caps[w.wave_type] and math.isfinite(w.cap_usd) and w.cap_usd > 0
    assert {w.wave_type for w in ev.WAVES.values()} == {"normal", "checkpoint", "held_out"}
    assert ev.WAVES["heldout"].cap_usd == 250.0 and ev.WAVES["dev_checkpoint"].cap_usd == 300.0
    assert ev.WAVES["dev_search"].cap_usd == 50.0


def test_the_snapshot_waves_run_all_four_v4_arms_without_a_checkpoint_slot() -> None:
    m = Markers()
    for name, split in (("snap_v1_dev", "dev"), ("snap_v1_clean", "clean"),
                        ("snap_v1r_dev", "dev"), ("snap_v1r_clean", "clean")):
        w = ev.WAVES[name]
        assert (w.wave_type, w.split) == ("normal", split)
        assert w.arms == ("input_only", "staged", "search_det", "search_ai") and w.has_model_arm
        assert ev.plan_wave(w, wave_id=f"s-{name}", n_shards=6, markers=m).slot is None


def test_a_wave_type_the_ledger_does_not_price_cannot_be_named() -> None:
    with pytest.raises(ValueError, match="no spending cap"):
        ev.WaveDef("w", "unlimited", "dev", ("staged",))
    with pytest.raises(TypeError):
        ev.WaveDef("w", "dev", ("staged",))  # type: ignore[call-arg]  # wave_type is not optional


def test_a_removed_cap_fails_loudly_rather_than_meaning_no_cap(monkeypatch) -> None:
    monkeypatch.delitem(_caps(), "checkpoint")
    with pytest.raises(ValueError, match="no spending cap"):
        ev.plan_wave(ev.WAVES["dev_checkpoint"], wave_id="w", n_shards=3, markers=Markers())


@pytest.mark.parametrize("cap", [math.inf, 0.0, math.nan])
def test_an_infinite_or_zero_cap_in_the_table_is_refused(monkeypatch, cap) -> None:
    monkeypatch.setitem(_caps(), "normal", cap)
    with pytest.raises(ValueError, match="finite and positive"):
        ev.plan_wave(ev.WAVES["dev_search"], wave_id="w", n_shards=3, markers=Markers())


def test_heldout_split_and_type_are_welded_together() -> None:
    with pytest.raises(ValueError, match="held-out"):
        ev.WaveDef("w", "normal", "heldout", ("staged",))
    with pytest.raises(ValueError, match="held-out"):
        ev.WaveDef("w", "held_out", "dev", ("staged",))


def test_wave_arms_are_validated_and_normalised() -> None:
    w = ev.WaveDef("w", "normal", "dev", ("heal_det", "search_det"))
    assert w.arms == ("staged", "search_det")
    for bad in ((), ("staged", "staged"), ("nope",)):
        with pytest.raises(ValueError):
            ev.WaveDef("w", "normal", "dev", bad)


def test_heldout_wave_runs_once_checkpoint_three_times() -> None:
    m = Markers()
    held = ev.WAVES["heldout"]
    assert ev.plan_wave(held, wave_id="h1", n_shards=4, markers=m).slot == "slot/held_out/1"
    assert ev.plan_wave(held, wave_id="h1", n_shards=4, markers=m).slot == "slot/held_out/1"  # resume
    with pytest.raises(ev.WaveSlotSpent, match="spent"):
        ev.plan_wave(held, wave_id="h2", n_shards=4, markers=m)
    cp = ev.WAVES["dev_checkpoint"]
    for i in range(3):
        ev.plan_wave(cp, wave_id=f"c{i}", n_shards=4, markers=m)
    with pytest.raises(ev.WaveSlotSpent):
        ev.plan_wave(cp, wave_id="c3", n_shards=4, markers=m)
    assert ev.plan_wave(ev.WAVES["dev_search"], wave_id="n", n_shards=4, markers=m).slot is None


def test_a_wave_plan_cannot_be_built_around_plan_wave() -> None:
    with pytest.raises(TypeError, match="plan_wave"):
        ev.WavePlan(ev.WAVES["dev_search"], "w", 2, 50.0, None)


def test_the_shard_ledger_carries_the_wave_types_cap(tmp_path) -> None:
    m = Markers()
    plan = ev.plan_wave(ev.WAVES["heldout"], wave_id="h1", n_shards=5, markers=m)
    led = plan.open_ledger(0, tmp_path / "s0.log", m)
    assert led.cap_usd == 250.0 / 5 == plan.shard_cap_usd
    assert led.provenance == "shard"
    assert plan.provenance()["wave_cap_usd"] == 250.0
    argv = plan.shard_argv(1, ledger_path="/l/log.jsonl", marker_bucket="b")
    assert "--wave-type" in argv and "held_out" in argv


def test_a_shard_started_with_another_wave_type_is_refused(tmp_path) -> None:
    m = Markers()
    ev.plan_wave(ev.WAVES["dev_search"], wave_id="w", n_shards=2, markers=m).open_ledger(0, tmp_path / "a", m)
    from meshpipeline.cad.repair_v4 import model_ledger

    with pytest.raises(model_ledger.ShardArithmeticError):
        model_ledger.WaveLedger.for_shard("checkpoint", wave_id="w", n_shards=2, shard_index=1,
                                          path=tmp_path / "b", markers=m)


def test_the_heldout_firewall_refuses_the_split_and_its_files() -> None:
    for item in ("heldout", "devtools/validation/corpora/v4/heldout.json", "/x/heldout/rows.json"):
        with pytest.raises(ev.HeldOutInTuning):
            ev.refuse_heldout_for_tuning("dev", item)
    ev.refuse_heldout_for_tuning("dev", "devtools/validation/corpora/v4/dev.json", "clean")


def test_the_firewall_hook_refuses_when_called_and_passes_dev_and_clean() -> None:
    """No tuning path calls the hook yet; this proves it bites the day one does."""
    with pytest.raises(ev.HeldOutInTuning, match="held-out"):
        ev.refuse_heldout_for_tuning("heldout")
    with pytest.raises(ev.HeldOutInTuning):
        ev.refuse_heldout_for_tuning("devtools/validation/corpora/v4/heldout.json")
    assert ev.refuse_heldout_for_tuning("dev", "clean", "corpora/v4/dev.json") is None


def test_plan_provenance_carries_the_slot_so_a_report_can_read_it_back() -> None:
    m = Markers()
    held = ev.plan_wave(ev.WAVES["heldout"], wave_id="h1", n_shards=4, markers=m)
    assert held.provenance()["slot"] == "slot/held_out/1"
    assert ev.plan_wave(ev.WAVES["dev_search"], wave_id="n", n_shards=4, markers=m).provenance()["slot"] is None


class _RacingMarkers(Markers):
    """A concurrent start of a wave: our `get` saw nothing, then the rival's `create` won the first key."""

    def __init__(self, rival_id):
        super().__init__()
        self.rival_id, self.armed = rival_id, True

    def create(self, k, v):
        if self.armed:
            self.armed = False
            self.d[k] = self.rival_id  # the rival lands between our get and our create
        return super().create(k, v)


def test_a_concurrent_start_of_the_same_wave_does_not_burn_a_second_slot() -> None:
    m = _RacingMarkers("c1")  # the rival IS the same wave_id
    plan = ev.plan_wave(ev.WAVES["dev_checkpoint"], wave_id="c1", n_shards=4, markers=m)
    assert plan.slot == "slot/checkpoint/1" and set(m.d) == {"slot/checkpoint/1"}
    # the sibling: a DIFFERENT wave that loses the race correctly takes the next slot
    m2 = _RacingMarkers("rival")
    assert ev.plan_wave(ev.WAVES["dev_checkpoint"], wave_id="c1", n_shards=4, markers=m2).slot == "slot/checkpoint/2"


@pytest.mark.parametrize("name", ["held-out", "held out", "Held_Out", "HELD-OUT.json", "heldOut", "held.out"])
def test_the_firewall_catches_every_spelling_of_held_out(name) -> None:
    for item in (name, f"devtools/validation/corpora/v4/{name}", f"/data/{name}/dev.json"):
        with pytest.raises(ev.HeldOutInTuning):
            ev.refuse_heldout_for_tuning(item)


def test_the_firewall_reads_a_differently_named_file_by_what_it_says_it_is(tmp_path) -> None:
    import json

    secret = tmp_path / "split_b.json"
    secret.write_text(json.dumps({"split": "heldout", "files": []}))
    with pytest.raises(ev.HeldOutInTuning):
        ev.refuse_heldout_for_tuning(str(secret))
    dev = tmp_path / "split_a.json"
    dev.write_text(json.dumps({"split": "dev", "files": []}))
    junk = tmp_path / "junk.json"
    junk.write_text("not json")
    ev.refuse_heldout_for_tuning(str(dev), str(junk), "clean", "dev", str(tmp_path / "absent.json"))  # no false positive


def test_v3_is_the_one_arm_whose_spend_is_not_metered() -> None:
    assert [a for a, x in ev.ARMS.items() if not x.spend_metered] == ["v3_meshability_pass_ai"]


def test_the_fast_loop_waves_run_the_search_arms_only_on_the_frozen_fast_dev_v1_files() -> None:
    import json
    from pathlib import Path

    root = Path(ev.__file__).resolve().parents[1] / "corpora/v4/fast_dev_v1"
    for name, split, n in (("fast_v1_dev", "dev", 15), ("fast_v1_clean", "clean", 5)):
        w = ev.WAVES[name]
        # LEAD-3 2026-11-24: search_seq (B24) joins the fast waves so the owner's sequential arm is measured
        # beside the two search arms on the same files, same wall, same op cap.
        assert (w.wave_type, w.split, w.arms) == ("normal", split, ("search_det", "search_ai", "search_seq"))
        body = json.loads((root / f"FAST_{split}_v1.json").read_text())
        assert body["split"] == split and len(body["files"]) == n
    dev = {f["sha256"][:12] for f in json.loads((root / "FAST_dev_v1.json").read_text())["files"]}
    assert not {"4c1fc7d5579d", "e63710eae340"} <= dev, "the duplicated part is in FAST-DEV v1 twice"
