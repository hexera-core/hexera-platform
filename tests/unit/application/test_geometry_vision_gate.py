# Responsibility: Verify the look is off by default, never on the request path, fails open in both directions, and is paid for once per file.
# Boundaries: the gate, the queue seam and the document update; the measurement package itself is not installed here.
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import geometry_measurement as measurement
from meshpipeline.application import geometry_vision as vision
from meshpipeline.contracts import geometry_measurement as contract

LOOK_OK = {
    "status": "ok", "label": "VISUAL IMPRESSION", "is_measurement": False,
    "impression": {"looks_like": "a branched distribution manifold", "confidence": "high",
                   "attachments": ["a bolt flange at each end"], "internal_features": [],
                   "defects": ["a stray body"], "notes": "possibly a dead leg",
                   "openings_seen": [{"id": "o1", "looks_like": "flange face", "mouth": "flush",
                                      "likely_role": "inlet", "why": ""}],
                   "orientation": "the long axis runs along +X", "symmetry": "mirror symmetric",
                   "sharp_edges": [], "thin_parts": []},
    "views": ["iso"], "model": "a-vision-model", "provider": "a-provider", "seconds": 12.0,
    "caveat": "words, not a measurement",
}


def _never_read(*a, **k):
    """A source repository that must not be constructed. Both tests below stop at the measurement
    row, and that is the claim: a cached look fetches no bytes and a missing row asks for none."""
    raise AssertionError("the source row was read when it did not need to be")


DOCUMENT = {"schema": "geometry_agent.measurement.v1", "status": "ok", "reason": "",
            "representation": "wall_shell", "look": {"status": "not_attempted", "impression": None},
            "planner_block": {"schema": "geometry_agent.planner_block.v1", "status": "ok"}}


# OFF BY DEFAULT, AND OFF UNLESS THE MEASUREMENT IS ON


def test_the_setting_is_off_in_a_deployment_that_says_nothing():
    assert polcfg.GEOMETRY_VISION_ENABLED is False


def test_the_look_does_nothing_without_the_measurement_even_when_it_is_asked_for(monkeypatch):
    """The two gates are an AND, and the AND is in code. There is no row for a look to attach to."""
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", False)
    assert vision.look_enabled() is False
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)
    assert vision.look_enabled() is True


async def test_with_the_gate_off_nothing_is_read_and_nothing_is_written(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", False)

    def explode(*a, **k):
        raise AssertionError("the database was touched with the look switched off")

    monkeypatch.setattr("meshpipeline.persistence.session.get_db", explode)
    assert await vision.look_and_store("any", "owner") == {"status": "off"}


# NEVER IN THE REQUEST


def test_the_look_is_queued_from_the_measurement_and_never_run_inline(monkeypatch):
    """The one call site is after the row is committed, so a look cannot race the row it attaches to
    and cannot be the thing an upload waits on."""
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)
    seen: list[tuple] = []
    contract.set_look_enqueuer(lambda s, o: seen.append((s, o)))
    try:
        assert measurement._queue_the_look("src", "owner", "ok") == "queued"
        assert seen == [("src", "owner")]
    finally:
        contract.set_look_enqueuer(None)


def test_a_measurement_that_did_not_succeed_queues_no_look(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)
    contract.set_look_enqueuer(lambda s, o: pytest.fail("a failed measurement queued a look"))
    try:
        assert measurement._queue_the_look("src", "owner", "measurement_failed") == "not_measured"
    finally:
        contract.set_look_enqueuer(None)


def test_with_the_gate_off_the_measurement_queues_nothing(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", False)
    contract.set_look_enqueuer(lambda s, o: pytest.fail("a look was queued with the gate off"))
    try:
        assert measurement._queue_the_look("src", "owner", "ok") == "off"
    finally:
        contract.set_look_enqueuer(None)


def test_a_broker_that_will_not_take_the_task_is_a_look_that_does_not_happen():
    """Never an exception, and never the measurement's problem."""
    def refuse(_s, _o):
        raise RuntimeError("the broker is unreachable")

    contract.set_look_enqueuer(refuse)
    try:
        assert contract.enqueue_look("src", "owner") is False
    finally:
        contract.set_look_enqueuer(None)


def test_no_enqueuer_bound_is_a_logged_no_op():
    contract.set_look_enqueuer(None)
    assert contract.enqueue_look("src", "owner") is False


# FAIL OPEN IN BOTH DIRECTIONS


def test_an_image_without_the_measurement_package_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "_package", lambda: (_ for _ in ()).throw(ImportError("no package")))
    assert vision.look_at_local_file(tmp_path / "part.step", DOCUMENT) is None


