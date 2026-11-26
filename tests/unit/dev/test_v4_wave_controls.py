# Responsibility: Prove the v4 launcher supplies what the model-spend ledger needs (wave type, N, unique shard
#                 indexes, a durable log, a marker store) and that each new gate REFUSES: the wave-count limits,
#                 the conditional-create probe gate, and the fail-closed disk-headroom check.
# Boundaries: hermetic. Markers live in a temp directory; the probe's two "hosts" are two local processes over a
#             directory store; gcloud, docker and curl are stubs. Nothing reaches a network or a bucket.
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from meshpipeline.cad.repair_v4 import model_ledger as ml  # noqa: E402

_spec = importlib.util.spec_from_file_location("fleet_launcher_tests_helpers",
                                               Path(__file__).with_name("test_v4_fleet_launcher.py"))
assert _spec is not None and _spec.loader is not None
base = importlib.util.module_from_spec(_spec)
sys.modules["fleet_launcher_tests_helpers"] = base
_spec.loader.exec_module(base)

V4 = base.V4
lf = base.lf
mp = base._load("marker_probe_under_test", V4 / "marker_probe.py")
wl = base._load("wave_limits_under_test", V4 / "wave_limits.py")
ra = base._load("v4_run_arm_wave_under_test", V4 / "run_arm.py")

BUCKET_NAME = "dev-crsv3-transfer-224734058693"
BACKEND = f"gcs/{BUCKET_NAME}"


@pytest.fixture
def live(monkeypatch):
    """subprocess.run replaced by a recorder and the live read-only checks stubbed: nothing is created."""
    rec = base._Recorder()
    monkeypatch.setattr(lf.subprocess, "run", rec)
    monkeypatch.setattr(lf, "live_checks", lambda *a, **k: ([], []))
    monkeypatch.setattr(lf, "done_units", lambda *a: set())
    monkeypatch.setattr(lf, "running_units", lambda *a: (set(), []))
    return rec


def _passed_markers(tmp_path: Path, backend: str = BACKEND, *, age_s: float = 0.0):
    markers = ml.FileMarkerStore(tmp_path / "markers")
    mp.record_pass(markers, backend=backend, run_id="r1", rounds=50, at=time.time() - age_s)
    return markers


# ============================================================ the probe

def _probe(spec: str, tmp_path: Path, rounds: int = 50):
    return mp.run_local(spec, rounds=rounds, interval_s=0.05, lead_s=1.5, run_id="t")


#: A local create takes ~0.35 ms but two processes' clocks and scheduling differ by milliseconds, so on a bare
#: `localdir` the writers' calls essentially never overlap (1 of 50 measured). 15 ms inside every create is well
#: above that jitter, so the writers genuinely race and MIN_OVERLAPPING_ROUNDS is met because they did.
SLOW = "localdir@15"


def test_probe_passes_on_a_backend_that_honours_the_precondition(tmp_path):
    report = _probe(f"{SLOW}:{tmp_path / 'b'}", tmp_path)
    assert report.failures == [] and report.passed
    assert report.rounds == 50 and report.sequential_ok
    assert report.overlapping_rounds >= mp.MIN_OVERLAPPING_ROUNDS     # real races, not a relaxed floor
    markers, backend = mp.open_markers(f"{SLOW}:{tmp_path / 'b'}")
    assert mp.probe_problem(markers, expected_backend=backend) is None   # the record the launcher reads


def test_probe_goes_through_ObjectMarkerStore_not_a_raw_put(tmp_path):
    markers, _ = mp.open_markers(f"localdir:{tmp_path / 'b'}")
    assert isinstance(markers, ml.ObjectMarkerStore)


def test_probe_fails_when_the_backend_ignores_the_precondition(tmp_path):
    """The dangerous mode: both writers get True, no error. The probe must catch it and record no pass."""
    spec = f"localdir-ignores-precondition:{tmp_path / 'b'}"
    report = _probe(spec, tmp_path, rounds=10)
    assert not report.passed
    assert any("sequential" in f for f in report.failures)      # second create returned True
    assert any("2 writers got True" in f or "both" in f for f in report.failures)
    markers, backend = mp.open_markers(spec)
    assert mp.probe_problem(markers, expected_backend=backend) is not None


def test_probe_fails_when_the_loser_is_told_False_but_silently_overwrites(tmp_path):
    """Returns the right booleans, but the stored value is the LAST writer's: only the read-back sees it."""
    spec = f"localdir-overwrites-silently:{tmp_path / 'b'}"
    report = _probe(spec, tmp_path, rounds=10)
    assert not report.passed
    assert any("holds" in f or "overwr" in f for f in report.failures)
    markers, backend = mp.open_markers(spec)
    assert mp.probe_problem(markers, expected_backend=backend) is not None


def test_probe_fails_when_nobody_wins(tmp_path):
    report = _probe(f"localdir-never-creates:{tmp_path / 'b'}", tmp_path, rounds=5)
    assert not report.passed and any("0 writers got True" in f or "no writer" in f for f in report.failures)


def test_probe_cli_exit_codes(tmp_path):
    def run(spec: str) -> int:
        return subprocess.run([sys.executable, str(V4 / "marker_probe.py"), "--store", spec, "--rounds", "50",
                               "--interval-s", "0.05", "--lead-s", "1.5"], capture_output=True,
                              text=True, timeout=120).returncode
    assert run(f"{SLOW}:{tmp_path / 'ok'}") == 0
    assert run(f"localdir-ignores-precondition:{tmp_path / 'bad'}") == 1


