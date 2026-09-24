# Responsibility: Prove a file whose FORMAT the measurement cannot open is refused with an instruction, and that
#                 every other measurement failure still defers and is never blamed on the customer.
# Boundaries: it measures real bytes through the installed package and drives the admission node; it uploads
#             nothing and meshes nothing.
#
# THE DEFECT. A `.vtp` upload was accepted, and then measured NOTHING, silently. The measurement package reads
# .stl/.obj/.ply/.off/.glb/.gltf through trimesh and .step/.stp/.iges/.igs through OpenCASCADE
# (`geometry_agent.facts.load`); VTK PolyData is readable only inside the mesh engine's own bundle, which is a
# different image from the two that measure. So `load_mesh` raised UnsupportedGeometry, the row stored
# `measurement_failed` - the same status a timeout gets - and `pipeline/geometry_admission` deferred to the
# builder, CORRECTLY, because a step of ours that failed is never the customer's fault. Net effect: for that
# one format the port table, the questions, the look and the plan were all skipped, the job meshed anyway, and
# nobody was told. A fact that lies by omission.
#
# WHAT IS NOT DONE HERE, deliberately: .vtp is still ACCEPTED at upload. vmtk takes a .vtp natively and staging
# it through the vmtk bundle loses nothing, so removing it from `ACCEPTED_SUFFIXES` would cost a real capability
# to fix a silence. What is fixed is the silence: the measurement now REFUSES the format as its own outcome, and
# admission refuses the job with a sentence naming what to send instead, before any builder runs.
#
# THE GENERAL RULE IS THE POINT, not the .vtp case. `test_no_accepted_format_is_silently_unmeasurable` derives
# both sides - the accepted list from the format table, the readable list from the installed package - so a
# format added tomorrow that nothing can measure and nothing can advise about fails on the commit that adds it.
from __future__ import annotations

import asyncio
import struct

import pytest

from meshpipeline.application import geometry_measurement as gm
from meshpipeline.contracts.geometry_measurement import (
    STATUS_MEASUREMENT_FAILED,
    STATUS_OK,
    STATUS_REFUSED,
    STATUS_UNSUPPORTED_FORMAT,
    STATUSES,
    STATUSES_ABOUT_THE_FILE,
)
from meshpipeline.contracts.intake_formats import ACCEPTED_SUFFIXES, SEND_INSTEAD, send_instead
from meshpipeline.pipeline import geometry_admission as ga


def _a_binary_stl(path, *, triangles: int = 1):
    """A real, readable binary STL: one degenerate-free triangle is enough to be opened."""
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", triangles))
        for _ in range(triangles):
            fh.write(struct.pack("<12fH", 0, 0, 1,
                                 0, 0, 0,  1, 0, 0,  0, 1, 0, 0))
    return path


# ---------------------------------------------------------------- what the package can open

def test_the_readable_formats_come_from_the_package_and_not_from_a_list_here():
    readable = gm.readable_suffixes()
    assert readable is not None, (
        "the installed measurement package could not be asked what it can open, so this whole file is "
        "measuring nothing. Install the vendored wheel (see .github/actions/setup-hexera).")
    # the three this product is built on, or the parse is wrong rather than the package narrow
    assert {".stl", ".step", ".stp", ".iges", ".igs"} <= readable, sorted(readable)
    # and the source is the package: no suffix is written down in the function that answers
    import inspect
    body = inspect.getsource(gm.readable_suffixes)
    assert "MESH_SUFFIXES" in body, body
    for suffix in sorted(readable):
        assert f'"{suffix}"' not in body and f"'{suffix}'" not in body, (
            f"readable_suffixes keeps its own copy of {suffix}, which is a list that goes stale in silence "
            f"on the day the package stops reading it")


