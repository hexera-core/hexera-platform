# Responsibility: Pin, for every engine x flow x geometry form, whether the engine is offered, and what a refusal says.
"""THE DECLARED CAPABILITY MATRIX, end to end.

Every engine declares the flows it is designed for and the geometry forms (CAD solid, surface)
it takes for each (EngineSpec.accepts). These tests pin the TRUE matrix on main, and that every
consumer derives from it: admission, the run's start-of-run choice, the fallback ladder, the
intake's proposal and comparison, and the closing message of a refused run.

The 2026-10-03 aorta STL (internal flow) is the case that started it: snappyHexMesh refused it
deep in its driver, the user read a crash, VMTK - which cannot take an STL either - was offered,
and the identical run was tried again."""
from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

import pytest

from meshpipeline.engines import capability as cap
from meshpipeline.engines.admission import AdmissionEvidence
from meshpipeline.engines.purposes import PURPOSES, is_compatible
from meshpipeline.engines.registry import (
    ENGINE_CATALOG,
    engine_label,
    engine_names,
    get_spec,
    resolve_engine_params,
)

#: THE TRUE MATRIX ON MAIN (2026-10-04): (engine, flow) -> the forms it takes. A pair not listed
#: is a flow the engine is not designed for. Surface internal flow lands with the STL-INTERNAL
#: work; when it does, only the engines' `accepts` declarations - and this table - change.
EXPECTED: dict[tuple[str, str], set[str]] = {
    ("cfmesh", "external"): {"cad", "surface"},
    ("cfmesh", "internal"): {"cad"},
    ("snappy", "external"): {"cad", "surface"},
    ("snappy", "internal"): {"cad"},
    ("gmsh", "structural"): {"cad"},
    ("gmsh", "external"): {"cad"},
    ("gmsh", "internal"): {"cad"},
    ("vmtk", "internal"): {"cad"},
    ("snappy_multiregion", "multi-region"): {"cad"},
}

