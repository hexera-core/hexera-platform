# Responsibility: Hold the purpose map to the phrase set - where each answer to "what are you
# meshing this for?" goes - and to its one-question rule.
# Boundaries: pure; no store, no model. `python tests/unit/application/test_purpose_map.py [origin]`
# prints the score per origin and every miss.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from purpose_phrases import PHRASES  # noqa: E402

from meshpipeline.application import purpose_map as pm  # noqa: E402


def label_of(reading: pm.PurposeReading) -> str:
    """The phrase-set label a reading earns: "fea_heat" for heat in a solid part, "ask" for a
    question, else its route."""
    if not reading.settled:
        return "ask"
    return "fea_heat" if reading.heat_in_solid else reading.route


def route_of(label: str) -> str:
    """The path a label takes: heat in a solid part is the FEA path."""
    return "fea" if label == "fea_heat" else label


def score(origin: str | None = None) -> tuple[int, int, int, list]:
    """(exact right, route right, total, misses) over the set, or one origin of it."""
    exact = route = total = 0
    misses = []
    for phrase, label, where in PHRASES:
        if origin and where != origin:
            continue
        total += 1
        got = label_of(pm.read_purpose(phrase))
        exact += got == label
        route += route_of(got) == route_of(label)
        if got != label:
            misses.append((where, phrase, label, got, pm.read_purpose(phrase).why))
    return exact, route, total, misses


def test_the_phrase_set_maps_to_the_right_path():
    # the whole set, exactly (heat in a solid told apart from structural): 95% or better
    exact, route, total, misses = score()
    assert exact / total >= 0.95, "\n".join(map(str, misses))
    assert route / total >= 0.95


def test_every_thermal_phrase_reaches_a_thermal_path_or_the_thermal_question():
    for phrase, label, _where in PHRASES:
        if label in ("electronics", "fea_heat"):
            assert label_of(pm.read_purpose(phrase)) in (label, "ask"), phrase


def test_an_ecxml_style_cooling_answer_never_goes_to_fea_and_heat_in_a_solid_never_to_cfd():
    assert pm.read_purpose("cooling of a chip with a fan").route == pm.ROUTE_ELECTRONICS
    assert pm.read_purpose("heat conduction through this solid").route == pm.ROUTE_FEA
    assert pm.read_purpose("heat conduction through this solid").heat_in_solid


def test_unclear_words_get_one_question_and_never_a_second():
    first = [{"role": "assistant", "content": "What are you meshing this for?"},
             {"role": "user", "content": "thermal"}]
    reading = pm.reading_for(first)
    assert pm.question_for(reading, first) == pm.THERMAL_QUESTION
    # answered with a button: settled
    clicked = first + [{"role": "assistant", "content": pm.THERMAL_QUESTION},
                       {"role": "user", "content": pm.THERMAL_CHOICES[1][1]}]
    r2 = pm.reading_for(clicked)
    assert r2.route == pm.ROUTE_FEA and r2.heat_in_solid
    clicked = first + [{"role": "assistant", "content": pm.THERMAL_QUESTION},
                       {"role": "user", "content": pm.THERMAL_CHOICES[0][1]}]
    assert pm.reading_for(clicked).route == pm.ROUTE_ELECTRONICS
    # answered with something still unclear: the generic path, no second question
    vague = first + [{"role": "assistant", "content": pm.THERMAL_QUESTION},
                     {"role": "user", "content": "not sure"}]
    r3 = pm.reading_for(vague)
    assert r3.route == pm.ROUTE_FLOW and pm.question_for(r3, vague) == ""


def test_the_domain_question_buttons_settle_it():
    first = [{"role": "user", "content": "simulation"}]
    assert pm.question_for(pm.reading_for(first), first) == pm.DOMAIN_QUESTION
    for (_label, send), route in zip(pm.DOMAIN_CHOICES, (pm.ROUTE_FLOW, pm.ROUTE_FEA)):
        msgs = first + [{"role": "assistant", "content": pm.DOMAIN_QUESTION},
                        {"role": "user", "content": send}]
        assert pm.reading_for(msgs).route == route


def test_buttons_come_with_exactly_the_application_questions():
    assert [c["send"] for c in pm.choices_for(pm.THERMAL_QUESTION)] == [s for _l, s in pm.THERMAL_CHOICES]
    analysis = pm.choices_for(pm.ANALYSIS_QUESTION)
    assert [c["soon"] for c in analysis] == [False, False, False, True, True, True, True]
    assert pm.choices_for("What fluid is it?") == []


def test_every_analysis_button_reads_back_as_its_own_type():
    for (key, _label, _v1), choice in zip(pm.ANALYSES, pm.choices_for(pm.ANALYSIS_QUESTION)):
        assert pm.analysis_of(choice["send"]) == key, choice


def test_v1_analyses_and_the_coming_soon_note():
    assert pm.V1_ANALYSES == ("static", "modal", "buckling")
    assert pm.coming_soon_note("static") == ""
    assert "coming soon" in pm.coming_soon_note("impact") and "static stress" in pm.coming_soon_note("impact")
    assert pm.analysis_of("modal analysis") == "modal"
    assert pm.analysis_of("linear buckling of this column") == "buckling"
    assert pm.analysis_of("FEA") == ""


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for origin in ([only] if only else ["audit60", "dev", "holdout", None]):
        e, r, t, m = score(origin)
        print(f"{origin or 'all'}: exact {e}/{t} ({100 * e / t:.0f}%), route {r}/{t} ({100 * r / t:.0f}%)")
        for row in m:
            print("   MISS", row)
