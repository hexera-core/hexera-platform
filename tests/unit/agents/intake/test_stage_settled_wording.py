# Responsibility: Verify a run the stage (or the facts) already make valid is never refused for the model's choice of word.
# Boundaries: the executor's tool boundary and the real admission gate; the selection, the staged file's form and
# its facts stood in for. The same class as the FEA fixes in test_fea_part_kind.py (dev-areen, 12-13 Nov 2026).
from __future__ import annotations

import asyncio
import json

import pytest

import meshpipeline.agents.intake.executor as ex_mod
from meshpipeline.agents.intake.executor import (
    IntakeExecutionState,
    IntakeToolExecutor,
    engine_param_words,
    stage_part_kind,
)
from meshpipeline.agents.intake.validation import preview_admission
from meshpipeline.agents.intake.vocabulary import (
    ENGINE,
    INPUT_KIND,
    PURPOSE,
    dimensionality_key,
    normalize_tool_args,
    to_key,
)

EXTERNAL = [{"name": "car", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
INTERNAL = [{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
            {"name": "outlet", "type": "outlet"}]


def _executor(monkeypatch, *, engine: str, declared, form: str = "cad"):
    es = ex_mod.es
    sel = es.propose(engine, session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    sel = {**sel, "state": es.CONFIRMED, "confirmed_revision": "r1",
           "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
    st = IntakeExecutionState(session_id="s", owner_id="u", revision="r1", user_msg_count=1,
                              declared_case=declared, selection=sel)
    monkeypatch.setattr(IntakeToolExecutor, "_geometry_form", lambda self: form)
    monkeypatch.setattr(IntakeToolExecutor, "_geometry_facts", lambda self: {})
    monkeypatch.setattr(IntakeToolExecutor, "_source_patches", lambda self, eng: ())
    return st, IntakeToolExecutor(state=st, job_id="j", implemented_engines=[engine],
                                  search_tool=lambda *a, **k: "")


def _preview(ex, **args):
    res = asyncio.run(ex.run("preview_selected_admission", args))
    return res, json.loads(res.content.split("\n\n")[-1])


# THE STAGE'S KIND: what the user confirmed stands over the model's word, where the word was refused
@pytest.mark.parametrize("engine,declared,model_kind,patches,form", [
    # a car the air flows around, called "fluid-domain" by the model ("we mesh the fluid around it")
    ("snappy", ("external_cfd", "solid-body"), "fluid-domain", EXTERNAL, "cad"),
    ("cfmesh", ("external_cfd", "body-surface"), "fluid-domain", EXTERNAL, "surface"),
    # a CAD solid OF THE FLUID itself, called "solid-body" by the model - Gmsh takes only the fluid
    ("gmsh", ("internal_cfd", "fluid-domain"), "solid-body", INTERNAL, "cad"),
    ("gmsh", ("internal_cfd", "fluid-domain"), "body-surface", INTERNAL, "cad"),
    # a confirmed assembly (thermal panel, or the stage's multi-region sentence), called a solid body
    ("snappy_multiregion", ("conjugate_heat_transfer", "solid-assembly"), "solid-body", INTERNAL, "cad"),
    ("snapgrid", ("conjugate_heat_transfer", "solid-assembly"), "solid-body", [], "cad"),
    # a CFD request on a part of several bodies, called an assembly by the model (the knee-implant word)
    ("snappy", ("external_cfd", "solid-body"), "solid-assembly", EXTERNAL, "cad"),
    ("snappy", ("internal_cfd", "fluid-domain"), "solid-assembly", INTERNAL, "cad"),
])
def test_a_kind_the_stage_settled_is_not_refused_for_the_models_word(monkeypatch, engine, declared,
                                                                     model_kind, patches, form):
    purpose, kind = declared
    before = preview_admission(engine, purpose, model_kind, dimensionality="3D", patches=patches,
                               geometry_form=form)
    assert before["verdict"] == "impossible", before          # the reproduction: refused today
    st, ex = _executor(monkeypatch, engine=engine, declared=declared, form=form)
    res, out = _preview(ex, selected_engine=engine, purpose=purpose, input_kind=model_kind,
                        dimensionality="3D", patches=patches)
    assert out["verdict"] != "impossible", out
    assert st.admission_refusal is None
    assert "as the user confirmed on the geometry stage" in res.content


def test_the_submission_reads_the_same_kind_so_the_token_matches(monkeypatch):
    st, ex = _executor(monkeypatch, engine="snappy", declared=("external_cfd", "solid-body"))
    _res, out = _preview(ex, selected_engine="snappy", purpose="external_cfd", input_kind="fluid-domain",
                         dimensionality="3D", patches=EXTERNAL, engine_params={})
    assert out["verdict"] == "supported", out
    assert st.pending["canonical"]["input_kind"] == "solid-body"
    sub = asyncio.run(ex.run("submit_requirements", {
        "mesh_engine": "snappy", "purpose": "external_cfd", "input_kind": "fluid-domain",
        "dimensionality": "3D", "patches": EXTERNAL, "engine_params": {},
        "engine_source": "suggested_confirmed", "domain": "car external aerodynamics",
        "request_txt": "External aerodynamics of the uploaded car body in a far-field box, one wall "
                       "patch for the car and one far field, a full 3D mesh at the standard detail.",
        "review_brief_txt": "The car surface is captured cleanly, the far field is far enough away, "
                            "and the cells near the body are of good quality.",
        "preview_token": out["preview_token"]}))
    assert "NOT authorized" not in sub.content and "rejected" not in sub.content, sub.content
    assert sub.accepted, sub.content


def test_a_kind_the_engine_takes_is_never_replaced():
    # the model's word passes: nothing is changed, whatever the stage said
    args = {"selected_engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface"}
    assert stage_part_kind(dict(args), ("external_cfd", "solid-body"), "cad") == (args, "")
    args = {"selected_engine": "snappy", "purpose": "internal_cfd", "input_kind": "solid-body"}
    assert stage_part_kind(dict(args), ("internal_cfd", "fluid-domain"), "cad") == (args, "")


def test_a_real_refusal_stands():
    # a CAD solid of the PART for internal flow: Gmsh needs the fluid itself, whatever it is called
    args = {"selected_engine": "gmsh", "purpose": "internal_cfd", "input_kind": "solid-body"}
    assert stage_part_kind(dict(args), ("internal_cfd", "solid-body"), "cad") == (args, "")
    assert preview_admission("gmsh", "internal_cfd", "solid-body", dimensionality="3D",
                             patches=INTERNAL, geometry_form="cad")["verdict"] == "impossible"
    # a surface for a multi-region case on the ECXML mesher: refused for the file, not the word
    args = {"selected_engine": "snapgrid", "purpose": "conjugate_heat_transfer", "input_kind": "solid-body"}
    out, _ = stage_part_kind(dict(args), ("conjugate_heat_transfer", "solid-assembly"), "surface")
    assert preview_admission("snapgrid", out["purpose"], out["input_kind"], dimensionality="3D",
                             geometry_form="surface")["verdict"] == "impossible"


def test_nothing_is_changed_without_a_standing_stage_declaration_or_for_another_purpose():
    args = {"selected_engine": "snappy", "purpose": "external_cfd", "input_kind": "fluid-domain"}
    # the user restated what the file is: declared_case is None, and the model's word is all there is
    assert stage_part_kind(dict(args), None, "cad") == (args, "")
    # the payload's purpose is not the stage's: a change of analysis is never overridden here
    assert stage_part_kind(dict(args), ("internal_cfd", "fluid-domain"), "cad") == (args, "")
    # no engine named: nothing to judge the kind against
    assert stage_part_kind({"purpose": "external_cfd", "input_kind": "fluid-domain"},
                           ("external_cfd", "solid-body"), "cad")[1] == ""


def test_without_a_stage_the_unsettled_refusal_still_reaches_the_model(monkeypatch):
    st, ex = _executor(monkeypatch, engine="snappy", declared=None)
    _res, out = _preview(ex, selected_engine="snappy", purpose="external_cfd", input_kind="fluid-domain",
                         dimensionality="3D", patches=EXTERNAL)
    assert out["verdict"] == "impossible"
    assert st.admission_refusal is not None


# DIMENSIONALITY: "3d" is "3D"
@pytest.mark.parametrize("said", ["3d", "3-D", " 3D ", "3D mesh", "three-dimensional", "3"])
def test_a_spelling_of_3d_is_3d(said):
    assert dimensionality_key(said) == "3D"
    assert preview_admission("snappy", "external_cfd", "solid-body", dimensionality="3d",
                             patches=EXTERNAL)["verdict"] == "malformed"     # the reproduction
    args = normalize_tool_args({"dimensionality": said})
    assert preview_admission("snappy", "external_cfd", "solid-body", patches=EXTERNAL,
                             dimensionality=args["dimensionality"])["verdict"] == "supported"


def test_dimensionality_words_that_mean_neither_are_left_for_the_gate():
    for said in ("2.5D", "axisymmetric", "", None):
        assert dimensionality_key(said) == (said.strip() if isinstance(said, str) else said)
    assert dimensionality_key("2d") == "2D"


# VOCABULARY: another spelling of a value is that value
@pytest.mark.parametrize("vocabulary,said,key", [
    (INPUT_KIND, "fluid domain", "fluid-domain"), (INPUT_KIND, "fluid_domain", "fluid-domain"),
    (INPUT_KIND, "Solid Assembly", "solid-assembly"), (PURPOSE, "external-cfd", "external_cfd"),
    (PURPOSE, "Internal CFD", "internal_cfd"), (PURPOSE, "FEA", "structural"),
    (PURPOSE, "conjugate-heat-transfer", "conjugate_heat_transfer"), (PURPOSE, "CHT", "conjugate_heat_transfer"),
    (ENGINE, "snappy hex mesh", "snappy"), (ENGINE, "snappy-multiregion", "snappy_multiregion"),
    (ENGINE, "Snap grid", "snapgrid"), (ENGINE, "cf-mesh", "cfmesh"),
])
def test_another_spelling_names_the_same_value(vocabulary, said, key):
    assert to_key(vocabulary, said) == key


def test_no_two_values_fold_to_one_spelling_and_unknowns_pass_through():
    from meshpipeline.agents.intake import vocabulary as v
    for name, table in ((PURPOSE, v._purposes()), (INPUT_KIND, v._input_kinds()), (ENGINE, v._engines())):
        folds: dict[str, str] = {}
        for display, key in table.items():
            for f in {v._fold(display), v._fold(key)}:
                assert folds.setdefault(f, key) == key, (name, f)
    assert to_key(INPUT_KIND, "wing") == "wing"
    assert to_key(PURPOSE, "acoustics") == "acoustics"


def test_a_structural_request_called_fea_keeps_the_part_rule():
    args = ex_mod.fea_part_kind(normalize_tool_args({"purpose": "FEA", "input_kind": "body surface"}))
    assert args == {"purpose": "structural", "input_kind": "solid-body"}


# ENGINE PARAMETERS: the engine's own word for a value written another way
@pytest.mark.parametrize("engine,params,want", [
    ("gmsh", {"element_order": "quadratic"}, {"element_order": "2"}),
    ("gmsh", {"element_order": 2.0}, {"element_order": "2"}),
    ("gmsh", {"element_order": "second-order"}, {"element_order": "2"}),
    ("gmsh", {"element_order": "linear"}, {"element_order": "1"}),
    ("vmtk", {"wall_layers": True}, {"wall_layers": "on"}),
    ("netgen", {"wall_layers": "no"}, {"wall_layers": "off"}),
    ("snappy_multiregion", {"fluid_topology": "Internal flow"}, {"fluid_topology": "internal"}),
    ("snappy", {"narrow_gaps": "within limit"}, {"narrow_gaps": "within_limit"}),
])
def test_a_parameter_value_in_other_words_is_the_engines_value(engine, params, want):
    purpose, kind = {"gmsh": ("structural", "solid-body"), "vmtk": ("internal_cfd", "fluid-domain"),
                     "netgen": ("internal_cfd", "fluid-domain"),
                     "snappy_multiregion": ("conjugate_heat_transfer", "solid-assembly"),
                     "snappy": ("internal_cfd", "fluid-domain")}[engine]
    patches = [] if purpose in ("structural", "conjugate_heat_transfer") else INTERNAL
    before = preview_admission(engine, purpose, kind, dimensionality="3D", patches=patches,
                               engine_params=params)
    assert before["verdict"] == "incomplete", before                 # the reproduction: held back
    out = engine_param_words({"selected_engine": engine, "engine_params": params})
    assert out["engine_params"] == want
    after = preview_admission(engine, purpose, kind, dimensionality="3D", patches=patches,
                              engine_params=out["engine_params"])
    assert after["verdict"] == "supported", after


def test_a_parameter_value_that_means_none_of_them_is_left_for_the_gate():
    args = {"mesh_engine": "gmsh", "engine_params": {"element_order": "3", "mystery": "x"}}
    assert engine_param_words(dict(args)) == args
    assert engine_param_words({"engine_params": {"element_order": "quadratic"}}) == \
        {"engine_params": {"element_order": "quadratic"}}      # no engine named: nothing to read
