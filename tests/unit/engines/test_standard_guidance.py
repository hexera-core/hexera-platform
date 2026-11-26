# Responsibility: Verify STD-6 - the team standard's lines that are no setting (design intent, context) reach the run's AI steps as guidance, labelled as the team's saved settings and placed BEFORE the run's own notes; only the lines for the run's kind of mesh (STD-5); told "kept as guidance for the mesh planning" before meshing, with the result and on the settings page; and nothing changes for a run whose standard holds no such line.
# Boundaries: pure over plain data; the standard's reading is fixed (no model is called); each AI step's prompt block is built by its own function.
from __future__ import annotations

import json

from meshpipeline.contracts import user_notes as un
from meshpipeline.engines import fea_ai as FA
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS
from meshpipeline.engines.fea_review import FEA_PURPOSE

INTENT = "we care about fatigue at the fillets"
CFD_ONLY = "keep the wake resolved behind the body"
TEXT = f"Global size 5 mm\n{INTENT}\nFor CFD meshes:\n{CFD_ONLY}"
SIZES_ONLY = "Global size 5 mm"
RUN_NOTE = "Fillets do not matter on this run"


def _answer(*, intent: bool = True) -> str:
    other = ([{"quote": INTENT, "kind": "mesh"}, {"quote": CFD_ONLY, "kind": "mesh"}] if intent else [])
    lines = [{"quote": CFD_ONLY, "kinds": ["cfd"]}] if intent else []
    return json.dumps({"stated_unit": "none", "sizes": [{"what": "global", "value": 5, "unit": "mm",
                                                       "quote": "Global size 5 mm"}],
                       "counts": [], "layers": [], "local": [], "physics": [], "element_kind": [], "limits": [],
                       "other": other, "controls": [], "strictness": [],
                       "applies_to": {"whole": ["any"], "lines": lines}})


def _state(text: str = TEXT, *, intent: bool = True, purpose: str = FEA_PURPOSE, notes=None) -> dict:
    std = MS.snapshot(text, {"key": MS.key_of(text), "arguments": _answer(intent=intent)})
    return {MS.STATE_KEY: std, "purpose": purpose,
            "user_notes": [un.make(RUN_NOTE, source=un.SOURCE_CONSOLE, at="t")] if notes is None else notes}


# ------------------------------------------------------------------------------- the hand-over to the AI steps ---
def test_the_intent_comes_first_labelled_and_the_runs_notes_after_it():
    got = MS.guidance_notes(_state(), route="fea")
    assert [n["source"] for n in got] == [un.SOURCE_TEAM, un.SOURCE_CONSOLE]
    block = un.prompt_block(got, consumer="fea_ai")
    assert INTENT in block and "your team's saved meshing settings" in block
    assert block.index(INTENT) < block.index(RUN_NOTE)
    assert "the run's notes win" in block


def test_a_cfd_only_line_is_not_handed_to_an_fea_run_and_is_to_a_flow_run():
    fea = un.render(MS.guidance_notes(_state(), route="fea"))
    assert INTENT in fea and CFD_ONLY not in fea
    flow = un.render(MS.guidance_notes(_state(purpose="external_flow"), route=None))
    assert INTENT in flow and CFD_ONLY in flow


def test_the_fea_plan_step_reads_it_before_the_notes():
    out = FA.brief({"faces": {}, "bodies": 1}, _state(), [], diag_m=0.1, limit=1_000_000)
    assert INTENT in out["user_notes"] and out["user_notes"].index(INTENT) < out["user_notes"].index(RUN_NOTE)
    assert CFD_ONLY not in out["user_notes"]


def test_the_builder_its_workspace_file_the_snappy_planner_and_the_reviewer_read_it(tmp_path):
    from meshpipeline.agents.builder import attempt

    state = _state(purpose="external_flow")
    note = attempt.user_notes_note(tmp_path, state)
    assert INTENT in note and CFD_ONLY in note and note.index(INTENT) < note.index(RUN_NOTE)
    # the snappy planner reads the attempt's user_notes.txt (written from the same notes)
    assert un.write_file(tmp_path, attempt._guidance_notes(state))
    planner = un.workspace_block(tmp_path, consumer="snappy_planner")
    assert INTENT in planner and planner.index(INTENT) < planner.index(RUN_NOTE)
    # the reviewer: context only, never a criterion (NOTE-3)
    ctx = un.context_block(MS.guidance_notes(state), consumer="reviewer")
    assert INTENT in ctx and "CONTEXT ONLY" in ctx


def test_with_no_run_notes_the_intent_alone_reaches_the_steps(tmp_path):
    from meshpipeline.agents.builder import attempt

    state = _state(notes=[], purpose="external_flow")
    assert INTENT in attempt.user_notes_note(tmp_path, state)


# ------------------------------------------------------------------------------------------- nothing changes ---
def test_no_standard_or_settings_only_is_byte_for_byte_the_run_before(tmp_path):
    from meshpipeline.agents.builder import attempt

    notes = [un.make(RUN_NOTE, source=un.SOURCE_CONSOLE, at="t")]
    for state in ({"user_notes": notes, "purpose": FEA_PURPOSE},
                  _state(SIZES_ONLY, intent=False, notes=notes)):
        assert MS.guidance_notes(state) is notes
        plain = {"user_notes": notes, "purpose": state["purpose"]}
        assert FA.brief({"faces": {}, "bodies": 1}, state, [], diag_m=0.1, limit=10**6) == \
            FA.brief({"faces": {}, "bodies": 1}, plain, [], diag_m=0.1, limit=10**6)
        assert attempt.user_notes_note(tmp_path, state) == attempt.user_notes_note(tmp_path, plain)
    assert MS.guidance_notes({"purpose": FEA_PURPOSE}) == []
    assert "user_notes" not in FA.brief({"faces": {}, "bodies": 1}, _state(SIZES_ONLY, intent=False, notes=[]), [],
                                        diag_m=0.1, limit=10**6)


# ------------------------------------------------------------------------------------------------- told back ---
def test_told_as_kept_as_guidance_before_meshing_with_the_result_and_on_the_page():
    merged = MS.merge(MS.checked(_answer(), TEXT, route="fea"), MR.section("none"), route="fea")
    line = f'{MS.GUIDANCE_LEAD}: "{INTENT}".'
    assert line in merged["lines"] and line in MR.told_words(merged)
    assert merged["standard"]["guidance"] == [INTENT]
    assert not any("no mesh setting of ours matches" in w for w in merged["lines"])
    assert not any(w.startswith("Not applied") and INTENT in w for w in merged["lines"])
    # the CFD-only line is STD-5's: named as written for flow meshes, never as guidance here
    assert any(CFD_ONLY in w and "flow (CFD)" in w for w in merged["lines"])
    assert not any(w.startswith(MS.GUIDANCE_LEAD) and CFD_ONLY in w for w in merged["lines"])
    page = MS.understood(TEXT, {"key": MS.key_of(TEXT), "arguments": _answer()})
    assert f'Kept as guidance for the mesh planning: "{INTENT}".' in page