def test_a_package_that_cannot_be_asked_is_not_a_package_that_reads_nothing(monkeypatch, tmp_path):
    """None, never an empty set. An image with no package must refuse every upload for the reason it really
    has - `refused: PACKAGE_ABSENT` - and not for a format verdict nobody reached."""
    monkeypatch.setattr(gm, "readable_suffixes", lambda: None)
    path = _a_binary_stl(tmp_path / "part.vtp")     # a suffix nothing can read, with readable bytes
    out = gm.measure_local_file(path)
    assert out["status"] != STATUS_UNSUPPORTED_FORMAT, (
        "the format was refused on a verdict that could not be reached")


# ---------------------------------------------------------------- the refusal

def test_a_vtp_is_refused_as_its_own_outcome_rather_than_stored_as_a_failure(tmp_path):
    # Real bytes, so nothing here passes for the wrong reason: the file exists, is readable, and is under
    # every ceiling. The only thing wrong with it is its format.
    path = _a_binary_stl(tmp_path / "lumen.vtp")
    out = gm.measure_local_file(path)
    assert out["status"] == STATUS_UNSUPPORTED_FORMAT, out
    assert out["status"] != STATUS_MEASUREMENT_FAILED, "still indistinguishable from a timeout"
    reason = out["reason"]
    assert ".vtp" in reason, reason
    assert ".stl" in reason, f"the refusal does not name what to send instead:\n{reason}"
    assert "cannot be measured" in reason, f"the refusal does not say why:\n{reason}"
    # it names what the measurement CAN read, so a customer with some other file is not left guessing
    assert ".step" in reason and ".iges" in reason, reason
    # and the document is still a document: no numbers invented, and the schema intact
    assert out["plan"] is None and out["schema"] == gm.MEASUREMENT_SCHEMA


def test_a_format_the_package_can_open_is_not_refused_for_its_format(tmp_path):
    # The premise. Without this the refusal above could be refusing everything.
    out = gm.measure_local_file(_a_binary_stl(tmp_path / "part.stl", triangles=4))
    assert out["status"] != STATUS_UNSUPPORTED_FORMAT, out


def test_the_refusal_is_reached_before_the_file_is_opened(tmp_path, monkeypatch):
    """A format that cannot be read is answered from the suffix, not by handing the bytes to a parser and
    catching what comes back. The old path did the latter, which is why the answer arrived as a failure."""
    def _boom(*_a, **_k):
        raise AssertionError("the measurement opened a file whose format it cannot read")

    monkeypatch.setattr("geometry_agent.facts.measure.measure_isolated", _boom)
    monkeypatch.setattr("geometry_agent.facts.measure.measure", _boom)
    out = gm.measure_local_file(_a_binary_stl(tmp_path / "lumen.vtp"))
    assert out["status"] == STATUS_UNSUPPORTED_FORMAT


# ---------------------------------------------------------------- the general rule

def test_no_accepted_format_is_silently_unmeasurable():
    """Derived on both sides, which is the only version of this worth having: the accepted list from
    `contracts/intake_formats`, the readable list from the installed package. A format accepted tomorrow that
    the measurement cannot open AND that has no sentence saying what to send instead fails here."""
    readable = gm.readable_suffixes()
    assert readable is not None, "the package could not be asked, so this rule checked nothing"
    for suffix in sorted(ACCEPTED_SUFFIXES):
        if suffix in readable:
            continue
        assert suffix in SEND_INSTEAD, (
            f"{suffix} is accepted at upload, the measurement cannot open it, and there is no sentence in "
            f"contracts/intake_formats.SEND_INSTEAD telling a customer what to send instead. Every part sent "
            f"as {suffix} reaches the builder with nothing measured, looked at, asked or planned, and admission "
            f"would refuse it with no instruction.")
        assert SEND_INSTEAD[suffix].strip().endswith("."), (
            f"the advice for {suffix} is not a sentence: {SEND_INSTEAD[suffix]!r}")