def _write_results(markers, run_id: str, rounds: int, *, overlap: bool) -> None:
    """Both writers' result records, as the real writers leave them: exactly one winner per round, the object
    holding the winner's payload. `overlap` False puts B's call strictly after A's (the writers never raced)."""
    for w, offset in (("A", 0.0), ("B", 0.0 if overlap else 1.0)):
        entries = []
        for r in range(rounds):
            payload = f"{w}|{run_id}|{r}|x"
            entries.append({"round": r, "payload": payload, "created": w == "A", "t0": r + offset,
                            "t1": r + offset + 0.5, "late_s": 0.0})
        markers.put(f"_probe/{run_id}/result-{w}", json.dumps({"writer": w, "rounds": entries}))
    for r in range(rounds):
        markers.put(f"_probe/{run_id}/race-{r}", f"A|{run_id}|{r}|x")


def test_a_probe_where_the_writers_never_overlapped_does_not_pass(tmp_path):
    """A non-atomic check-then-write passes a run in which the writers never raced; so no overlap, no pass."""
    markers = ml.FileMarkerStore(tmp_path)
    _write_results(markers, "r", 20, overlap=False)
    report = mp.conclude(markers, "gcs/b", "r", 20)
    assert not report.passed and report.overlapping_rounds == 0
    assert any("overlapping" in f for f in report.failures)
    assert json.loads(markers.get(mp.PROBE_KEY))["passed"] is False     # no pass record, a revoking one
    assert "0 with overlapping" in report.describe()                       # the count is printed
    assert mp.probe_problem(markers, expected_backend="gcs/b") is not None  # and a failure record revokes


def test_the_latency_suffix_makes_every_create_take_that_long(tmp_path):
    """The slow store's whole point: each create lasts at least the stated latency (deterministic, unlike the
    overlap it buys, which a fast native host can reach without it)."""
    markers, _ = mp.open_markers(f"localdir@40:{tmp_path / 'b'}")
    t0 = time.perf_counter()
    markers.create("k", "v")
    assert time.perf_counter() - t0 >= 0.04
    fast, _ = mp.open_markers(f"localdir:{tmp_path / 'c'}")
    t0 = time.perf_counter()
    fast.create("k", "v")
    assert time.perf_counter() - t0 < 0.04


def test_a_real_run_that_cannot_meet_the_floor_is_refused_and_revokes_the_record(tmp_path):
    """End to end over a slow store that really races: demand more overlapping rounds than exist and the verdict
    is a refusal, with a passed:false record (not merely a missing one)."""
    spec = f"{SLOW}:{tmp_path / 'b'}"
    assert _probe(spec, tmp_path, rounds=20).passed
    markers, backend = mp.open_markers(spec)
    report = mp.conclude(markers, backend, "t", 20, min_overlap=21)
    assert not report.passed and any("fewer than the 21 required" in f for f in report.failures)
    assert json.loads(markers.get(mp.PROBE_KEY))["passed"] is False
    assert mp.probe_problem(markers, expected_backend=backend) is not None


def test_a_probe_with_enough_overlap_passes_and_one_below_the_minimum_does_not(tmp_path):
    ok = ml.FileMarkerStore(tmp_path / "ok")
    _write_results(ok, "r", mp.MIN_OVERLAPPING_ROUNDS, overlap=True)
    assert mp.conclude(ok, "gcs/b", "r", mp.MIN_OVERLAPPING_ROUNDS).passed
    short = ml.FileMarkerStore(tmp_path / "short")
    _write_results(short, "r", mp.MIN_OVERLAPPING_ROUNDS - 1, overlap=True)
    assert not mp.conclude(short, "gcs/b", "r", mp.MIN_OVERLAPPING_ROUNDS - 1).passed


def test_the_pass_record_is_not_written_until_the_caller_says_so(tmp_path):
    markers = ml.FileMarkerStore(tmp_path)
    _write_results(markers, "r", 20, overlap=True)
    report = mp.conclude(markers, "gcs/b", "r", 20, write_pass=False)
    assert report.passed and markers.get(mp.PROBE_KEY) is None      # no pass record yet


def test_a_writer_that_exits_nonzero_leaves_no_pass_record(tmp_path, monkeypatch):
    spec = f"localdir:{tmp_path / 'b'}"
    real = mp.subprocess.Popen

    class Failing:
        def __init__(self, cmd, **kw):
            self._p = real(cmd, **kw)
            self._fail = "B" in cmd[cmd.index("--writer") + 1]

        def communicate(self, *a, **k):
            out = self._p.communicate(*a, **k)
            return out

        @property
        def returncode(self):
            return 7 if self._fail else self._p.returncode
    monkeypatch.setattr(mp.subprocess, "Popen", Failing)
    seen: list[object] = []
    real_record = mp.record_pass

    def spy(markers, **kw):
        seen.append(kw["passed"])
        real_record(markers, **kw)
    monkeypatch.setattr(mp, "record_pass", spy)
    report = _probe(spec, tmp_path)
    assert not report.passed and any("exited 7" in f for f in report.failures)
    assert seen and True not in seen     # a pass record was never written, even transiently (the count of
                                         # False rewrites differs between host and container)


