"""The Surveyor compared the pictures with the measurement at every mouth and told the customer none of it.

`reconcile.verdict` has decided, per claim and per place, whether the look and the measurement agree since step
4 was wired into this platform's own composition. Every CONFIRMED verdict is a reading of pictures a measurement
of the same bytes did not contradict, and on the 40 stored looks in this deployment's database there are 479 of
them - 145 on what kind of opening a mouth is and 85 on how it sits in the wall - against 41 disputed and 18
suppressed. All 479 were spent on nothing: the panel printed the look's own adjectives and never once said the
two sources had been put beside each other, and the question that followed named every mouth alike.

THE OWNER'S RULE, verbatim: "the disagreement triggered asking, if there is no disagreement, say that and if
there is, do what your doing now."

WHAT MAY NOT MOVE, and it is the reason most of this file is about that instead. Making a role question stop
naming a mouth took a measured run from 3-of-3 submissions to 1-of-4, because `geometry_survey.role_problems`
refuses any port role the customer has not confirmed and a mouth nobody is asked about can never be confirmed.
So the role question still covers every unplaced mouth; what changed is which one the sentence leads with and
that the agreement is stated. `test_the_role_gate_is_untouched` and
`test_an_agreement_does_not_settle_a_role` are the guards.
"""
from __future__ import annotations

from tests._surveyor_package import require

from meshpipeline.agents.intake.geometry_brief import agreement_lines, surveyor_panel
from meshpipeline.application import geometry_survey as gs

# ---------------------------------------------------------------------------------------------------------------
# the row: the reconciler's verdicts, filtered and renamed, and nothing else
# ---------------------------------------------------------------------------------------------------------------

class _Place:
    """What `reconcile.verdict.by_place` hands back, in the shape the row reads it."""

    def __init__(self, verdict: str, fields=(), measured: str = "") -> None:
        self.verdict = verdict
        self._fields = list(fields)
        self.measured = measured
        self.disagreed = [_V(f) for f in self._fields] if verdict == "disagreed" else []

    @property
    def fields(self):
        return list(self._fields)


class _V:
    def __init__(self, field: str) -> None:
        self.look_field = field


class _Joint:
    def __init__(self, places: dict, readers: dict | None = None) -> None:
        self._places = places
        self.readers = readers or {"mouth_march": False}

    @property
    def agreement(self):
        return self._places


def _doc(*ids: str) -> dict:
    return {"status": "ok", "openings": [{"id": i} for i in ids]}


def test_the_row_says_which_mouths_agree_and_which_do_not():
    row = gs.agreement_row(_Joint({
        "o1": _Place("agreed", ["openings_seen[].looks_like"]),
        "o2": _Place("agreed", ["openings_seen[].mouth"]),
        "o4": _Place("disagreed", ["openings_seen[].mouth"], measured="planar_face, stub L/D=0.0"),
    }), _doc("o1", "o2", "o3", "o4"))
    assert row["schema"] == gs.AGREEMENT_SCHEMA
    assert set(row["agreed"]) == {"o1", "o2"}
    assert set(row["disagreed"]) == {"o4"}
    assert row["nothing_comparable"] == ["o3"], "a mouth with no place is a mouth nobody compared"
    assert row["agreed_total"] == 2 and row["disagreed_total"] == 1


def test_nothing_comparable_is_never_folded_into_the_agreement():
    """The one way this could publish a fact that lies. A mouth the look never named leaves no claim for the
    measurement to check, and reporting the absence as agreement is the same error `Joint.readers` exists to
    stop one channel up."""
    row = gs.agreement_row(_Joint({"o1": _Place("nothing_comparable")}), _doc("o1", "o2"))
    assert row["agreed"] == {} and row["disagreed"] == {}
    assert sorted(row["nothing_comparable"]) == ["o1", "o2"]
    assert "not the two sources agreeing" in row["read_as"]


def test_a_place_that_is_not_a_measured_mouth_is_not_shown():
    """`by_place` is keyed by whatever kind of place a claim was about - a mark letter, `knife`, an axis letter.
    The panel names mouths to a person, so the row keeps the ids in this document's own opening table. It is a
    filter on what is SHOWN and the counts are over what it shows."""
    row = gs.agreement_row(_Joint({
        "o1": _Place("agreed", ["openings_seen[].mouth"]),
        "A": _Place("disagreed", ["marks_seen[]"], measured="A stands on material"),
        "knife": _Place("agreed", ["sharp_edges"]),
    }), _doc("o1"))
    assert set(row["agreed"]) == {"o1"}
    assert row["disagreed"] == {}, "a mark letter is not a mouth the customer can be shown"