def test_the_advice_never_points_at_a_format_that_cannot_be_measured_either():
    """A refusal that tells a customer to convert to something this product also cannot measure is worse than
    one that tells them nothing."""
    readable = gm.readable_suffixes()
    assert readable is not None
    generic = send_instead(".dwg")
    named = {s for s in ACCEPTED_SUFFIXES if s in generic}
    assert named, f"the generic advice names no format at all:\n{generic}"
    assert named <= readable, f"the generic advice points at {sorted(named - readable)}"
    for suffix, advice in SEND_INSTEAD.items():
        pointed = {s for s in ACCEPTED_SUFFIXES if s in advice}
        assert pointed, f"the advice for {suffix} names no format to send:\n{advice}"
        assert pointed <= readable, (
            f"the advice for {suffix} says to send {sorted(pointed - readable)}, which the measurement "
            f"cannot open either")
        assert suffix not in pointed, f"the advice for {suffix} says to send a {suffix}"


# ---------------------------------------------------------------- at admission

STATE = {"job_id": "j", "engine": "vmtk", "purpose": "internal_cfd", "input_kind": "body-surface",
         "dimensionality": "3D", "intake_patches": [], "engine_params": {"wall_layers": "on"},
         "geometry": {"ref": {"source_id": "11111111-1111-4111-8111-111111111111",
                              "owner_id": "o", "object_key": "sources/x", "sha256": "b" * 64,
                              "size_bytes": 10, "original_filename": "lumen.vtp",
                              "suffix_hint": ".vtp"}}}


@pytest.fixture
def admission(monkeypatch):
    """The node, with the stream silenced and any attempt to compute an analysis made loud."""
    published: list[tuple[str, str]] = []

    async def _record(_job_id, level, text):
        published.append((level, text))

    def _boom(*_a, **_k):
        raise AssertionError("the slot computed a surface analysis instead of reading one")

    monkeypatch.setattr(ga, "_publish", _record)
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _boom)
    monkeypatch.setattr("meshpipeline.cad.surface_checks.surface_analysis_for", _boom)

    def run(analysis):
        async def _read(_ref):
            return analysis
        monkeypatch.setattr("meshpipeline.cad.regions.reading_for_source", _read)
        return asyncio.run(ga.node_geometry_admission(dict(STATE))), published

    return run


def test_admission_refuses_a_format_that_cannot_be_measured_and_says_what_to_send(admission):
    reason = gm.UNSUPPORTED_FORMAT.format(suffix=".vtp", readable=".stl, .step",
                                          instead=send_instead(".vtp"))
    out, published = admission({"status": STATUS_UNSUPPORTED_FORMAT, "reason": reason})
    assert out["executor_success"] is False, out
    assert out["retry_count"] > 0, "a format that cannot be read is retried"
    assert ".stl" in out["geometry_unsuitable_reason"], out["geometry_unsuitable_reason"]
    assert published and published[-1][0] == "ERROR"
    assert "Input rejected" in published[-1][1] and ".stl" in published[-1][1], published[-1]


def test_admission_refuses_even_when_the_reason_came_back_empty(admission):
    # A row with no reason must still refuse, and still say something: the alternative is a job that stops
    # with no explanation, which is a worse silence than the one being fixed.
    out, published = admission({"status": STATUS_UNSUPPORTED_FORMAT, "reason": ""})
    assert out["executor_success"] is False
    assert out["geometry_unsuitable_reason"].strip(), out
    assert published[-1][0] == "ERROR"


@pytest.mark.parametrize("status,reason", [
    (STATUS_MEASUREMENT_FAILED, "the file could not be opened"),
    (STATUS_REFUSED, "the geometry measurement package is not installed in this image"),
])
def test_every_other_outcome_still_defers_and_never_blames_the_customer(admission, status, reason):
    """THE DISTINCTION THIS WHOLE CHANGE IS. A measurement that timed out, or a package that is not in the
    image, is OURS: the job goes on and the customer is told nothing about a step of ours that failed. Only a
    fact about the FILE is refused. `refused` deliberately stays on this side, because it covers both a file
    over the ceiling and a package that is absent and the status alone cannot tell them apart."""
    out, published = admission({"status": status, "reason": reason})
    assert out == {}, out
    assert not [p for p in published if p[0] == "ERROR"], published