def test_probe_gate_refuses_every_way_the_record_can_be_wrong(tmp_path):
    empty = ml.FileMarkerStore(tmp_path / "empty")
    assert "no probe record" in (mp.probe_problem(empty, expected_backend=BACKEND) or "")
    assert mp.probe_problem(_passed_markers(tmp_path), expected_backend=BACKEND) is None
    other = _passed_markers(tmp_path / "o", backend="localhost:9000/mesh-artifacts")
    assert "different backend" in (mp.probe_problem(other, expected_backend=BACKEND) or "")
    old = _passed_markers(tmp_path / "old", age_s=mp.PROBE_MAX_AGE_S + 60)
    assert "older than" in (mp.probe_problem(old, expected_backend=BACKEND) or "")
    failed = ml.FileMarkerStore(tmp_path / "failed")
    failed.put(mp.PROBE_KEY, json.dumps({"passed": False, "backend": BACKEND, "version": mp.PROBE_VERSION,
                                         "at": time.time()}))
    assert "did not pass" in (mp.probe_problem(failed, expected_backend=BACKEND) or "")
    stale = ml.FileMarkerStore(tmp_path / "stale")
    stale.put(mp.PROBE_KEY, json.dumps({"passed": True, "backend": BACKEND, "version": mp.PROBE_VERSION + 1,
                                        "at": time.time()}))
    assert "version" in (mp.probe_problem(stale, expected_backend=BACKEND) or "")
    junk = ml.FileMarkerStore(tmp_path / "junk")
    junk.put(mp.PROBE_KEY, "not json")
    assert mp.probe_problem(junk, expected_backend=BACKEND) is not None


# --------------------------------------------- what a create costs, and the probe's own litter (task 55)

def test_the_probe_measures_create_latency_and_never_lets_it_decide_the_verdict(tmp_path):
    """Latency is a RESULT the wave needs (every shard pays two creates before it may spend) but not a pass
    criterion: a store made 40 ms slow per create still passes, and says so in milliseconds."""
    spec = f"localdir@40:{tmp_path / 'b'}"
    report = mp.run_local(spec, rounds=20, interval_s=0.05, lead_s=1.5, run_id="t", latency_samples=5)
    assert report.passed
    lat = report.latency["uncontended"]
    assert lat["n"] == 5 and lat["median_ms"] >= 40.0 and lat["max_ms"] >= lat["median_ms"] >= lat["min_ms"]
    assert report.latency["racing"]["n"] == 40      # 20 rounds x 2 writers, timed by the writers themselves
    assert "create latency (uncontended, n=5): median" in report.describe()
    fast = mp.run_local(f"localdir:{tmp_path / 'c'}", rounds=20, interval_s=0.05, lead_s=1.5, run_id="t2",
                        latency_samples=5)
    assert fast.latency["uncontended"]["median_ms"] < lat["median_ms"]   # measured, not asserted


def test_latency_samples_zero_skips_the_measurement_without_failing(tmp_path):
    report = mp.run_local(f"localdir@40:{tmp_path / 'b'}", rounds=20, interval_s=0.05, lead_s=1.5, run_id="t",
                          latency_samples=0)
    assert report.passed and report.latency["uncontended"] == {"n": 0}
    assert "create latency (uncontended" not in report.describe()


def test_cleanup_removes_this_runs_probe_keys_and_never_the_pass_record(tmp_path):
    """The probe writes throwaway keys to the REAL wave bucket, so it has to take them away - but the pass
    record is the whole point of the run and must survive."""
    root = tmp_path / "b"
    report = mp.run_local(f"localdir@40:{root}", rounds=20, interval_s=0.05, lead_s=1.5, run_id="t",
                          latency_samples=4, clean=True)
    assert report.passed and report.cleaned["problem"] is None
    assert report.cleaned["deleted"] == report.cleaned["attempted"] == 20 + 1 + 2 + 4
    assert not report.cleaned["failed"]
    left = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert left == [f"model-ledger/{mp.PROBE_KEY}"]
    markers, backend = mp.open_markers(f"localdir@40:{root}")
    assert mp.probe_problem(markers, expected_backend=backend) is None   # still proven after the sweep


def test_cleanup_reports_a_store_that_cannot_delete_instead_of_failing(tmp_path):
    class NoDelete(mp.LocalDirStore):
        delete_object = None

    markers = ml.ObjectMarkerStore(NoDelete(tmp_path / "b"))
    markers.create("_probe/t/seq", "v")
    out = mp.cleanup(markers, "t", 2, 0)
    assert out["deleted"] == 0 and "no delete_object" in out["problem"]
    assert markers.get("_probe/t/seq") == "v"


def test_the_marker_store_exposes_its_object_store_and_keys_for_the_probe(tmp_path):
    markers = ml.ObjectMarkerStore(mp.LocalDirStore(tmp_path), prefix="model-ledger")
    assert markers.object_key("_probe/x") == "model-ledger/_probe/x"
    assert isinstance(markers.store, mp.LocalDirStore)
    # the ledger's own interface still has no delete: a marker it depends on must not be removable through it
    assert not hasattr(ml.MarkerStore, "delete")

# ============================================================ wave-count limits

