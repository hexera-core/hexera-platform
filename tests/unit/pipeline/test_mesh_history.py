# Responsibility: Verify mesh-time history reports a measured distribution or nothing, never a fabricated number.
from __future__ import annotations

from pathlib import Path

import pytest

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

import meshpipeline.engines.mesh_history as mh  # noqa: E402
from meshpipeline.contracts import mesh_timing  # noqa: E402


class FakeMeshTimingStore:

    def __init__(self):
        self.samples: dict = {}

    def append_sample(self, engine, purpose, seconds, cap):
        k = (engine, purpose)
        self.samples.setdefault(k, []).insert(0, round(float(seconds), 1))
        self.samples[k] = self.samples[k][:cap]

    def read_samples(self, engine, purpose):
        return list(self.samples.get((engine, purpose), []))


@pytest.fixture(autouse=True)
def _no_store_leak():
    yield
    mesh_timing.set_mesh_timing_store(None)


def _fake(monkeypatch):
    store = FakeMeshTimingStore()
    mesh_timing.set_mesh_timing_store(store)
    return store


def test_no_history_returns_none_not_a_fabricated_number(monkeypatch):
    _fake(monkeypatch)
    assert mh.estimate("snappy", "external_cfd") is None
    mh.record("snappy", "external_cfd", 200)
    mh.record("snappy", "external_cfd", 240)
    assert mh.estimate("snappy", "external_cfd") is None       # still < MIN_SAMPLES


def test_estimate_reports_the_measured_distribution(monkeypatch):
    _fake(monkeypatch)
    for s in (120, 180, 200, 220, 240, 600):
        mh.record("cfmesh", "internal_cfd", s)
    est = mh.estimate("cfmesh", "internal_cfd")
    assert est is not None
    assert est["n"] == 6
    assert est["p10_s"] <= est["typical_s"] <= est["p90_s"]
    assert est["p10_s"] >= 120 and est["p90_s"] <= 600      # nearest-rank: real values only


def test_engine_and_purpose_are_separate_populations(monkeypatch):
    _fake(monkeypatch)
    for _ in range(5):
        mh.record("snappy", "external_cfd", 3000)
    for _ in range(5):
        mh.record("cfmesh", "internal_cfd", 180)
    ext = mh.estimate("snappy", "external_cfd")
    intl = mh.estimate("cfmesh", "internal_cfd")
    assert ext["typical_s"] == 3000 and intl["typical_s"] == 180


def test_an_implausibly_short_run_is_not_recorded(monkeypatch):
    r = _fake(monkeypatch)
    for bad in (0, -5, 0.04, 0.9):
        mh.record("snappy", "external_cfd", bad)
    assert r.samples == {}


def test_stale_subsecond_garbage_already_in_the_store_is_ignored(monkeypatch):
    r = _fake(monkeypatch)
    r.samples[("vmtk", "unknown")] = [0.0, 0.0, 0.0]
    assert mh.estimate("vmtk", "unknown") is None      # three zeros are not three runs


def test_only_a_real_mesh_run_records_its_time():
    # run_mesh lives in the meshing family.
    src = (APP / "agents" / "builder" / "tools" / "meshing.py").read_text()
    i = src.index("_hist_record")
    window = src[i - 400:i]
    assert 'res.get("rc") == 0' in window, (
        "run_mesh records mesh time without gating on a successful (rc==0) run")


def test_the_meshing_event_carries_history_only_when_it_was_measured():
    import meshpipeline.events as E
    ev = E.meshing("builder", "snappy", 5400,
                   history={"n": 41, "typical_s": 220, "p10_s": 120, "p90_s": 360}).wire()
    assert ev["history"]["typical_s"] == 220
    # and with no history the field is simply absent, not a zero
    assert "history" not in E.meshing("builder", "snappy", 5400).wire()