def test_a_look_that_raises_despite_promising_not_to_still_writes_nothing(monkeypatch, tmp_path):
    def angry(*a, **k):
        raise RuntimeError("the entry point broke its own promise")

    monkeypatch.setattr(vision, "_package", lambda: (angry, MagicMock(), MagicMock()))
    assert vision.look_at_local_file(tmp_path / "part.step", DOCUMENT) is None


def test_the_document_keeps_its_measurement_when_the_look_is_attached():
    updated = vision.attach_look(DOCUMENT, LOOK_OK)
    assert updated["status"] == "ok"
    assert updated["representation"] == "wall_shell"
    assert updated["look"]["status"] == "ok"
    # and the original is not mutated: a caller that decides not to store it has lost nothing
    assert DOCUMENT["look"]["status"] == "not_attempted"


def test_the_planner_block_is_recomposed_by_the_package_and_never_patched_here(monkeypatch):
    """Every number in the block is the package's. A second spelling on this side is the whole thing
    `contracts/geometry_agent_block.py` refuses."""
    called: list[dict] = []
    fake = types.ModuleType("geometry_agent.agent.hexera")

    def planner_block(document):
        called.append(document)
        return {"schema": "geometry_agent.planner_block.v1", "status": "ok", "look": {"from": "package"}}

    fake.planner_block = planner_block
    with patch.dict(sys.modules, {"geometry_agent": types.ModuleType("geometry_agent"),
                                  "geometry_agent.agent": types.ModuleType("geometry_agent.agent"),
                                  "geometry_agent.agent.hexera": fake}):
        updated = vision.attach_look(DOCUMENT, LOOK_OK)
    assert called and called[0]["look"] == LOOK_OK
    assert updated["planner_block"]["look"] == {"from": "package"}


def test_a_package_that_cannot_recompose_the_block_leaves_the_measurements_own(monkeypatch):
    fake = types.ModuleType("geometry_agent.agent.hexera")

    def planner_block(_document):
        raise RuntimeError("the package moved under us")

    fake.planner_block = planner_block
    with patch.dict(sys.modules, {"geometry_agent": types.ModuleType("geometry_agent"),
                                  "geometry_agent.agent": types.ModuleType("geometry_agent.agent"),
                                  "geometry_agent.agent.hexera": fake}):
        updated = vision.attach_look(DOCUMENT, LOOK_OK)
    assert updated["planner_block"] == DOCUMENT["planner_block"]
    assert updated["look"]["status"] == "ok", "the look survives a block that could not be recomposed"


# PAID FOR ONCE PER FILE


async def test_a_second_job_on_the_same_file_pays_nothing(monkeypatch):
    """The look is stored with the measurement and keyed the same way, so a re-queued task stops at
    the row without a render or a provider call."""
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)
    row = MagicMock()
    row.document = {**DOCUMENT, "look": LOOK_OK}
    row.purpose, row.status, row.reason = "internal_cfd", "ok", ""
    row.sha256, row.facts_schema_version, row.agent_git_sha = "a" * 64, 3, "deadbeef"
    row.measure_seconds = 1.0

    class _Repo:
        async def for_source(self, *a, **k):
            return row

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    monkeypatch.setattr(vision, "look_at_local_file",
                        lambda *a, **k: pytest.fail("a cached look was looked at again"))
    with patch("meshpipeline.persistence.session.get_db", _db), \
         patch("meshpipeline.persistence.repositories.geometry_measurement_repository."
               "GeometryMeasurementRepository", _Repo), \
         patch("meshpipeline.persistence.repositories.geometry_source_repository."
               "GeometrySourceRepository", _never_read):
        out = await vision.look_and_store("12345678-1234-4234-a234-123456789abc", "owner")
    assert out["status"] == "cached"


async def test_no_measurement_row_is_a_skip_and_never_a_look(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)

    class _Repo:
        async def for_source(self, *a, **k):
            return None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    with patch("meshpipeline.persistence.session.get_db", _db), \
         patch("meshpipeline.persistence.repositories.geometry_measurement_repository."
               "GeometryMeasurementRepository", _Repo), \
         patch("meshpipeline.persistence.repositories.geometry_source_repository."
               "GeometrySourceRepository", _never_read):
        out = await vision.look_and_store("12345678-1234-4234-a234-123456789abc", "owner")
    assert out["status"] == "skipped"


async def test_a_malformed_source_id_is_a_skip(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_VISION_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)
    out = await vision.look_and_store("not-a-uuid", "owner")
    assert out == {"status": "skipped", "reason": "malformed source id"}