def test_a_fourth_checkpoint_wave_is_refused_and_a_resume_is_not(tmp_path):
    m = ml.FileMarkerStore(tmp_path)
    for n in range(3):
        assert wl.check_wave_count(m, "checkpoint", f"cp-{n}", claim=True) is None
    refusal = wl.check_wave_count(m, "checkpoint", "cp-3", claim=True)
    assert refusal and "at most 3 checkpoint" in refusal
    assert wl.check_wave_count(m, "checkpoint", "cp-1", claim=True) is None     # relaunch of wave 2
    assert wl.check_wave_count(m, "checkpoint", "cp-3", claim=False) is not None


def test_a_second_held_out_wave_is_refused_and_a_resume_is_not(tmp_path):
    m = ml.FileMarkerStore(tmp_path)
    assert wl.check_wave_count(m, "held_out", "ho-1", claim=True) is None
    assert "exactly one held-out" in (wl.check_wave_count(m, "held_out", "ho-2", claim=True) or "")
    assert wl.check_wave_count(m, "held_out", "ho-1", claim=True) is None


def test_a_dry_check_consumes_nothing(tmp_path):
    m = ml.FileMarkerStore(tmp_path)
    for n in range(10):
        assert wl.check_wave_count(m, "held_out", f"ho-{n}", claim=False) is None
        assert wl.check_wave_count(m, "checkpoint", f"cp-{n}", claim=False) is None
    assert wl.check_wave_count(m, "held_out", "ho-x", claim=True) is None


def test_normal_waves_are_unlimited_and_unknown_types_refused(tmp_path):
    m = ml.FileMarkerStore(tmp_path)
    for n in range(8):
        assert wl.check_wave_count(m, "normal", f"n-{n}", claim=True) is None
    assert wl.check_wave_count(m, "bogus", "x", claim=True) is not None


@pytest.mark.parametrize("wave,slots", [("checkpoint", 3), ("held_out", 1)])
def test_concurrent_launchers_cannot_over_claim(tmp_path, wave, slots):
    m = ml.FileMarkerStore(tmp_path)
    got: list[str | None] = []
    gate = threading.Barrier(8)

    def go(i: int) -> None:
        gate.wait()
        got.append(wl.check_wave_count(m, wave, f"w-{i}", claim=True))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r is None for r in got) == slots


def test_shard_indexes_are_unique_in_range_and_agree_on_N():
    assert wl.shard_assignments([f"reprobe:pool:{i}/4" for i in (1, 2, 3, 4)], 4) == [0, 1, 2, 3]
    with pytest.raises(wl.WaveRefused, match="twice"):
        wl.shard_assignments(["reprobe:pool:1/4", "reprobe:pool:1/4"], 4)
    with pytest.raises(wl.WaveRefused, match="outside"):
        wl.shard_assignments(["reprobe:pool:5/4"], 4)
    with pytest.raises(wl.WaveRefused, match="shards-total"):
        wl.shard_assignments(["reprobe:pool:1/3"], 4)
    with pytest.raises(wl.WaveRefused):
        wl.shard_assignments(["garbage"], 4)


def test_the_cap_comes_from_the_ledger_table_not_a_typed_number():
    assert wl.WAVE_CAPS_USD is ml.WAVE_CAPS_USD or wl.WAVE_CAPS_USD == ml.WAVE_CAPS_USD
    assert wl.cap_per_shard("checkpoint", 96) == pytest.approx(300.0 / 96)


# ============================================================ the launcher

WAVE = ("--wave-type", "checkpoint", "--wave-id", "cp-one")


class _Boom:
    """A gates stand-in that fails the test if anything touches it."""
    def __getattr__(self, name):
        raise AssertionError(f"the marker store was touched ({name})")


def _gates(tmp_path: Path, **kw):
    return (_passed_markers(tmp_path, **kw), BACKEND)


def _live(live, tmp_path, *argv, gates=None, wave=WAVE):
    return lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD,
                    "--skip-preflight", "--profile", "v4", *wave, *argv],
                   gates=gates if gates is not None else _gates(tmp_path))


def test_a_v4_wave_without_a_wave_type_does_not_launch(tmp_path, capsys):
    r = base._launch("--profile", "v4", "--image", base.IMAGE, "--commit", base.HEAD, "--image-commit",
                     base.HEAD, "--skip-preflight", "--dry-run", "--wave-id", "w-1", tmp_path=tmp_path)
    assert r.returncode == 2 and "--wave-type" in r.stderr and not base._creates(r.stdout)
    r = base._launch("--profile", "v4", "--image", base.IMAGE, "--commit", base.HEAD, "--image-commit",
                     base.HEAD, "--skip-preflight", "--dry-run", "--wave-type", "normal", tmp_path=tmp_path)
    assert r.returncode == 2 and "--wave-id" in r.stderr


def test_an_unnamed_wave_type_is_refused(tmp_path):
    r = base._v4("--dry-run", "--wave-type", "huge", "--wave-id", "w-1", tmp_path=tmp_path)
    assert r.returncode == 2 and "huge" in r.stderr


def test_v3_refuses_the_wave_flags(tmp_path):
    r = base._launch("--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD, "--dry-run",
                     "--skip-preflight", "--wave-type", "normal", tmp_path=tmp_path)
    assert r.returncode == 2 and "v4 only" in r.stderr