def test_the_two_kinds_of_outcome_are_declared_apart_rather_than_enumerated_at_each_reader():
    assert STATUS_UNSUPPORTED_FORMAT in STATUSES, "the new answer is not in the status vocabulary"
    assert STATUSES_ABOUT_THE_FILE == (STATUS_UNSUPPORTED_FORMAT,), STATUSES_ABOUT_THE_FILE
    for ours in (STATUS_MEASUREMENT_FAILED, STATUS_REFUSED, STATUS_OK):
        assert ours not in STATUSES_ABOUT_THE_FILE
    # and the reader reads the tuple rather than naming the status, so a second file-fact status reaches it
    text = open(ga.__file__, encoding="utf-8").read()
    assert "STATUSES_ABOUT_THE_FILE" in text
    assert f'"{STATUS_UNSUPPORTED_FORMAT}" ==' not in text and f"== '{STATUS_UNSUPPORTED_FORMAT}'" not in text


def test_the_refusal_survives_the_projection_between_the_row_and_admission():
    """FOLLOWED RATHER THAN ASSUMED. The status crosses two translations on its way to the node -
    `contracts.projection_of` and `cad/regions.surface_analysis_from_document` - and both of them map an
    unknown status onto `measurement_failed`, which DEFERS. A refusal that were dropped by either would leave
    the node deferring on exactly the case it exists to refuse, with every test above still green."""
    from meshpipeline.cad.regions import surface_analysis_from_document
    from meshpipeline.contracts.geometry_measurement import projection_of

    document = {"schema": gm.MEASUREMENT_SCHEMA, "status": STATUS_UNSUPPORTED_FORMAT,
                "reason": gm.UNSUPPORTED_FORMAT.format(suffix=".vtp", readable=".stl",
                                                       instead=send_instead(".vtp")),
                "plan": None, "seconds": 0.0}
    projected = projection_of(document)
    assert projected["status"] == STATUS_UNSUPPORTED_FORMAT, projected
    analysis = surface_analysis_from_document(document)
    assert analysis["status"] == STATUS_UNSUPPORTED_FORMAT, analysis
    assert ".stl" in analysis["reason"], analysis
    assert analysis["status"] in STATUSES_ABOUT_THE_FILE, (
        "the status the node receives is not the one it refuses on, so nothing would ever be refused")


def test_a_real_measurement_of_a_vtp_reaches_admission_as_a_refusal(tmp_path, admission):
    """END TO END through the real functions, because every step above was checked on its own. The bytes are
    measured, the document that comes back is projected the way the row is read, and the node is given THAT."""
    from meshpipeline.cad.regions import surface_analysis_from_document

    document = gm.measure_local_file(_a_binary_stl(tmp_path / "lumen.vtp"))
    assert document["status"] == STATUS_UNSUPPORTED_FORMAT
    out, published = admission(surface_analysis_from_document(document))
    assert out["executor_success"] is False, out
    assert ".stl" in out["geometry_unsuitable_reason"], out["geometry_unsuitable_reason"]
    assert published[-1][0] == "ERROR"


def test_a_stored_status_outside_the_vocabulary_is_not_read_as_a_file_fact():
    """`status_of` maps anything it does not know onto `measurement_failed`, which defers. A row written by a
    newer image must never be read as grounds to refuse a customer's job."""
    from meshpipeline.contracts.geometry_measurement import MEASUREMENT_SCHEMA, status_of
    assert status_of({"schema": MEASUREMENT_SCHEMA, "status": "something_later"}) == STATUS_MEASUREMENT_FAILED
    assert status_of({"schema": MEASUREMENT_SCHEMA,
                      "status": STATUS_UNSUPPORTED_FORMAT}) == STATUS_UNSUPPORTED_FORMAT