def test_the_channels_that_never_ran_travel_with_the_row():
    """A channel with no reader finds no disagreement, and the nought it produces is indistinguishable from the
    two sources agreeing. On this platform's path the axial profile always refuses: there are no triangles."""
    row = gs.agreement_row(_Joint({"o1": _Place("agreed", ["openings_seen[].mouth"])},
                                  readers={"axial_profile": False, "marks": True}),
                           _doc("o1"))
    assert row["channels_not_read"] == ["axial_profile"]


def test_no_joint_means_no_row_at_all():
    """A composition with no look, or one where step 4 could not run, is byte for byte the row it was."""
    assert gs.agreement_row(None, _doc("o1")) is None


def test_a_broken_joint_costs_the_row_and_never_the_composition():
    class _Raises:
        @property
        def agreement(self):
            raise RuntimeError("step 4 is broken on this part")
    assert gs.agreement_row(_Raises(), _doc("o1")) is None


class _Asked:
    def __init__(self, *places: str) -> None:
        self.questions = [_Q(list(places))] if places else []


class _Q:
    def __init__(self, places: list[str]) -> None:
        self.places = places


def test_a_disagreement_records_whether_a_question_actually_names_the_mouth():
    """WITHOUT THIS THE PANEL LIES. Measured over the 162 stored looks in this deployment, each composed for
    internal_cfd: 22 parts carry a mouth the two sources read differently, 79 mouths in all, and ZERO of the 79
    is a mouth the role question names - the look disagrees about the faces the builder never cuts (a shell's
    shoulder rings, a domain's own caps) and agrees about the ones it does. A line reading "so I am asking
    about that one" about a mouth nothing will ask about is the fact-that-lies shape."""
    places = {"o1": _Place("disagreed", ["openings_seen[].mouth"], measured="planar_face, stub L/D=0.0"),
              "o9": _Place("disagreed", ["openings_seen[].mouth"], measured="planar_face, stub L/D=0.0")}
    row = gs.agreement_row(_Joint(places), _doc("o1", "o9"), _Asked("o1"))
    assert row["disagreed"]["o1"]["asked"] is True
    assert row["disagreed"]["o9"]["asked"] is False
    said = " ".join(agreement_lines(_survey(row)))
    assert "reads o1 differently" in said and "so I am asking about that one" in said
    assert "no question of mine covers them" in said


def test_the_mouths_no_question_covers_are_one_line_and_not_one_line_each():
    """Six promises of a question with nothing behind any of them is what a line each would have printed on
    the part measured above. The asked ones get a line; the rest get a count, and still get said."""
    places = {f"o{n}": _Place("disagreed", ["openings_seen[].looks_like"], measured="cap/planar_face")
              for n in range(1, 8)}
    row = gs.agreement_row(_Joint(places), _doc(*[f"o{n}" for n in range(1, 8)]), _Asked())
    lines = [ln for ln in agreement_lines(_survey(row)) if "differently" in ln]
    assert len(lines) == 1, lines
    assert "o1, o2, o3, o4, o5, o6 and 1 more" in lines[0]
    assert "the mesher does not cut those faces" in lines[0]


def test_with_no_finder_answer_no_mouth_is_claimed_to_be_asked():
    """A row built without the finder's answer must not guess. Absence of a question is not a question."""
    row = gs.agreement_row(_Joint({"o1": _Place("disagreed", ["openings_seen[].mouth"])}), _doc("o1"))
    assert row["disagreed"]["o1"]["asked"] is False


def test_the_row_is_absent_from_a_survey_that_never_had_one():
    assert gs.agreement_of(None) == {}
    assert gs.agreement_of({}) == {}
    assert gs.agreement_of({"asking": {"agreement": {"schema": gs.AGREEMENT_SCHEMA}}})["schema"] == (
        gs.AGREEMENT_SCHEMA)


def test_the_agreement_survives_the_row_the_way_the_row_is_actually_stored():
    """THE ONE THAT WOULD HAVE MADE THIS CHANGE INVISIBLE FROM THE SECOND TURN ON.

    `geometry_surveys` has a COLUMN PER TOP-LEVEL STATE KEY and `state_of` rebuilds the state from those
    columns, so a new top-level key written by `compose` lives for the turn that composed it and is gone on
    the next read. The panel would have shown the agreement once, on the turn the survey was composed, and
    never again - and nothing would have failed. So the agreement rides inside `asking`, which round-trips
    whole, and this test reads the repository's own reader rather than trusting the arrangement.
    """
    from meshpipeline.persistence.models import GeometrySurvey
    from meshpipeline.persistence.repositories.geometry_survey_repository import state_of

    row = GeometrySurvey(sha256="a" * 64, facts_sha256="b" * 64, stage="surveyed", survey={},
                         asking={"schema": gs.ASKING_SCHEMA, "put": [], "agreement": AGREED_ROW},
                         composed_for={}, asked=[], answers=[], agent_git_sha="", look_queued="",
                         state_schema=gs.SURVEY_STATE_SCHEMA)
    back = state_of(row)
    assert gs.agreement_of(back)["agreed"] == AGREED_ROW["agreed"]
    assert "agreement" not in back, "a top-level key is not what the row stores"