def test_the_wave_reaches_every_vm_as_metadata_and_the_cap_comes_from_the_table(tmp_path):
    r = base._v4("--dry-run", "--wave-type", "checkpoint", "--wave-id", "cp-one", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    creates = base._creates(r.stdout)
    assert len(creates) == 12
    for line in creates:
        for want in ("v4-wave-id=cp-one", "v4-wave-type=checkpoint", "v4-n-shards=96",
                     f"v4-marker-bucket={BUCKET_NAME}"):
            assert want in line, want
    assert "$300.00" in r.stdout and "96" in r.stdout          # the table's cap, split over N


def _dry(tmp_path, *, gates, wave=WAVE):
    return lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD,
                    "--skip-preflight", "--profile", "v4", *wave, "--dry-run"], gates=gates)


def test_a_dry_run_reads_the_gates_and_passes_when_they_pass(live, tmp_path):
    assert _dry(tmp_path, gates=_gates(tmp_path)) == 0 and live.creates() == []


def test_a_dry_run_refuses_a_bad_probe_and_claims_nothing(live, tmp_path, capsys):
    gates = (ml.FileMarkerStore(tmp_path / "none"), BACKEND)
    assert _dry(tmp_path, gates=gates) == 1
    assert "no probe record" in capsys.readouterr().err
    assert gates[0].get("slot/checkpoint/1") is None               # read-only: nothing claimed


def test_a_dry_run_refuses_an_exhausted_wave_count(live, tmp_path, capsys):
    gates = _gates(tmp_path)
    for n in range(3):
        assert wl.check_wave_count(gates[0], "checkpoint", f"earlier-{n}", claim=True) is None
    assert _dry(tmp_path, gates=gates) == 1
    assert "at most 3 checkpoint" in capsys.readouterr().err
    assert wl.check_wave_count(gates[0], "checkpoint", "another", claim=False) is not None   # still 3, not 4


@pytest.mark.parametrize("make_exc", [lambda: OSError("no route"),
                                      lambda: __import__("google.auth.exceptions", fromlist=["x"])
                                      .DefaultCredentialsError("no ADC"),
                                      lambda: __import__("google.api_core.exceptions", fromlist=["x"])
                                      .ServiceUnavailable("503")])
def test_a_dry_run_warns_instead_of_skipping_when_the_store_is_unreachable(live, tmp_path, monkeypatch, capsys,
                                                                           make_exc):
    def boom():
        raise make_exc()
    monkeypatch.setattr(lf, "default_gates", boom)
    assert lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD,
                    "--skip-preflight", "--profile", "v4", *WAVE, "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "WARN" in err and "UNCHECKED" in err


@pytest.mark.parametrize("bug", [TypeError("bad arg"), KeyError("missing"), RuntimeError("no ADC")])
def test_a_bug_in_the_wave_count_check_fails_a_dry_run(live, tmp_path, monkeypatch, capsys, bug):
    """A dry run only downgrades REACHABILITY failures; a bug inside check_wave_count must not exit 0."""
    def broken(*a, **k):
        raise bug
    monkeypatch.setattr(lf.wave_limits, "check_wave_count", broken)
    assert _dry(tmp_path, gates=_gates(tmp_path)) == 1
    assert "UNCHECKED" not in capsys.readouterr().err


def test_a_dry_run_of_v3_never_touches_the_marker_store(live, tmp_path):
    assert lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD,
                    "--skip-preflight", "--dry-run"], gates=_Boom()) == 0


def test_a_live_v4_wave_with_a_passed_probe_launches(live, tmp_path):
    assert _live(live, tmp_path, "--max-run-hours", "8") == 0
    creates = live.creates()
    assert len(creates) == 12
    assert all("v4-wave-type=checkpoint" in "|".join(c) for c in creates)


def test_a_live_v4_wave_without_a_probe_creates_no_vm(live, tmp_path, capsys):
    empty = (ml.FileMarkerStore(tmp_path / "none"), BACKEND)
    assert _live(live, tmp_path, gates=empty) == 1
    assert live.creates() == [] and "no probe record" in capsys.readouterr().err


def test_a_probe_for_another_backend_does_not_unlock_this_bucket(live, tmp_path, capsys):
    gates = (_passed_markers(tmp_path, backend="localdir:/tmp/x"), BACKEND)
    assert _live(live, tmp_path, gates=gates) == 1 and live.creates() == []
    assert "different backend" in capsys.readouterr().err


def test_a_failed_probe_record_blocks_the_launch(live, tmp_path):
    markers = ml.FileMarkerStore(tmp_path / "m")
    markers.put(mp.PROBE_KEY, json.dumps({"passed": False, "backend": BACKEND, "version": mp.PROBE_VERSION,
                                          "at": time.time()}))
    assert _live(live, tmp_path, gates=(markers, BACKEND)) == 1 and live.creates() == []


def test_an_unreachable_marker_store_blocks_the_launch(live, tmp_path, monkeypatch, capsys):
    def boom():
        raise RuntimeError("no MINIO_ENDPOINT")
    monkeypatch.setattr(lf, "default_gates", boom)
    rc = lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD,
                  "--skip-preflight", "--profile", "v4", *WAVE])
    assert rc == 1 and live.creates() == [] and "marker store" in capsys.readouterr().err


