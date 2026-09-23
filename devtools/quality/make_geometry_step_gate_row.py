#!/usr/bin/env python3
# Responsibility: Write `geometry_step_gate_row.json`, the answered survey row with a plan on it that
#                 `check_flags_gone_changed_nothing.py` reads, from the current code and the conversation the
#                 first one recorded.
# Boundaries: a fixture generator. It composes through the platform's own `geometry_survey`, answers through
#             `geometry_survey.answered`, and plans through `geometry_step.plan_the_part`. It decides nothing.
#
# WHY IT EXISTS. The row was recorded by hand in `77d142f` and nothing could make it again. A stored survey is
# only usable while it is what its own inputs COMPOSE to: `geometry_step` refuses to plan against a survey it
# cannot reproduce, because the customer's answers are bound to the survey they were asked about. So the moment
# the measurement package changed how a survey is composed (here: `52b95308`, which changed where the builder's
# bore comes from), the fixture went stale and the step began falling back to the builder's own planner. The
# gate that read it then reported "the planner's message did not move" and called ITSELF blind, which was the
# correct verdict and a useless one: nothing was wrong with the platform, the ruler had rotted.
#
# The CONVERSATION is preserved and only the COMPOSITION is refreshed: the same two role answers on the same two
# mouths, by the same principal, so the gate keeps testing what it was written to test.
#
#   python devtools/quality/make_geometry_step_gate_row.py [--out PATH] [--document PATH]
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

#: The conversation `77d142f` recorded, kept verbatim so the refreshed row asks and answers the same things.
ANSWERS = (("role_inlet", "o1", "o1 it is"), ("role_outlet", "o2", "o2 it is"))
PRINCIPAL = "owner-gate"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).with_name("geometry_step_gate_row.json")))
    ap.add_argument("--document",
                    default=str(ROOT / "tests" / "fixtures" / "geometry_survey" / "venturi_orifice_001.json"))
    ap.add_argument("--brief-from", default=str(Path(__file__).with_name("geometry_step_gate_row.json")),
                    help="the row to take the customer's own words from; the words are theirs, not this file's")
    args = ap.parse_args()

    import meshpipeline.settings.policy as polcfg
    polcfg.GEOMETRY_AGENT_STEP_PROVIDER = "reference"
    # THE TWO PACKAGE STAGES AT THEIR SHIPPED DEFAULT, which is off, because that is the configuration the
    # gate that reads this row runs under. A row composed with a stage its reader does not have is a row
    # that reader cannot reproduce, and the step refuses a survey it cannot reproduce. An export in the
    # shell wins over the platform, so the two are cleared first and the arming is asserted, not assumed.
    polcfg.GEOMETRY_MEASURED_STOPS_ENABLED = False
    polcfg.GEOMETRY_FLUID_SIDE_ENABLED = False
    for name in polcfg.GEOMETRY_PACKAGE_SWITCHES:
        os.environ.pop(name, None)
    armed = polcfg.arm_the_package()
    if set(armed.values()) != {"off"}:
        raise SystemExit(f"the package's stages are not where this row is composed: {armed}")

    from meshpipeline.application import geometry_step as gst
    from meshpipeline.application import geometry_survey as gs

    doc = json.loads(Path(args.document).read_text(encoding="utf-8"))
    before = json.loads(Path(args.brief_from).read_text(encoding="utf-8")).get("composed_for") or {}
    brief = str(before.get("brief") or "")
    if not brief:
        raise SystemExit(f"no customer words to compose from in {args.brief_from}")

    state = gs.compose(doc, purpose=str(before.get("purpose") or "internal_cfd"), brief=brief, declared=[],
                       engine=str(before.get("engine") or "snappy"))
    for question_id, option, words in ANSWERS:
        state = gs.answered(state, doc, question_id=question_id, choice=option, words=words,
                            latest_user_message=words, principal=PRINCIPAL)
    state = gst.plan_the_part(state, doc, fidelity="standard", job_id="gate-row")

    step = state.get("geometry_step") or {}
    if str(step.get("status") or "") != "planned":
        raise SystemExit(f"the step did not plan, so this row witnesses nothing: {step.get('reason')!r}")
    for question_id, _o, _w in ANSWERS:
        if question_id not in (state.get("asked") or []):
            raise SystemExit(f"{question_id} was not asked, so the recorded conversation does not fit this survey")

    Path(args.out).write_text(json.dumps(state, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"  stage      {state.get('stage')}")
    print(f"  asked      {state.get('asked')}")
    print(f"  answers    {len(state.get('answers') or [])}")
    print(f"  step       {step.get('status')}, plan {'present' if step.get('plan') else 'absent'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
