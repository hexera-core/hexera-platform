"""We correct a guess we made on the customer's behalf, and the only place that said so was the mesh.

The intake default proposes every unplaced mouth as an outlet, the customer says "you decide everything",
and `geometry_survey._the_proposal_recorded` stores OUR reading under THEIR name with the note "accepted
the setup the application proposed". MEASURED over 24 driven conversations: all 24 reached a mesh and the
geometry agent planned on only 19, every one of the five refusals was `grounding_rejected`, and all five
were one family of part - a solid block with channels through it. Two of the five were ours, refused by
`contract.given.check_plan` with "the plan calls o12 'wall' and the customer confirmed 'outlet'" against a
row whose three role answers all read `accepted the setup the application proposed`.

A sibling change makes a role of OURS correctable by the agent. That is right and only half right on its
own: the owner's rule is "whatever the human says is final if he says smth wrong u can correct him", and
correcting him silently is not correcting him. So the pre-dispatch confirmation names what changed.

EVERY TEST HERE DRIVES THE SHIPPED `_do_submit_requirements` and reads the summary it composed. The engine
swap's tests pin a four-line copy of the composition instead, and a reviewer caught that today: a test of a
reimplementation proves nothing about the screen the customer reads.

The harness leaves `survey_armed` False, so the three submission gates no-op exactly as they do with no
measurement, and `st.geometry_survey` is the field they would otherwise have refreshed - it is the same
field, set to the same shape, that `_geometry_step_gate` assigns a few lines above the composition and that
`_agent_words` already reads for the agent's findings.

IT DOES CARRY A MEASUREMENT (`_DOC`), because the composition now has to know which submitted port each
mouth is before it will say anything about one: the announcement is rendered from the corrections that were
carried into the payload, and a mouth whose port cannot be identified is not corrected and not announced.
"""
from __future__ import annotations

import asyncio
import inspect

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake import executor as ex
from meshpipeline.application import geometry_survey as gs

_MSGS = [{"role": "user", "content": "internal CFD on this block, gmsh, you decide everything"}]
_REV = at.revision_of(_MSGS)
_PATCHES = [{"name": "in", "type": "inlet", "diameter_mm": 40},
            {"name": "out", "type": "outlet", "diameter_mm": 60},
            {"name": "w", "type": "wall"}]
#: A measurement the two declared ports bind to and the corrected mouths do NOT. `bind_patches` is how the
#: composition learns which mouth each submitted port is, and it binds a port that states no position by its
#: bore and only when exactly one opening fits - so the two rows with a `bore_diameter_m` are "in" and "out",
#: and every other mouth here carries no bore and can therefore be claimed by no declared port. That is the
#: real shape of the measured defect: the mouths the agent re-reads as solid faces are mouths intake proposed
#: a role for and submitted no port for, so the announcement is honoured with nothing to rewrite. The payload
#: rewrite itself is driven end to end in
#: test_a_role_correction_we_announce_is_the_role_the_mesh_gets.py.
_DOC = {"status": "ok", "bbox": {"diagonal_m": 1.0},
        "openings": [{"id": "oin", "bore_diameter_m": 0.040}, {"id": "oout", "bore_diameter_m": 0.060},
                     *({"id": f"o{n}"} for n in (1, 2, 3, 4, 5, 6, 7, 12, 14, 16,
                                                 21, 22, 23, 24, 25, 26, 27))]}
_PARAMS = {"element_order": "2"}
_CANON = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D", _PATCHES, _PARAMS)
_SUBMIT = {"domain": "a block with channels through it", "request_txt": "x" * 120,
           "review_brief_txt": "y" * 90, "dimensionality": "3D", "purpose": "internal_cfd",
           "input_kind": "fluid-domain", "mesh_engine": "gmsh", "mesh_fidelity": "standard",
           "engine_source": "user_direct", "engine_params": _PARAMS, "patches": _PATCHES}

#: The real reason off a real rejection on one of these parts, as the agent wrote it.
_WHY = ("patch o12 is the end face of an inner body (a centre body) inside the wall shell; it is an "
        "obstacle face, not a port; the port is the ring's bore around it")


def _answer(mouth: str, role: str, *, ours: bool, retired: bool = False) -> dict:
    """One stored role answer. The note is read from the module, never restated: a second spelling of it
    here and the whole block would empty out silently while this file still passed."""
    row = {"question_id": "role_1", "about": "opening.role", "answered_by": gs.CUSTOMER,
           "subject": mouth, "value": role, "option": role, "at": "2026-09-26T00:00:00Z",
           "words": "you decide everything" if ours else f"{mouth} is the {role}",
           "note": gs.ACCEPTED_THE_PROPOSAL if ours else ""}
    if retired:
        row["retired"] = True
    return row


def _patch(mouth: str, role: str, why: str = "") -> dict:
    """One plan patch, the shape `agent.schema.Patch` dumps: id, name, role, confidence, evidence."""
    return {"id": mouth, "name": f"{role}_{mouth}", "role": role, "confidence": 0.8,
            "evidence": [why] if why else []}