def test_a_fourth_checkpoint_wave_does_not_launch(live, tmp_path, capsys):
    gates = _gates(tmp_path)
    for n in range(3):
        assert wl.check_wave_count(gates[0], "checkpoint", f"earlier-{n}", claim=True) is None
    assert _live(live, tmp_path, gates=gates) == 1
    assert live.creates() == [] and "at most 3 checkpoint" in capsys.readouterr().err


def test_a_second_held_out_wave_does_not_launch(live, tmp_path, capsys):
    gates = _gates(tmp_path)
    assert _live(live, tmp_path, gates=gates, wave=("--wave-type", "held_out", "--wave-id", "ho-1")) == 0
    live.calls.clear()
    assert _live(live, tmp_path, gates=gates, wave=("--wave-type", "held_out", "--wave-id", "ho-2")) == 1
    assert live.creates() == [] and "exactly one held-out" in capsys.readouterr().err
    # ... while a relaunch of the SAME held-out wave (a resume) is not a second wave
    assert _live(live, tmp_path, gates=gates, wave=("--wave-type", "held_out", "--wave-id", "ho-1")) == 0


def test_a_refused_launch_does_not_burn_a_checkpoint_slot(live, tmp_path):
    gates = (ml.FileMarkerStore(tmp_path / "none"), BACKEND)            # no probe: refused
    assert _live(live, tmp_path, gates=gates) == 1
    assert wl.check_wave_count(gates[0], "checkpoint", "other", claim=False) is None
    assert gates[0].get("slot/checkpoint/1") is None


def test_create_wave_itself_refuses_when_the_gate_refuses(live):
    ev = lf.budget_spec
    ok = ev.Projection(files=1, full_files=0, shards=1, vms=1, vcpus_per_vm=8, cpu_hours=1.0, wall_hours=1.0,
                       vm_hours=1.0, max_run_hours=3, worst_vm_hours=3.0)

    def refuse():
        raise wl.WaveRefused("no probe")
    with pytest.raises(wl.WaveRefused):
        lf.create_wave([["gcloud", "compute", "instances", "create", "x"]], ok, gate=refuse, profile=lf.V4)
    assert live.creates() == []


def test_create_wave_refuses_a_v4_wave_with_no_gate_and_any_wave_with_no_profile(live):
    ev = lf.budget_spec
    ok = ev.Projection(files=1, full_files=0, shards=1, vms=1, vcpus_per_vm=8, cpu_hours=1.0, wall_hours=1.0,
                       vm_hours=1.0, max_run_hours=3, worst_vm_hours=3.0)
    cmd = [["gcloud", "compute", "instances", "create", "x"]]
    with pytest.raises(lf.wave_limits.WaveRefused, match="gate"):
        lf.create_wave(cmd, ok, profile=lf.V4)
    with pytest.raises(TypeError):
        lf.create_wave(cmd, ok)
    assert live.creates() == []
    lf.create_wave(cmd, ok, profile=lf.V3)        # v3 has no probe gate and still launches
    assert len(live.creates()) == 1


def test_v3_never_touches_the_gates(live, tmp_path):
    rc = lf.main(["--image", base.IMAGE, "--commit", base.HEAD, "--image-commit", base.HEAD,
                  "--skip-preflight"], gates=_Boom())
    assert rc == 0 and len(live.creates()) == 12


# ============================================================ run_arm builds the ledger

def _ra_args(tmp_path: Path) -> list[str]:
    return ["--shard", "1/2", "--wave-type", "checkpoint", "--wave-id", "cp-one", "--n-shards", "2",
            "--ledger-path", str(tmp_path / "ledger" / "log.jsonl"), "--out", str(tmp_path / "out"),
            "--marker-bucket", BUCKET_NAME, "--cache-uri", "gs://B/eval/v4-reprobe/verdict-cache"]


def test_run_arm_builds_this_shards_slice_with_its_index_and_cap(tmp_path):
    markers = ml.FileMarkerStore(tmp_path / "m")
    args = ra.parse_args(_ra_args(tmp_path))
    ledger = ra.build_ledger(args, markers)
    assert ledger.provenance == "shard" and ledger.cap_usd == pytest.approx(300.0 / 2)
    assert markers.get("cp-one/shard-0") is not None          # shard 1/2 is index 0
    ledger2 = ra.build_ledger(ra.parse_args(_ra_args(tmp_path)), markers)   # a restart of the same shard
    assert ledger2.spent_usd() == 0.0


def test_run_arm_refuses_a_shard_whose_log_vanished(tmp_path):
    markers = ml.FileMarkerStore(tmp_path / "m")
    args = ra.parse_args(_ra_args(tmp_path))
    ra.build_ledger(args, markers)
    Path(args.ledger_path).unlink()
    with pytest.raises(ml.LostShardLog):
        ra.build_ledger(args, markers)


def test_run_arm_refuses_the_same_index_on_a_second_host(tmp_path):
    markers = ml.FileMarkerStore(tmp_path / "m")
    ra.build_ledger(ra.parse_args(_ra_args(tmp_path)), markers)
    other = ra.parse_args(_ra_args(tmp_path)[:-6] + ["--ledger-path", str(tmp_path / "host2" / "log.jsonl"),
                                                     "--out", str(tmp_path / "out2"),
                                                     "--marker-bucket", BUCKET_NAME])
    with pytest.raises((ml.LostShardLog, ml.ShardArithmeticError)):
        ra.build_ledger(other, markers)