# ---------------------------------------------------------------------------------------------------------------
# the panel: the saying half of the owner's rule
# ---------------------------------------------------------------------------------------------------------------

def _survey(row: dict | None, purpose: str = "internal_cfd") -> dict:
    """A survey row the way `compose` writes one: the agreement rides INSIDE `asking`, because the stored row
    has a column per top-level key and a new one is dropped on the next read (`gs.agreement_of`)."""
    out: dict = {"composed_for": {"purpose": purpose, "representation": "fluid_domain"}, "asking": {}}
    if row is not None:
        out["asking"] = {"schema": gs.ASKING_SCHEMA, "put": [], "agreement": row}
    return out


AGREED_ROW = {"schema": gs.AGREEMENT_SCHEMA,
              "agreed": {"o1": ["what kind of opening a mouth is"], "o2": ["how a mouth sits in the wall"]},
              "agreed_total": 2, "agreed_and_asked": ["o1", "o2"], "agreed_and_asked_total": 2,
              "disagreed": {}, "disagreed_total": 0,
              "nothing_comparable": [], "nothing_comparable_total": 0, "channels_not_read": []}

DISAGREED_ROW = {**AGREED_ROW,
                 "disagreed": {"o4": {"about": ["how a mouth sits in the wall"],
                                      "measured": "planar_face, stub L/D=0.0, area_fraction=0.12",
                                      "asked": True}},
                 "disagreed_total": 1}


def test_the_agreement_is_stated_as_a_positive_line():
    lines = agreement_lines(_survey(AGREED_ROW))
    assert any("agrees with the measurement about o1, o2" in ln for ln in lines)
    assert any("not asking what those are" in ln for ln in lines)


def test_a_disagreement_names_the_mouth_the_measurement_and_the_fact_it_is_asked_about():
    """The owner's own elbow: the look called a mouth protruding and the measurement had it flat. That is a
    question a person answers immediately, and the panel says which mouth it is about."""
    lines = agreement_lines(_survey(DISAGREED_ROW))
    said = " ".join(lines)
    assert "reads o4 differently from the measurement" in said
    assert "stub L/D=0.0" in said
    assert "asking about that one" in said


def test_the_panel_says_when_nothing_could_be_compared_rather_than_implying_agreement():
    row = {"schema": gs.AGREEMENT_SCHEMA, "agreed": {}, "disagreed": {},
           "nothing_comparable": ["o1", "o2"], "nothing_comparable_total": 2}
    said = " ".join(agreement_lines(_survey(row)))
    assert "could be checked" in said and "not agreement" in said
    assert "agrees with the measurement" not in said


def test_a_row_composed_before_this_existed_prints_nothing():
    assert agreement_lines(_survey(None)) == []
    assert agreement_lines(None) == []


def test_the_panel_carries_the_agreement_under_seen_and_not_under_measured():
    """MEASURED is arithmetic on the customer's bytes and is simply true. An agreement between a reading of
    pictures and a measurement is not arithmetic and not proof: `reconcile.verdict.STANDING` records that on the
    commoner direction of contradiction the MEASUREMENT was at fault 30 times in 33. So it goes under the
    section headed as a reading."""
    panel = surveyor_panel(
        {"status": "ok", "unit": {"declared": "mm"}, "bbox": {"extent_m": [1.0, 0.4, 0.3]},
         "openings": [{"id": "o1"}, {"id": "o2"}],
         "look": {"status": "ok", "seconds": 18.0,
                  "impression": {"looks_like": "an elbow", "openings_seen": [{"id": "o1"}, {"id": "o2"}]}},
         "facts": {"cell_estimates": [{"tier": "standard", "cells": 1_000_000}]}},
        _survey(AGREED_ROW))
    assert "agrees with the measurement about o1, o2" in panel
    seen, suggests = panel.index("SEEN ("), panel.index("SUGGESTS (")
    assert seen < panel.index("agrees with the measurement") < suggests
    assert panel.index("MEASURED (") < seen