def _survey(answers: list[dict], patches: list[dict] | None) -> dict:
    row: dict = {"answers": answers}
    if patches is not None:
        row["geometry_step"] = {"status": "planned", "plan": {"patches": patches}}
    return row


def _summary(survey: dict | None) -> tuple[str, dict]:
    """The summary the shipped submission composed, and the executor state it left behind."""
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision=_REV)
    tok = at.issue(session_id="s", owner_id="u", revision=_REV, canonical=_CANON,
                   verdict="supported", mode=at.SELECTED, selection_id=sel["id"])
    st = ex.IntakeExecutionState(owner_id="u", session_id="s", revision=_REV, user_msg_count=1,
                                 selection=sel, pending=tok, geometry_survey=survey,
                                 geometry_document=_DOC)
    exe = ex.IntakeToolExecutor(state=st, job_id="j", implemented_engines=["gmsh"], search_tool=None)
    out = asyncio.run(exe._do_submit_requirements({**_SUBMIT, "preview_token": tok["token"]}))
    assert out.accepted, out.content
    return str(st.submit_summary), st.__dict__


# -------------------------------------------------------------------------------------------------
# the change is named, with the reason the agent gave
# -------------------------------------------------------------------------------------------------

def test_a_role_we_proposed_and_the_agent_changed_is_named_with_the_agents_own_reason():
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=True)],
                               [_patch("o12", "wall", _WHY), _patch("o1", "inlet")]))
    assert ex.ROLES_I_CHANGED_HEAD in said
    assert "o12: I proposed outlet, and it is wall" in said, said
    assert "end face of an inner body" in said, "a change with no reason is not an explanation"


def test_it_is_read_above_the_ask_and_below_the_setup_it_corrects():
    """The faces line says what each mouth is being submitted as; this says which of those I moved. A
    correction under the sentence that points at the setup would point backwards, past the correction."""
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=True)],
                               [_patch("o12", "wall", _WHY)]))
    assert said.index("faces") < said.index(ex.ROLES_I_CHANGED_HEAD)
    assert said.index(ex.ROLES_I_CHANGED_HEAD) < said.index(at.CONFIRM_REQUIREMENTS_ASK)
    assert said.rstrip().endswith("Shall I proceed with mesh generation?")


def test_the_customer_still_reads_it_on_a_later_turn():
    """It must survive the turn: `ap.create` stores the composed summary on the approval, and that row is
    what the confirmation turn is judged against. A block only in memory is a block nobody reads."""
    said, state = _summary(_survey([_answer("o12", "outlet", ours=True)],
                                   [_patch("o12", "wall", _WHY)]))
    assert state["approval"]["summary"] == said
    assert ex.ROLES_I_CHANGED_HEAD in state["approval"]["summary"]


def test_every_changed_role_gets_its_own_line_and_nothing_else_does():
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=True),
                                _answer("o14", "outlet", ours=True),
                                _answer("o16", "outlet", ours=True)],
                               [_patch("o12", "wall", _WHY), _patch("o14", "wall", "o14 likewise"),
                                _patch("o16", "outlet", "o16 is the bore and stays the outlet")]))
    block = said.split(ex.ROLES_I_CHANGED_HEAD)[1].split(chr(10) * 2)[0]
    assert len([ln for ln in block.splitlines() if ln.strip()]) == 2, block
    assert "o16" not in block, "a role the agent agreed with is not a change"


def test_a_change_the_agent_gave_no_reason_for_is_still_named():
    """A fact that lies is worse than a missing one - and silence about the change is the worse half."""
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=True)], [_patch("o12", "wall")]))
    assert "o12: I proposed outlet, and it is wall" in said
    assert said.count(" - " + chr(10)) == 0 and "wall -" not in said


def test_more_changes_than_the_screen_carries_are_counted_rather_than_listed():
    mouths = [f"o{n}" for n in range(21, 28)]
    said, _ = _summary(_survey([_answer(m, "outlet", ours=True) for m in mouths],
                               [_patch(m, "wall", f"{m} is solid") for m in mouths]))
    block = said.split(ex.ROLES_I_CHANGED_HEAD)[1].split(chr(10) * 2)[0]
    assert len([ln for ln in block.splitlines() if ln.strip()]) == ex.ROLES_I_CHANGED_MAX + 1
    assert "and 2 more the same way" in block, block


# -------------------------------------------------------------------------------------------------
# a role the customer TYPED can never appear here
# -------------------------------------------------------------------------------------------------

def test_a_role_the_customer_typed_is_never_reported_as_changed():
    """A role they named is never silently changed and never will be. Reporting one as changed would be
    this code claiming we did the thing it exists to say we never do."""
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=False)],
                               [_patch("o12", "wall", _WHY)]))
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert "o12" not in said