def test_run_arm_rejects_an_N_that_disagrees_with_its_shard(tmp_path):
    argv = _ra_args(tmp_path)
    argv[argv.index("--n-shards") + 1] = "3"
    with pytest.raises(SystemExit):
        ra.parse_args(argv)


def test_run_arm_rejects_a_half_specified_wave(tmp_path):
    with pytest.raises(SystemExit):
        ra.parse_args(["--shard", "1/2", "--wave-type", "normal"])


def test_a_lost_log_stops_the_shard_and_says_why(tmp_path, monkeypatch):
    markers = _passed_markers(tmp_path)
    monkeypatch.setattr(ra, "make_marker_store", lambda bucket: (markers, f"gcs/{bucket}"))
    monkeypatch.setattr(ra, "run_reprobe", lambda *a, **k: 0)
    argv = _ra_args(tmp_path)
    assert ra.main(argv) == 0
    Path(argv[argv.index("--ledger-path") + 1]).unlink()
    assert ra.main(argv) == 5
    prov = json.loads((tmp_path / "out" / "PROVENANCE.json").read_text())
    assert "LostShardLog" in prov["error"]


# ============================================================ vm_startup.sh

V4_META = {"v4-image": "IMG@sha256:x", "v4-commit": "c" * 40, "v4-image-commit": "abc1234",
           "v4-units": "reprobe:pool:1/2", "v4-bucket": "gs://B", "v4-prefix": "gs://B/eval/v4-reprobe",
           "v4-code": "gs://B/eval/v4-reprobe/code-cccccccccccc/code.tgz",
           "v4-machine-type": "c2d-standard-8", "v4-run-arm": "devtools/validation/v4_eval/run_arm.py",
           "v4-pool": "gs://B/eval/v4-reprobe/pool.json", "v4-cache-uri": "gs://B/eval/v4-reprobe/verdict-cache",
           "v4-sources": "abc_fail", "v4-wave-id": "cp-one", "v4-wave-type": "checkpoint",
           "v4-n-shards": "2", "v4-marker-bucket": "B"}


def _runs(log: list[str]) -> list[str]:
    return [ln for ln in log if ln.startswith("docker run")]


def test_startup_threads_the_ledger_inputs_into_run_arm(tmp_path):
    log = base._startup(tmp_path, V4_META)
    (run,) = _runs(log)
    assert "--wave-type checkpoint --wave-id cp-one --n-shards 2" in run
    assert "--ledger-path /eval/ledger/log.jsonl" in run
    assert "-v ROOT/ledger/reprobe_pool_1_2:/eval/ledger" in run     # the boot disk, not the container's
    assert '--marker-bucket B' in run
    assert "marker.env" not in run and not any("secrets" in ln for ln in log)   # no marker credentials at all


@pytest.mark.parametrize("missing", ["v4-wave-id", "v4-wave-type", "v4-n-shards", "v4-marker-bucket"])
def test_startup_refuses_a_v4_wave_without_its_ledger_inputs(tmp_path, missing):
    meta = {k: v for k, v in V4_META.items() if k != missing}
    log = base._startup(tmp_path, meta, expect_rc=1)
    assert _runs(log) == [] and any("FAILED" in ln for ln in log)


def test_the_shard_log_is_mirrored_to_the_bucket_after_the_run(tmp_path):
    log = base._startup(tmp_path, V4_META)
    assert any(ln.startswith("storage cp ROOT/ledger/reprobe_pool_1_2/log.jsonl "
                             "gs://B/eval/reprobe-cccccccccccc/pool/shard-1-of-2/ledger/log.jsonl") for ln in log)


def test_a_replacement_vm_restores_the_mirrored_log_before_starting(tmp_path):
    log = base._startup(tmp_path, V4_META, extra_env={"LS_OK": "/ledger/log.jsonl"})
    assert any(ln == "storage cp gs://B/eval/reprobe-cccccccccccc/pool/shard-1-of-2/ledger/log.jsonl "
                     "ROOT/ledger/reprobe_pool_1_2/log.jsonl" for ln in log)


# ---- the disk-headroom check fails CLOSED

def test_disk_check_passes_with_realistic_du_output_and_room(tmp_path):
    log = base._startup(tmp_path, V4_META,
                        extra_env={"DU_OUT": "5000000000 gs://B/eval/v4-reprobe/inputs\n",
                                   "DF_OUT": "Avail\n100000000000"})
    assert len(_runs(log)) == 1


@pytest.mark.parametrize("env,why", [
    ({"DU_OUT": ""}, "du printed nothing"),
    ({"DU_OUT": "ERROR: (gcloud.storage.du) not found\n"}, "du printed an error"),
    ({"DU_OUT": "", "DU_RC": "1"}, "du failed"),
    ({"DU_OUT": "12.5GiB gs://B/inputs\n"}, "du non-numeric"),
    ({"DF_OUT": "Avail\n"}, "df printed no value"),
    ({"DF_OUT": ""}, "df printed nothing"),
    ({"DF_OUT": "Avail\nlots"}, "df non-numeric"),
    ({"DU_OUT": "90000000000 gs://B/inputs\n", "DF_OUT": "Avail\n100000000000"}, "does not fit in 80%"),
])
def test_disk_check_refuses_instead_of_skipping(tmp_path, env, why):
    log = base._startup(tmp_path, V4_META, expect_rc=1, extra_env=env)
    assert _runs(log) == [], why
    assert not any("pool inputs" in ln or "inputs/*" in ln for ln in log), f"copied inputs despite: {why}"
    assert any("FAILED" in ln for ln in log), why