FILE_OF = {"cad": "part.step", "surface": "part.stl"}
_PATCHES = {
    "external": ({"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}),
    "internal": ({"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                 {"name": "outlet", "type": "outlet"}),
}
_PURPOSE_OF = {p.flow_kind: k for k, p in PURPOSES.items()}


def _cells():
    """Every (engine, flow, form) the system can be asked for."""
    return list(itertools.product(sorted(engine_names()), cap.FLOW_KINDS, cap.GEOMETRY_FORMS))


def _input_kind(spec, purpose: str) -> str | None:
    """A geometry kind this engine's capability accepts for the purpose (so only the FORM is
    under test), or None when the engine cannot serve the purpose at all."""
    for c in spec.capabilities:
        if is_compatible(spec, purpose, c.input_kind):
            return c.input_kind
    return None


def _state(engine: str, flow: str, form: str, **kw) -> dict:
    purpose = _PURPOSE_OF[flow]
    s = {"job_id": "t", "engine": engine, "engine_source": "user_direct", "purpose": purpose,
         "input_kind": _input_kind(get_spec(engine), purpose) or "body-surface",
         "dimensionality": "3D", "intake_patches": [dict(p) for p in _PATCHES.get(flow, ())],
         "engine_params": resolve_engine_params(engine, {}), "request_txt": "mesh it",
         "review_brief_txt": "", "retry_count": 0, "engine_ladder": {},
         # the reference alone: what an API turn and the ladder carry - the form is read from it
         "geometry": {"ref": {"suffix_hint": "." + FILE_OF[form].split(".")[-1],
                              "original_filename": FILE_OF[form]}}}
    s.update(kw)
    return s


# the declarations themselves

def test_every_implemented_engine_declares_what_it_takes_per_flow():
    for name in engine_names():
        sp = get_spec(name)
        assert sp.accepts, f"{name} declares no `accepts` - it could never be offered truthfully"
        for fs in sp.accepts:
            assert fs.flow in cap.FLOW_KINDS, f"{name}: unknown flow {fs.flow!r}"
            assert fs.forms and set(fs.forms) <= set(cap.GEOMETRY_FORMS), (
                f"{name}/{fs.flow}: forms {fs.forms} are not canonical geometry forms")


def test_the_declared_flows_agree_with_the_capabilities():
    # ONE fact, two declarations: the flows `accepts` names are exactly the purposes the
    # engine's mesh capabilities can serve - neither may claim a flow the other does not
    for name in engine_names():
        sp = get_spec(name)
        served = {PURPOSES[p].flow_kind for p in PURPOSES if is_compatible(sp, p)}
        assert {fs.flow for fs in sp.accepts} == served, (
            f"{name}: accepts {sorted(fs.flow for fs in sp.accepts)} but its capabilities serve "
            f"{sorted(served)}")


def test_the_matrix_is_the_true_one_on_main():
    table = cap.capability_table()
    for engine, flow, form in _cells():
        want = form in EXPECTED.get((engine, flow), set())
        assert table[(engine, flow, form)] is want, (
            f"{engine} x {flow} x {form}: declared {table[(engine, flow, form)]}, true {want}")


def test_a_purpose_names_its_flow():
    assert {p.flow_kind for p in PURPOSES.values()} == set(cap.FLOW_KINDS)
    assert cap.flow_of("internal_cfd") == "internal" and cap.flow_of("structural") == "structural"
    assert cap.flow_of("not a purpose") == ""


@pytest.mark.parametrize("source,form", [
    ("aorta.stl", "surface"), ("AORTA.STL", "surface"), (".stl", "surface"),
    ("wing.step", "cad"), ("wing.stp", "cad"), ("duct.iges", "cad"), ("duct.igs", "cad"),
    ("block.brep", "cad"), ("lumen.vtp", "surface"), ("part.obj", "surface"),
    ("part.ply", "surface"), ("vol.vtu", "surface"), ("m.msh", "surface"),
    ("/jobs/x/input.step", "cad"), ("C:\\cad\\car.STEP", "cad"),
    ("bracket.sldprt", ""), ("noext", ""), ("", ""), (None, "")])
def test_a_file_reads_as_cad_or_surface_and_unknown_says_nothing(source, form):
    assert cap.geometry_form(source) == form


def test_the_form_is_read_from_the_runs_geometry_reference():
    assert cap.geometry_form_of_state(_state("snappy", "internal", "surface")) == "surface"
    assert cap.geometry_form_of_state({"geometry": {"local_path": "/w/in.step",
                                                    "ref": {"suffix_hint": ""}}}) == "cad"
    assert cap.geometry_form_of_state({}) == "" and cap.geometry_form_of_state(None) == ""


# admission: the one rule every consumer goes through

@pytest.mark.parametrize("engine,flow,form", _cells())
def test_admission_refuses_exactly_the_forms_an_engine_does_not_take(engine, flow, form):
    spec = get_spec(engine)
    purpose = _PURPOSE_OF[flow]
    codes = {r.code for r in spec.admit(AdmissionEvidence(
        engine=engine, purpose=purpose, geometry_form=form))}
    designed = (engine, flow) in EXPECTED
    refused = designed and form not in EXPECTED[(engine, flow)]
    assert ("geometry_form_unsupported" in codes) is refused, (engine, flow, form, codes)
    # an engine not designed for the flow is refused for THAT (purpose_incompatible), not its form
    assert ("purpose_incompatible" in codes) is (not designed), (engine, flow, form, codes)


@pytest.mark.parametrize("engine", sorted(ENGINE_CATALOG))
def test_an_unknown_form_is_never_refused(engine):
    for purpose in PURPOSES:
        for form in (None, ""):
            codes = {r.code for r in get_spec(engine).admit(AdmissionEvidence(
                engine=engine, purpose=purpose, geometry_form=form))}
            assert "geometry_form_unsupported" not in codes


def test_the_refusal_states_the_engines_own_limit_in_plain_words():
    r = [x for x in get_spec("snappy").admit(AdmissionEvidence(
        engine="snappy", purpose="internal_cfd", geometry_form="surface"))
        if x.code == "geometry_form_unsupported"][0]
    assert r.phase == "declared" and r.field == "geometry"
    assert r.message == ("snappyHexMesh cannot mesh internal flow from a surface mesh (STL or a "
                         "similar triangle file): for internal flow it needs a CAD solid (STEP or "
                         "IGES).")
    assert "crash" not in r.message.lower()


# who is offered: the roster, the start-of-run choice and the ladder

@pytest.mark.parametrize("flow,form", list(itertools.product(cap.FLOW_KINDS,
                                                             cap.GEOMETRY_FORMS)))
def test_the_engines_offered_for_a_file_are_exactly_those_that_take_it(flow, form):
    want = [e for e in cap.ladder_order(flow) if form in EXPECTED.get((e, flow), set())]
    assert cap.engines_for(flow, form) == want


@pytest.mark.parametrize("engine,flow,form", [
    c for c in _cells() if c[1] in ("external", "internal") and (c[0], c[1]) in EXPECTED])
def test_the_ladder_never_carries_an_engine_that_cannot_take_the_file(engine, flow, form):
    from meshpipeline.pipeline import engine_fallback as lad
    rungs = [r.engine for r in lad.ladder(_state(engine, flow, form))]
    assert rungs[0] == engine
    for other in rungs[1:]:
        assert form in EXPECTED[(other, flow)], (
            f"{other} is a rung for {flow} from {form}, which it cannot take")


def test_the_aorta_stl_offers_no_engine_that_cannot_take_an_stl():
    # job d20ad762: snappyHexMesh, internal flow, an STL - the run ended offering VMTK
    from meshpipeline.pipeline import engine_fallback as lad
    st = _state("snappy", "internal", "surface")
    assert [r.engine for r in lad.ladder(st)] == ["snappy"]


def test_a_cad_file_keeps_every_internal_engine_on_the_ladder():
    from meshpipeline.pipeline import engine_fallback as lad
    rungs = [r.engine for r in lad.ladder(_state("snappy", "internal", "cad",
                                                 input_kind="fluid-domain"))]
    assert rungs[0] == "snappy" and {"cfmesh", "vmtk", "gmsh"} & set(rungs[1:])


@pytest.mark.parametrize("form,engine", [("surface", "cfmesh"), ("cad", "cfmesh")])
def test_the_start_of_run_choice_reads_the_declarations(monkeypatch, tmp_path, form, engine):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.pipeline.engine_select import node_engine_select
    install(monkeypatch, _ep)
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    st = _state("", "internal", form)
    st.pop("engine")
    # with no engine taking an STL for internal flow, the default stays the candidate and
    # admission refuses it honestly; with a STEP the declared first choice is taken
    assert asyncio.run(node_engine_select(st)) == {"engine": engine}


def test_a_future_engine_is_offered_from_its_spec_alone():
    # adding an engine needs only its spec: the roster, the ladder order and the matrix read it
    from dataclasses import replace

    from meshpipeline.engines.base import FlowSupport
    fake = replace(get_spec("vmtk"), name="tubemesh", ladder_rank=5,
                   accepts=(FlowSupport("internal", ("cad", "surface")),))
    specs = {**{n: get_spec(n) for n in engine_names()}, "tubemesh": fake}
    assert cap.ladder_order("internal", specs)[0] == "tubemesh"
    assert cap.engines_for("internal", "surface", specs) == ["tubemesh"]
    assert cap.capability_table(specs)[("tubemesh", "internal", "surface")] is True
    assert "tubemesh" in cap.who_can("internal", "surface", specs=specs)


# the intake: proposals, previews and comparisons are judged against the uploaded file

def _intake_preview(engine: str, flow: str, form: str) -> dict:
    from meshpipeline.agents.intake.validation import preview_admission
    purpose = _PURPOSE_OF[flow]
    return preview_admission(engine, purpose, _input_kind(get_spec(engine), purpose) or "",
                             dimensionality="3D", patches=list(_PATCHES.get(flow, ())),
                             engine_params=resolve_engine_params(engine, {}),
                             geometry_form=form)


@pytest.mark.parametrize("engine,flow,form", [c for c in _cells() if (c[0], c[1]) in EXPECTED])
def test_the_intake_preview_refuses_exactly_what_the_engine_cannot_take(engine, flow, form):
    r = _intake_preview(engine, flow, form)
    if form in EXPECTED[(engine, flow)]:
        assert "geometry_form_unsupported" not in (r.get("blocking_rule_codes") or []), r
        return
    assert r["verdict"] == "impossible"
    assert "geometry_form_unsupported" in r["blocking_rule_codes"]
    assert r["capability_reason"].startswith(f"{engine_label(engine)} cannot mesh")
    if not cap.engines_for(flow, form):
        # NO engine can: the way on is the file, said without naming any other engine
        assert "STEP or IGES" in r["safe_user_message"]
        assert "Ask me to list the compatible engines" not in r["safe_user_message"]
        others = [engine_label(n) for n in engine_names() if n != engine]
        said = r["safe_user_message"].replace(engine_label(engine), "")
        assert not any(o in said for o in others), r["safe_user_message"]


def test_a_comparison_lists_the_engines_that_cannot_take_the_file_as_needing_another():
    from meshpipeline.agents.intake.recommendation import recommend_compatible_engines
    recs = recommend_compatible_engines("internal_cfd", "fluid-domain", "3D",
                                        patches=list(_PATCHES["internal"]), authorized=True,
                                        geometry_form="surface")
    assert recs["compatible_engines"] == []
    assert set(recs["needs_other_input"]) >= {"snappy", "cfmesh", "vmtk"}
    cad_recs = recommend_compatible_engines("internal_cfd", "fluid-domain", "3D",
                                            patches=list(_PATCHES["internal"]), authorized=True,
                                            geometry_form="cad")
    verdicts = {c["engine"]: c["verdict"] for c in cad_recs["candidates"]}
    # from a CAD solid they can (VMTK's own wall-layer question is still to ask: incomplete)
    assert verdicts["snappyHexMesh"] == "supported" and verdicts["VMTK"] != "impossible"


def _intake(form: str, declared=("internal_cfd", "fluid-domain")):
    from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor
    ref = SimpleNamespace(suffix_hint="." + FILE_OF[form].split(".")[-1],
                          original_filename=FILE_OF[form])
    st = IntakeExecutionState(session_id="s", owner_id="o", revision="r1", user_msg_count=1,
                              latest_user_msg="aorta, internal flow", user_texts=(),
                              declared_case=declared, source_ref=ref)
    return IntakeToolExecutor(state=st, job_id="j", implemented_engines=engine_names(),
                              search_tool=None)


def test_the_intake_never_proposes_an_engine_that_cannot_take_the_upload():
    # the aorta STL was proposed VMTK ("built for branching tubular lumens") - which cannot take
    # an STL. Now no engine is proposed, and the model is handed the way on instead.
    ex = _intake("surface")
    for shown in ("VMTK", "snappyHexMesh", "cfMesh"):
        out = asyncio.run(ex.run("propose_engine_selection", {"engine": shown, "reason": "fits"}))
        assert out.accepted is False
        assert "do not propose any engine" in out.content
        assert "STEP or IGES" in out.content
    assert ex.state.selection is None


def test_the_intake_still_proposes_an_engine_that_takes_the_upload():
    ex = _intake("cad")
    out = asyncio.run(ex.run("propose_engine_selection", {"engine": "VMTK", "reason": "fits"}))
    assert out.accepted is True and ex.state.selection["engine"] == "vmtk"


def test_the_proposer_reads_each_engines_declared_takes_and_delivers_lines():
    from meshpipeline.engines.registry import catalog_menu
    menu = catalog_menu()
    for name in engine_names():
        assert cap.takes_line(get_spec(name)) in menu
        assert cap.delivers_line(get_spec(name)) in menu
    assert ("Takes: external flow from a CAD solid or a surface mesh; internal flow from a CAD "
            "solid.") in menu
    assert "Delivers: hex-dominant cells, a staircased wall, no reliable near-wall prism" in menu


# the run: a refused-by-design file ends before anything is built, and says so

@pytest.fixture
def run_nodes(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    import meshpipeline.pipeline.executor as ex
    import meshpipeline.pipeline.geometry_admission as ga

    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    install(monkeypatch, _ep)
    monkeypatch.setattr(ex, "execution_publisher", _ep.execution_publisher)

    def _no_staging(*_a, **_k):
        raise AssertionError("a refused-by-design input was staged")
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _no_staging)
    return ga, ex


def _refused_run(ga, ex, tmp_path, engine: str, flow: str, form: str,
                 **kw) -> tuple[dict, str, dict]:
    """Admission, the executor's short-circuit, the ladder's closing record and the closing
    message - the path a refused-by-design run takes, with nothing mocked but the stream."""
    from tests._geometry_support import geometry_state

    from meshpipeline.application import final_result as fr
    from meshpipeline.pipeline import engine_fallback as lad
    from meshpipeline.pipeline.graph import route_after_executor
    st = _state(engine, flow, form, geometry=geometry_state(tmp_path, filename=FILE_OF[form]),
                **kw)
    st.update(asyncio.run(ga.node_geometry_admission(st)))
    st.update(asyncio.run(ex.node_executor(st)))
    assert route_after_executor(st) == "__end__", "a refused-by-design input was retried"
    rec = lad.final_record(st, succeeded=False, system_failure=False)
    result = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine=engine,
        purpose=st["purpose"], dimensionality="3D", approved_snapshot_id="",
        executor_success=False, reviewer_verdict="", failed_gate=st["executor_failed_gate"],
        api_failure="", attempts=int(st["retry_count"]), attempts_max=3, required_ready=False,
        delivered_types=[], optional_warnings=[],
        failure_cause=st["executor_failure_cause"], failure_facts=st["executor_failure_facts"])
    return st, fr.render_message(fr.with_engine_ladder(result, rec)), rec


@pytest.mark.parametrize("engine,flow,form", [
    c for c in _cells() if (c[0], c[1]) in EXPECTED and c[1] in ("external", "internal")
    and c[2] not in EXPECTED[(c[0], c[1])]])
def test_a_refused_by_design_run_says_why_builds_nothing_and_gives_the_way_on(
        run_nodes, tmp_path, engine, flow, form):
    ga, ex = run_nodes
    st, text, rec = _refused_run(ga, ex, tmp_path, engine, flow, form)
    label = engine_label(engine)
    assert st["executor_failed_gate"] == "geometry"
    assert st["executor_failure_cause"] == "geometry_rejected"
    assert f"{label} cannot mesh {cap.flow_words(flow)} from a surface mesh" in text
    assert "Nothing was built" in text
    assert "We did not try again" in text
    for lie in ("crash", "stopped before", "on our side", "Mesh built"):
        assert lie not in text, f"{lie!r} in the message for a refusal by design:\n{text}"
    # the attempt that never built is recorded as attempt 0, refused before building
    assert rec["attempts"] == [{"attempt": 0, "engine": engine, "kind": "engine",
                                "cause": "geometry_rejected",
                                "reason": f"it cannot mesh {cap.flow_words(flow)} from a "
                                          "surface mesh",
                                "refused_before_building": True}]
    from meshpipeline.pipeline import engine_fallback as lad
    # who can take THIS request (its boundaries and geometry kind too) from this file: the
    # ladder's own answer, recorded in the facts the message is told from
    able = lad.able_engines(st, exclude=(engine,))
    assert st["executor_failure_facts"]["able"] == able
    assert all(form in EXPECTED[(e, flow)] for e in able)
    offer = rec["offer"]
    if able:
        # the out is an engine that takes this file - and only such an engine
        assert offer["kind"] == "engine" and offer["engine"] in able
    else:
        # no engine takes it: the out is the file
        assert offer["kind"] == "input"
        assert "STEP or IGES" in offer["text"] and "upload" in offer["text"]
    for other in engine_names():
        if other != engine and form not in EXPECTED.get((other, flow), set()):
            assert f"use {engine_label(other)}" not in text, (
                f"{engine_label(other)} offered for a file it cannot take:\n{text}")


def test_the_aorta_message_end_to_end(run_nodes, tmp_path):
    # jobs d20ad762 / 26f5429a as they now end: the user named snappyHexMesh, confirmed the STL as
    # the fluid volume itself, internal flow. Before: "Mesh built", "the mesher stopped before it
    # finished... usually on our side", an identical second attempt, and an offer of VMTK.
    ga, ex = run_nodes
    _st, text, rec = _refused_run(ga, ex, tmp_path, "snappy", "internal", "surface",
                                  input_kind="fluid-domain")
    assert text == (
        "Mesh generation did not complete successfully.\n"
        "snappyHexMesh cannot mesh internal flow from a surface mesh (STL or a similar triangle "
        "file): for internal flow it needs a CAD solid (STEP or IGES). Nothing was built, and no "
        "attempt was used.\n"
        "No downloadable mesh deliverable is available.\n"
        "We did not try again: another attempt would hit the same problem.\n"
        "No engine here can mesh internal flow from a surface mesh yet - snappyHexMesh, VMTK and "
        "Gmsh take a CAD solid (STEP or IGES). Export the fluid region itself as a STEP or IGES "
        "solid from your CAD tool and upload it in this chat - that file can be meshed.")
    assert "VMTK can mesh it" not in text
    assert rec["attempts"][0]["refused_before_building"] is True


def test_a_file_the_engine_takes_is_not_refused_on_its_form(run_nodes, tmp_path):
    from tests._geometry_support import geometry_state
    ga, _ex = run_nodes
    st = _state("snappy", "external", "surface",
                geometry=geometry_state(tmp_path, filename="car.stl"))
    assert asyncio.run(ga.node_geometry_admission(st)) == {}
