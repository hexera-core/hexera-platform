# Responsibility: Verify the reply after an unrepaired refusal may be the model's own words but never a loosening of the finding.
# Boundaries: the reply guard - which combinations are impossible is the admission suite's proof.
from __future__ import annotations

from meshpipeline.agents.intake import refusal
from meshpipeline.agents.intake.validation import preview_admission

_FIVE = [{"name": n, "type": "wall"} for n in
         ("fuselage", "wing", "horizontal_tail", "nacelles", "pylons")] + \
        [{"name": "farfield", "type": "farfield"}]

_ACCEPTABLE = (
    "snappyHexMesh wraps a body's surface in a fluid domain and fills the fluid around it, so it "
    "cannot mesh a flat sheet - there is no fluid region around a sheet to fill. Nothing was "
    "changed. Which part would you like to revise?"
)


def _facts(engine="snappy", input_kind="planar-domain"):   # a flat sheet: snappyHexMesh cannot wrap one (a solid body it can - it is a body surface to a flow engine)
    return preview_admission(engine, "external_cfd", input_kind,
                             dimensionality="3D", patches=_FIVE)


# what a reply must satisfy before anyone reads it

def test_a_faithful_reply_is_accepted():
    assert refusal.check(_ACCEPTABLE, _facts()) == []


def test_naming_any_other_engine_is_refused():
    problems = refusal.check(_ACCEPTABLE + " You could try cfmesh.", _facts())
    assert any("another engine" in p for p in problems), problems


def test_an_engine_named_by_its_display_name_is_refused():
    # Messages reach the user in display names, so a leak will be spelled "cfMesh", not "cfmesh".
    problems = refusal.check(_ACCEPTABLE + " You could try cfMesh.", _facts())
    assert any("another engine" in p for p in problems), problems


def test_a_reply_need_not_list_the_declared_values():
    # Requiring every declared value be repeated forced the model to print a list it had already
    # been handed, which is what made these replies read like a form. The values are preserved in
    # the record and re-checked at submission, so the prose does not have to carry them.
    assert refusal.check(_ACCEPTABLE, _facts()) == []
    assert "fuselage" not in _ACCEPTABLE


def test_a_reply_that_asks_nothing_is_refused():
    statement = _ACCEPTABLE.replace("Which part would you like to revise?", "")
    assert any("asks the user nothing" in p for p in refusal.check(statement, _facts()))


def test_an_empty_or_overlong_reply_is_refused():
    assert refusal.check("", _facts()) == ["empty"]
    assert any("too long" in p for p in refusal.check("snappy " + "x" * refusal.MAX_CHARS, _facts()))


# what actually reaches the user

def test_an_acceptable_reply_is_delivered_as_written():
    out = refusal.settle(_ACCEPTABLE, _facts())
    assert out.source == "composed" and out.text == _ACCEPTABLE


def test_a_reply_with_a_proposed_revision_is_the_point_and_passes():
    # The model may put the finding to the user with the one change that would pass, so that
    # "ok" answers it. That is a question, and it names only the selected engine.
    proposed = ("Your file is one unnamed body, so snappyHexMesh can write one wall patch for it, "
                "not five. I would call that patch 'aircraft' and keep the farfield - ok, or tell "
                "me which part you want to revise?")
    out = refusal.settle(proposed, _facts("snappy", "body-surface"))
    assert out.source == "composed" and out.text == proposed


def test_every_failure_falls_back_to_the_rendered_finding():
    facts = _facts()
    rendered = facts["safe_user_message"]
    for name, reply in (("names another engine", _ACCEPTABLE + " Try cfmesh."),
                        ("asks nothing", "It cannot do that. Nothing was changed."),
                        ("empty reply", ""),
                        ("no reply at all", None)):
        out = refusal.settle(reply, facts)
        assert out.source == "rendered", name
        assert out.text == rendered, name


def test_the_rendered_finding_itself_passes_the_checks():
    # The fallback must never be something the guard would itself reject.
    facts = _facts()
    assert refusal.check(facts["safe_user_message"], facts) == []


def test_the_module_calls_no_model():
    # The reply is written in the loop, where the model holds the finding as a tool result. A
    # separate composing call here would be a second place to leak an engine name from.
    import inspect

    src = inspect.getsource(refusal)
    for forbidden in ("provider_call", "call_intake_model", "async def"):
        assert forbidden not in src, forbidden