# ============================================================ the native GCS marker store

class _PreconditionFailed(Exception):
    pass


class _FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self.bucket.calls.append((self.name, if_generation_match))
        if self.bucket.fail is not None:
            raise self.bucket.fail
        if if_generation_match == 0 and self.name in self.bucket.objects:
            raise self.bucket.precondition_failed("412 object exists")
        self.bucket.objects[self.name] = data


class _FakeBucket:
    def __init__(self, precondition_failed):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, object]] = []
        self.fail: Exception | None = None
        self.precondition_failed = precondition_failed

    def blob(self, name):
        return _FakeBlob(self, name)


@pytest.fixture
def gcs():
    from google.api_core.exceptions import PreconditionFailed
    bucket = _FakeBucket(PreconditionFailed)
    return ml.GcsObjectStore("wave-bucket", bucket=bucket), bucket


def test_gcs_store_creates_with_a_generation_zero_precondition_and_412_is_false(gcs):
    store, bucket = gcs
    markers = ml.ObjectMarkerStore(store)
    assert markers.create("k", "first") is True
    assert markers.create("k", "second") is False
    assert markers.get("k") == "first"                       # the loser did not overwrite
    creates = [c for c in bucket.calls if c[0] == "model-ledger/k"]
    assert creates == [("model-ledger/k", 0), ("model-ledger/k", 0)]     # BOTH went out conditional


def test_gcs_store_does_not_report_a_win_it_cannot_prove(gcs):
    store, bucket = gcs
    bucket.fail = RuntimeError("503 backend error")
    with pytest.raises(RuntimeError):
        ml.ObjectMarkerStore(store).create("k", "v")         # an unknown outcome is never a win


def test_gcs_put_is_unconditional_and_the_backend_id_names_the_bucket(gcs):
    store, bucket = gcs
    ml.ObjectMarkerStore(store).put("k", "v1")
    ml.ObjectMarkerStore(store).put("k", "v2")
    assert [c[1] for c in bucket.calls] == [None, None] and bucket.objects["model-ledger/k"] == b"v2"
    assert store.backend_id == "gcs/wave-bucket"


def test_open_markers_gcs_spec_gives_the_gcs_backend_id_and_minio_still_opens(monkeypatch):
    markers, backend = mp.open_markers("gcs:wave-bucket")
    assert backend == "gcs/wave-bucket" and isinstance(markers, ml.ObjectMarkerStore)
    with pytest.raises(ValueError):
        mp.open_markers("gcs:")


def test_the_launcher_expects_the_backend_the_shards_are_told():
    assert lf.MARKER_BACKEND == BACKEND and lf.MARKER_BUCKET == BUCKET_NAME


# ============================================================ the shard checks it is on the proven backend

def _main_with(tmp_path, monkeypatch, markers, backend, bucket=BUCKET_NAME):
    monkeypatch.setattr(ra, "make_marker_store", lambda b: (markers, backend))
    monkeypatch.setattr(ra, "run_reprobe", lambda *a, **k: 0)
    argv = _ra_args(tmp_path)
    argv[argv.index("--marker-bucket") + 1] = bucket
    rc = ra.main(argv)
    return rc, json.loads((tmp_path / "out" / "PROVENANCE.json").read_text())


def test_a_shard_on_the_proven_bucket_runs_and_reports_its_backend(tmp_path, monkeypatch):
    rc, prov = _main_with(tmp_path, monkeypatch, _passed_markers(tmp_path), BACKEND)
    assert rc == 0 and prov["marker_backend"] == BACKEND


def test_a_shard_whose_marker_backend_differs_from_the_launchers_refuses(tmp_path, monkeypatch):
    """The secret-pointing-elsewhere failure: the store is not the bucket the launcher named."""
    rc, prov = _main_with(tmp_path, monkeypatch, _passed_markers(tmp_path), "gcs/some-other-bucket")
    assert rc == 5 and "MarkerBackendMismatch" in prov["error"]
    assert not (tmp_path / "markers" / "cp-one").exists()           # it wrote no shard marker


def test_a_shard_that_cannot_read_the_probe_record_refuses(tmp_path, monkeypatch):
    empty = ml.FileMarkerStore(tmp_path / "empty")                  # same bucket name, but no probe record
    rc, prov = _main_with(tmp_path, monkeypatch, empty, BACKEND)
    assert rc == 5 and "no probe record" in prov["error"]


def test_run_arm_reads_the_wave_types_from_the_table_not_a_literal():
    src = (V4 / "run_arm.py").read_text()
    assert "WAVE_CAPS_USD" in src and '"held_out")' not in src.split("--wave-type")[1].split("help=")[0]


def test_store_unreachable_errors_narrows_instead_of_raising_when_google_libs_are_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", None)  # an import of it now raises ImportError
    assert lf.store_unreachable_errors() == (OSError,)