def test_the_panel_says_why_a_role_is_still_asked_where_the_two_sources_agree():
    """Nothing in a static geometry says which way flow goes, so agreement about WHAT a mouth is settles
    everything except the one thing the mesh needs. Saying so is what stops the question reading as an
    interrogation after a panel that claims the work was already done."""
    panel = surveyor_panel(
        {"status": "ok", "unit": {"declared": "mm"}, "bbox": {"extent_m": [1.0, 0.4, 0.3]},
         "openings": [{"id": "o1"}, {"id": "o2"}],
         "look": {"status": "ok", "impression": {"looks_like": "an elbow",
                                                 "openings_seen": [{"id": "o1"}, {"id": "o2"}]}},
         "facts": {}},
        _survey(AGREED_ROW))
    assert "nothing measures that, so it is the one question I put" in panel


# ---------------------------------------------------------------------------------------------------------------
# the trap next door
# ---------------------------------------------------------------------------------------------------------------

def _state_with_a_role_question(status: str) -> dict:
    """A survey row whose one role question is in `status`, built the way `question_views` reads one."""
    answers = []
    if status == "answered":
        answers = [{"question_id": "q_port_roles", "answered_by": gs.CUSTOMER, "subject": "o1", "value": "inlet"},
                   {"question_id": "q_port_roles", "answered_by": gs.CUSTOMER, "subject": "o2",
                    "value": "outlet"}]
    elif status == "defaulted":
        answers = [{"question_id": "q_port_roles", "answered_by": gs.DEFAULT_TAKEN, "subject": "o1",
                    "value": "inlet"}]
    return {"answers": answers}


def _view(status: str) -> dict:
    return {"id": "q_port_roles", "about": "opening.role", "subjects": ["o1", "o2"], "default": "treat o1 as "
            "the inlet and the other unplaced mouths as outlets", "status": status}


def test_the_role_gate_is_untouched(monkeypatch):
    """`role_problems` must still refuse a port role the customer has not confirmed. Nothing in this change may
    soften it: what changed is WHICH question is put, never what counts as an answer."""
    monkeypatch.setattr(gs, "question_views", lambda state: [_view(state["_status"])])
    patches = [{"name": "inlet_1", "type": "inlet"}]
    for status in ("open", "skipped", "defaulted"):
        problems = gs.role_problems({"_status": status, "answers": []}, _doc("o1", "o2"), patches)
        assert problems, f"a {status} role question stopped refusing"
        assert "answer_survey_question" in problems[0]


def test_an_agreement_does_not_settle_a_role(monkeypatch):
    """The whole trap in one test. Even where the look and the measurement agree about every mouth, an
    unconfirmed role is still refused: an agreement about what a mouth IS is not a customer's word about what it
    is FOR, and treating it as one would put a boundary condition on a face nobody named."""
    monkeypatch.setattr(gs, "question_views", lambda state: [_view("open")])
    state = {**_state_with_a_role_question("open"), "agreement": AGREED_ROW}
    assert gs.role_problems(state, _doc("o1", "o2"), [{"name": "in", "type": "inlet"}])


def test_a_delegation_is_still_not_a_label():
    """`learn.ingest_platform.chose_it_themselves` is the one judge of that and this change does not touch it.
    Pinned here because the whole point of asking where the answer is worth having is to make real answers
    arrive, and it would be self-defeating to widen what counts as one."""
    ip = require("geometry_agent.learn.ingest_platform",
                 needs="the one judge of whether an answer is a label at all")
    verdict, by, _why = ip.chose_it_themselves(
        {"answered_by": "customer", "delegated": True, "words": "you decide everything"}, "o1", 2)
    assert verdict == "not_a_label" and by == "delegated"


def test_the_line_about_the_one_question_is_not_printed_when_no_question_is_put():
    """A part whose roles the customer's own port list already settled has no role question, and a panel
    promising one would be promising an ask that is not coming."""
    row = {**AGREED_ROW, "agreed_and_asked": [], "agreed_and_asked_total": 0}
    panel = surveyor_panel(
        {"status": "ok", "unit": {"declared": "mm"}, "bbox": {"extent_m": [1.0, 0.4, 0.3]},
         "openings": [{"id": "o1"}, {"id": "o2"}],
         "look": {"status": "ok", "impression": {"looks_like": "a bend",
                                                 "openings_seen": [{"id": "o1"}, {"id": "o2"}]}},
         "facts": {}},
        _survey(row))
    assert "agrees with the measurement about o1, o2" in panel
    assert "it is the one question I put" not in panel


def test_the_row_marks_which_agreed_mouths_a_question_names():
    row = gs.agreement_row(_Joint({"o1": _Place("agreed", ["openings_seen[].mouth"]),
                                   "o2": _Place("agreed", ["openings_seen[].mouth"])}),
                           _doc("o1", "o2"), _Asked("o1"))
    assert row["agreed_and_asked"] == ["o1"]
    assert row["agreed_and_asked_total"] == 1
