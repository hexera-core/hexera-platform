# Responsibility: Verify the look is never on the request path, fails open in both directions, and is paid for once per file.
# Boundaries: the queue seam and the document update; the measurement package itself is not installed here.
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


# NEVER IN THE REQUEST, AND NEVER AT THE UPLOAD


def test_the_measurement_queues_no_look_and_says_the_look_is_the_surveys_to_take():
    """The upload never queues a look: one taken for the purpose assumed at upload reads an external
    body as internal flow. The measurement records which of the two situations this row is in."""
    contract.set_look_enqueuer(lambda s, o: pytest.fail("the measurement queued a look"))
    try:
        assert measurement._look_outcome("ok") == "deferred_to_survey"
    finally:
        contract.set_look_enqueuer(None)


def test_a_measurement_that_did_not_succeed_is_a_different_fact_from_one_that_is_waiting():
    """A failed measurement will never be looked at; a successful one is waiting for step 3. The row's
    log line keeps them apart, because "no look yet" and "no look ever" are not the same answer."""
    assert measurement._look_outcome("measurement_failed") == "not_measured"
    assert measurement._look_outcome("refused") == "not_measured"


def test_the_survey_is_what_queues_the_look_once_the_purpose_is_known():
    """Step 3 is queued at step 2, after the measurement has been composed for what the customer said."""
    from meshpipeline.application import geometry_survey as gs
    seen: list[tuple] = []
    contract.set_look_enqueuer(lambda s, o: seen.append((s, o)))
    try:
        assert gs._queue_the_look("src", "owner", dict(DOCUMENT)) == "queued"
        assert seen == [("src", "owner")]
    finally:
        contract.set_look_enqueuer(None)


def test_a_look_already_taken_is_not_queued_again():
    contract.set_look_enqueuer(lambda s, o: pytest.fail("a stored look was looked at again"))
    try:
        from meshpipeline.application import geometry_survey as gs
        assert gs._queue_the_look("src", "owner", {**DOCUMENT, "look": LOOK_OK}) == "cached"
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


# A LOOK NOBODY TOOK IS NOT A LOOK THAT FAILED
#
# `hexera.look_block` has TWO statuses of its own, `ok` and `failed`, and defaults an empty impression to
# `failed`: inside the agent a run always looks, so every way of not getting words back there IS a failure.
# `measure_local_file` took that default, so the block it writes at UPLOAD - before the look is queued at
# all, which the survey does later once the customer has said what the part is for - said the part's reading
# had FAILED. Every upload. `geometry_survey.look_state` then read `failed` off it and the builder was told
# a look had broken on a part nothing had looked at yet.


def test_the_measurement_says_nothing_looked_and_never_lets_the_package_decide_what_that_means():
    """The status is PASSED, not defaulted. A stand-in `look_block` that refuses to default is the whole
    test: it fails if this platform ever stops saying which of the states it means."""
    seen: list[dict] = []

    class _Hexera:
        def look_block(self, impression, *, status=None, **kw):
            assert status, ("the platform let the package decide what a look nobody took means; "
                            "its default for an empty impression is `failed`")
            seen.append({"status": status, "impression": impression})
            return {"status": status, "impression": impression}

        def report_measured(self, _facts, _unit, _brief, **kw):
            return {"look": kw["look"], "facts": {}}

    hexera = _Hexera()
    monkey = MagicMock()
    monkey.measure_isolated.return_value = object()
    monkey.measure.return_value = object()
    run = MagicMock()
    run.file_ceiling_bytes.return_value = 0
    with patch.object(measurement, "_package", lambda: (hexera, run, monkey)):
        document = measurement.measure_local_file(_a_file(), timeout_s=1.0)
    assert document["status"] == "ok", document.get("reason")
    assert seen == [{"status": measurement.LOOK_NOT_ATTEMPTED, "impression": None}]
    assert document["look"]["status"] == measurement.LOOK_NOT_ATTEMPTED