def test_a_role_they_typed_over_our_proposal_leaves_with_the_retired_row():
    """`gs._corrected` retires our proposal when they place the mouth themselves, so the answer that stands
    for o12 is theirs and the block has nothing of ours to report."""
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=True, retired=True),
                                _answer("o12", "outlet", ours=False)],
                               [_patch("o12", "wall", _WHY)]))
    assert ex.ROLES_I_CHANGED_HEAD not in said


def test_a_retired_proposal_is_the_record_and_not_an_input_wherever_it_sits():
    """`gs._corrected`'s rule for that row is "Every reader goes through `live_answers`", and this reader
    obeys it: a retired answer binds nothing now, whatever its place in an append-only list.

    THE ORDER HERE IS CONSTRUCTED. `_corrected` retires and then appends, so it does not itself leave a
    retired row last for a mouth, and `carry_answers` retires in place. What this pins is the reader, not an
    ordering the writer is known to produce - read off `answers` instead, the superseded proposal comes back
    underneath a role the customer typed and their own word is reported to them as a change of ours.
    """
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=False),
                                _answer("o12", "outlet", ours=True, retired=True)],
                               [_patch("o12", "wall", _WHY)]))
    assert ex.ROLES_I_CHANGED_HEAD not in said


def test_the_last_live_answer_decides_whose_the_role_is_not_any_live_answer():
    """Both rows live, theirs last. Reading "any live row carrying the note" would find the proposal
    underneath and report their own word back to them as a change of ours."""
    said, _ = _summary(_survey([_answer("o12", "outlet", ours=True),
                                _answer("o12", "outlet", ours=False)],
                               [_patch("o12", "wall", _WHY)]))
    assert ex.ROLES_I_CHANGED_HEAD not in said


def test_a_skipped_or_defaulted_answer_is_nobody_speaking_and_proposes_nothing():
    rows = [{**_answer("o12", "outlet", ours=True), "skipped": True},
            {**_answer("o14", "outlet", ours=True), "answered_by": gs.DEFAULT_TAKEN}]
    said, _ = _summary(_survey(rows, [_patch("o12", "wall", _WHY), _patch("o14", "wall", _WHY)]))
    assert ex.ROLES_I_CHANGED_HEAD not in said


def test_the_note_that_marks_a_role_as_ours_has_exactly_one_writer():
    """The structural half of the guarantee above. This block can only ever name a role the application
    proposed because `_the_proposal_recorded` is the single place that writes the note it filters on; a
    role the customer typed lands through `_subject_and_value`, whose note on a role question is "". A
    second writer of that note would let a typed role through, which is why this fails on one appearing -
    and it fails for a reason the assertions above cannot see, on a file this change does not own.
    """
    src = inspect.getsource(gs)
    assert src.count('"note": ACCEPTED_THE_PROPOSAL') == 1, (
        "something else now writes the note that means 'the application proposed this', so a role the "
        "customer typed could be reported as one we changed")
    shape = inspect.getsource(gs._subject_and_value)
    assert "ACCEPTED_THE_PROPOSAL" not in shape, "a typed role must carry no note of ours"


# -------------------------------------------------------------------------------------------------
# nothing changed: the screen is what it is today, to the byte
# -------------------------------------------------------------------------------------------------

def test_a_plan_that_agrees_with_us_reads_exactly_as_the_screen_does_today():
    agreed, _ = _summary(_survey([_answer("o12", "outlet", ours=True)], [_patch("o12", "outlet", _WHY)]))
    none_at_all, _ = _summary(None)
    assert agreed == none_at_all, "an agreeing plan changed the screen"
    assert ex.ROLES_I_CHANGED_HEAD not in agreed


def test_a_step_that_never_planned_says_nothing_rather_than_an_empty_heading():
    unplanned, _ = _summary(_survey([_answer("o12", "outlet", ours=True)], None))
    assert unplanned == _summary(None)[0]


def test_a_plan_whose_patches_are_not_a_list_is_silence_and_not_a_lost_turn():
    sel_free = {"answers": [_answer("o12", "outlet", ours=True)],
                "geometry_step": {"status": "planned", "plan": {"patches": "o12"}}}
    assert _summary(sel_free)[0] == _summary(None)[0]


# -------------------------------------------------------------------------------------------------
# the reason, cut to one line
# -------------------------------------------------------------------------------------------------

def test_a_long_reason_is_cut_at_a_word_and_never_padded_with_one_we_made_up():
    assert ex.one_line_reason([]) == ""
    assert ex.one_line_reason(None) == ""
    assert ex.one_line_reason(["  ", "second one counts"]) == "second one counts"
    cut = ex.one_line_reason(["word " * 200])
    assert len(cut) <= ex.REASON_MAX + 3 and cut.endswith("...") and not cut.endswith(" ...")


def test_a_role_word_reaches_the_customer_as_the_row_spells_it():
    assert ex.role_words("closed_end") == "closed end"
    assert ex.role_words("outlet") == "outlet"
