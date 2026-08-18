# Responsibility: Verify a refusal may be reworded for the user but never loosened on the way.
# Boundaries: the paraphrase seam - which combinations are impossible is the admission suite's proof.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from meshpipeline.agents.intake import refusal
from meshpipeline.agents.intake.validation import preview_admission

_FIVE = [{"name": n, "type": "wall"} for n in
         ("fuselage", "wing", "horizontal_tail", "nacelles", "pylons")] + \
        [{"name": "farfield", "type": "farfield"}]

_ACCEPTABLE = (
    "snappyHexMesh wraps the whole body in one wall patch, so it cannot give you the separate "
    "named surfaces you asked for - which means no per-component forces from this setup. Nothing "
    "was changed. Which part would you like to revise?"
)


def _facts(engine="snappy", input_kind="solid-body"):
    return preview_admission(engine, "external_cfd", input_kind,
                             dimensionality="3D", patches=_FIVE)


def _provider(text=None, *, boom=False, seen=None, tokens=(11, 7)):
    async def _call(**kw):
        if seen is not None:
            seen.update(kw)
        if boom:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(assistant_text=text,
                               input_tokens=tokens[0], output_tokens=tokens[1])
    return _call


# what a paraphrase must satisfy before anyone reads it

def test_a_faithful_paraphrase_is_accepted():
    assert refusal.check(_ACCEPTABLE, _facts()) == []


def test_naming_any_other_engine_is_refused():
    problems = refusal.check(_ACCEPTABLE + " You could try cfmesh.", _facts())
    assert any("another engine" in p for p in problems), problems


def test_an_engine_named_by_its_display_name_is_refused():
    # Messages reach the user in display names, so a leak will be spelled "cfMesh", not "cfmesh".
    problems = refusal.check(_ACCEPTABLE + " You could try cfMesh.", _facts())
    assert any("another engine" in p for p in problems), problems


def test_a_paraphrase_need_not_list_the_declared_values():
    # Requiring every declared value be repeated forced the model to print a list it had already
    # been handed, which is what made these replies read like a form. The values are preserved in
    # the record and re-checked at submission, so the prose does not have to carry them.
    assert refusal.check(_ACCEPTABLE, _facts()) == []
    assert "fuselage" not in _ACCEPTABLE


def test_a_paraphrase_that_asks_nothing_is_refused():
    statement = _ACCEPTABLE.replace("Which part would you like to revise?", "")
    assert any("asks the user nothing" in p for p in refusal.check(statement, _facts()))


def test_an_empty_or_overlong_paraphrase_is_refused():
    assert refusal.check("", _facts()) == ["empty"]
    assert any("too long" in p for p in refusal.check("snappy " + "x" * refusal.MAX_CHARS, _facts()))


# what actually reaches the user

def test_an_acceptable_paraphrase_is_delivered():
    out = asyncio.run(refusal.explain(
        _facts(), provider_call=_provider(_ACCEPTABLE), job_id="j", user_id="u"))
    assert out.source == "paraphrased" and out.text == _ACCEPTABLE


def test_every_failure_falls_back_to_the_rendered_message():
    facts = _facts()
    rendered = facts["safe_user_message"]
    for name, provider in (("names another engine", _provider(_ACCEPTABLE + " Try cfmesh.")),
                           ("provider failed", _provider(boom=True)),
                           ("empty reply", _provider("")),
                           ("no reply field", _provider(None))):
        out = asyncio.run(refusal.explain(
            facts, provider_call=provider, job_id="j", user_id="u"))
        assert out.source == "rendered", name
        assert out.text == rendered, name


def test_the_paraphrase_call_is_offered_no_tool():
    # It composes one message and nothing else. A tool here could submit, select or dispatch on a
    # setup the application has just refused.
    seen: dict = {}
    asyncio.run(refusal.explain(_facts(), provider_call=_provider(_ACCEPTABLE, seen=seen),
                                job_id="j", user_id="u"))
    assert seen.get("tools") == []
    assert seen.get("tool_choice") == "none"


def test_the_call_is_reported_even_when_its_wording_is_discarded():
    # Data collection must see what a refusal actually cost. The call is billed whether or not its
    # wording survives, so a discarded paraphrase that reported nothing would teach the corpus that
    # refusals are free.
    facts = _facts()
    kept = asyncio.run(refusal.explain(facts, provider_call=_provider(_ACCEPTABLE),
                                       job_id="j", user_id="u"))
    assert (kept.input_tokens, kept.output_tokens) == (11, 7)

    binned = asyncio.run(refusal.explain(
        facts, provider_call=_provider(_ACCEPTABLE + " Try cfmesh."), job_id="j", user_id="u"))
    assert binned.source == "rendered"
    assert (binned.input_tokens, binned.output_tokens) == (11, 7), "a discarded call reported nothing"

    # A call that never happened spends nothing.
    failed = asyncio.run(refusal.explain(facts, provider_call=_provider(boom=True),
                                         job_id="j", user_id="u"))
    assert (failed.input_tokens, failed.output_tokens) == (0, 0)


def test_the_request_carries_the_finding_and_every_declared_value():
    facts = _facts()
    body = refusal.request_messages(facts)[0]["content"]
    assert facts["capability_reason"][:40] in body
    for patch in facts["preserved_declared_values"]["patches"]:
        assert patch["name"] in body, patch["name"]