def test_the_word_the_measurement_writes_is_the_word_look_state_reads():
    """Two spellings of one fact is the thing that goes out of step, and this fact is read in another
    module: `geometry_survey.look_state` maps the stored status onto one of four states, and a status it
    does not recognise falls through to `not_attempted` by luck rather than by agreement."""
    from meshpipeline.application import geometry_survey as gs

    assert measurement.LOOK_NOT_ATTEMPTED == gs.LOOK_NONE
    assert measurement.LOOK_NOT_ATTEMPTED in gs.LOOK_STATES
    assert measurement.LOOK_NOT_ATTEMPTED not in (gs.LOOK_FAILED, gs.LOOK_OK, gs.LOOK_PENDING)


def test_the_reader_that_was_never_configured_says_the_same_word(monkeypatch):
    """The other place this platform writes the block itself. A reader with no key in this environment did
    not fail to look: nothing looked, and the row has to say which."""
    monkeypatch.setattr(vision, "reader", lambda: None)
    monkeypatch.setattr(vision, "_package", lambda: (MagicMock(), MagicMock(), _RecordingHexera()))
    block = vision.look_at_local_file(_a_file(), DOCUMENT)
    assert block["status"] == measurement.LOOK_NOT_ATTEMPTED
    assert "has no key in this environment" in block["reason"]


class _RecordingHexera:
    def look_block(self, impression, *, status=None, reason="", **kw):
        assert status, "the platform let the package decide what a reader that was never built means"
        return {"status": status, "impression": impression, "reason": reason}


def _a_file():
    """A path that exists and is small, so `measure_local_file` reaches the report rather than the ceiling."""
    return Path(__file__)


# FAIL OPEN IN BOTH DIRECTIONS


def test_an_image_without_the_measurement_package_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "_package", lambda: (_ for _ in ()).throw(ImportError("no package")))
    assert vision.look_at_local_file(tmp_path / "part.step", DOCUMENT) is None


def test_a_look_that_raises_despite_promising_not_to_still_writes_nothing(monkeypatch, tmp_path):
    def angry(*a, **k):
        raise RuntimeError("the entry point broke its own promise")

    monkeypatch.setattr(vision, "_package", lambda: (angry, MagicMock(), MagicMock()))
    # a reader that builds, so the entry point is actually reached: without a provider key in the
    # environment `reader()` is None and the look stops at `not_attempted` before it is ever called
    monkeypatch.setattr(vision, "reader", lambda: object())
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


@pytest.mark.parametrize("composed", [False, True])
async def test_a_new_look_is_stored_under_the_purpose_the_row_was_measured_for(monkeypatch, tmp_path, composed):
    """The row keeps the purpose its facts were MEASURED for, whatever the survey later composed. Before a
    survey was composed the look used to be written with a purpose of None, which the repository cannot slice,
    so every look was paid for and then dropped as `unstored`; after one, the row was relabelled with the
    customer's purpose. Both are the same row and the same label."""
    row = MagicMock()
    row.document = dict(DOCUMENT)
    row.purpose, row.status, row.reason = "internal_cfd", "ok", ""
    row.sha256, row.facts_schema_version, row.agent_git_sha = "a" * 64, 3, "deadbeef"
    row.measure_seconds = 1.0
    written: list[dict] = []

    class _Repo:
        async def for_source(self, *a, **k):
            return row

        async def record(self, db, **kw):
            assert isinstance(kw["purpose"], str)  # the real repository slices it, and None cannot be sliced
            written.append(kw)

    source = MagicMock(id="12345678-1234-4234-a234-123456789abc", owner_id="owner", object_key="k",
                       sha256="a" * 64, size_bytes=10, original_filename="p.step", suffix_hint=".step",
                       purged_at=None)

    class _Sources:
        async def get_for_owner(self, *a, **k):
            return source

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    async def composed(*_a, **_k):
        return ("external_cfd", "external") if composed else (None, None)

    seen: dict = {}

    def look(path, document, **kw):
        seen.update(kw)
        return LOOK_OK

    async def recompose(*_a, **_k):
        return "ok"

    from meshpipeline.application import geometry_survey
    monkeypatch.setattr(vision, "_composed_for", composed)
    monkeypatch.setattr(vision, "look_at_local_file", look)
    monkeypatch.setattr(vision, "attach_look", lambda document, found: {**document, "look": found})
    monkeypatch.setattr(geometry_survey, "recompose_after_look", recompose)
    with patch("meshpipeline.persistence.session.get_db", _db), \
         patch("meshpipeline.persistence.repositories.geometry_measurement_repository."
               "GeometryMeasurementRepository", _Repo), \
         patch("meshpipeline.persistence.repositories.geometry_source_repository."
               "GeometrySourceRepository", _Sources), \
         patch("meshpipeline.application.geometry_materializer.fetch_verified_bytes",
               lambda ref, **k: tmp_path / "p.step"):
        out = await vision.look_and_store("12345678-1234-4234-a234-123456789abc", "owner")
    assert out["status"] == "ok"
    assert [w["purpose"] for w in written] == ["internal_cfd"]
    assert seen["purpose"] == ("external_cfd" if composed else None)


async def test_a_look_that_failed_is_composed_into_the_survey_as_well_as_one_that_landed(monkeypatch,
                                                                                          tmp_path):
    """A FAILED LOOK IS NOT AN ABSENT ONE (audit item 15). This was gated on `status == "ok"`, so a look that
    broke never reached the survey row: the row went on saying `not_attempted`, which is what it says when
    nobody has looked at all, and the builder's block then told the builder nothing had looked at a part whose
    look had broken. The row records the look's state, so every state has to reach it."""
    row = MagicMock()
    row.document = dict(DOCUMENT)
    row.purpose, row.status, row.reason = "internal_cfd", "ok", ""
    row.sha256, row.facts_schema_version, row.agent_git_sha = "a" * 64, 3, "deadbeef"
    row.measure_seconds = 1.0
    failed = {"status": "failed", "reason": "the provider returned something that was not JSON",
              "impression": None}

    class _Repo:
        async def for_source(self, *a, **k):
            return row

        async def record(self, db, **kw):
            return None

    source = MagicMock(id="12345678-1234-4234-a234-123456789abc", owner_id="owner", object_key="k",
                       sha256="a" * 64, size_bytes=10, original_filename="p.step", suffix_hint=".step",
                       purged_at=None)

    class _Sources:
        async def get_for_owner(self, *a, **k):
            return source

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    async def composed(*_a, **_k):
        return ("internal_cfd", "wall_shell")

    taken: list[dict] = []

    async def recompose(_source_id, _owner, document):
        taken.append(document)
        return "recomposed"

    from meshpipeline.application import geometry_survey
    monkeypatch.setattr(vision, "_composed_for", composed)
    monkeypatch.setattr(vision, "look_at_local_file", lambda path, document, **kw: failed)
    monkeypatch.setattr(vision, "attach_look", lambda document, found: {**document, "look": found})
    monkeypatch.setattr(geometry_survey, "recompose_after_look", recompose)
    with patch("meshpipeline.persistence.session.get_db", _db), \
         patch("meshpipeline.persistence.repositories.geometry_measurement_repository."
               "GeometryMeasurementRepository", _Repo), \
         patch("meshpipeline.persistence.repositories.geometry_source_repository."
               "GeometrySourceRepository", _Sources), \
         patch("meshpipeline.application.geometry_materializer.fetch_verified_bytes",
               lambda ref, **k: tmp_path / "p.step"):
        out = await vision.look_and_store("12345678-1234-4234-a234-123456789abc", "owner")
    assert out["status"] == "failed"
    assert len(taken) == 1, "the failed look never reached the survey"
    assert taken[0]["look"]["status"] == "failed"
    assert taken[0]["look"]["reason"]


async def test_no_measurement_row_is_a_skip_and_never_a_look(monkeypatch):

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
    out = await vision.look_and_store("not-a-uuid", "owner")
    assert out == {"status": "skipped", "reason": "malformed source id"}
