"""Try the plan step's conversation against the REAL intake model (NOTE-18, NOTE-19, NOTE-20): the go reading, the
reply writer and the question answerer (agents/intake/plan_reply.answered), through the same functions the product calls (agents/intake/plan_go.read, agents/intake/plan_reply.written
with its guard), on cases built here in memory. Nothing is stored; no session, no database, no worker.

The model settings are read the way the app reads them: from the environment (meshpipeline.settings, the intake's
route in agents/intake/settings). No setting value is printed. With no model configured it says so and exits 0.

Run from the worktree, with the environment the app runs with:
    PYTHONPATH=src python devtools/try_plan_talk.py            # both parts
    PYTHONPATH=src python devtools/try_plan_talk.py --go       # the go reading only
    PYTHONPATH=src python devtools/try_plan_talk.py --replies  # the replies and the answers to questions only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

PLAN_LINE = "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."
ASKED_OTHER = "Do you want the fillets finer as well?"

#: (message, our last line, what it should be) - "go" means it must start the run, "no go" that it must not,
#: "question" that it is a question about the mesh, answered from the plan's numbers (NOTE-20)
GO_CASES: list[tuple[str, str, str]] = [
    *[(t, PLAN_LINE, "go") for t in (
        "proceed", "ok I'm good to mesh", "all right, I'm good to mesh", "let's proceed to mesh generation",
        "ok im good", "yep run it", "alright that's everything", "that's all from me, mesh it", "fine, go ahead",
        "lets go", "do it", "send it", "looks good, start", "nothing else, proceed", "no more changes",
        "we're done here, mesh", "sounds good, mesh it", "im done", "good to go", "yes please start",
        "perfect, run it", "go for it", "alright lets mesh", "ok thats it", "k, go", "proceeed", "mesh it pls",
        "that's everything, thanks", "we're good", "carry on", "happy with that, go", "OK IM GOOD TO MESH",
        "yeah thats all lets go", "I'm done with this part, let's head to meshing", "nope, nothing else. go")],
    ("ok", "Start the mesh now?", "go"),
    *[(t, PLAN_LINE, "no go") for t in (
        "go finer on the bore", "proceed with 2 mm on the holes", "is it good to mesh like this?",
        "what happens if I proceed?", "not yet", "hold on", "go ahead but make the fillets finer", "don't start yet",
        "wait, one more thing", "run it at 1 mm", "can we refine the bore first?", "no, not yet", "stop",
        "yes but coarser on the plate", "proceed?", "ready to mesh?", "give me a sec", "how long will it take?")],
    ("ok", ASKED_OTHER, "no go"),
    # NOTE-20: questions about the mesh, typos and no question mark included (the first as typed on the site)
    *[(t, PLAN_LINE, "question") for t in (
        "so for this mesh basedon my rquirements, what would the elemwnt size be roughly",
        "how many elements will it be", "how long will it tak", "what did i set so far",
        "what will the cell count be?", "roughly how fine will the mesh be on the fillets",
        "tell me the element size")],
    # NOTE-21's message as typed on the site (a screenshot came with it, no face picked): a requirement, never a go
    ("make it finer in this hole", PLAN_LINE, "no go"),
]

FACE23 = {"id": "Face_23", "label": "Face 23 · round Ø19.8 mm"}
FACE7 = {"id": "Face_7", "label": "Face 7 · flat 40 × 12 mm"}
THREE = [{"id": f"Face_{k}", "label": f"Face {k} · round Ø6 mm"} for k in (4, 5, 6)]
PIC = [{"id": "a1", "kind": "image", "mime": "image/png"}]


def _done(rows=(), unsure=(), lines=()):
    return {"status": "done", "rows": list(rows), "unsure": list(unsure), "lines": list(lines)}


def _row(text, size, *, faces=None, where="", picked=False, words=""):
    r = {"quote": text, "words": words or text, "size": size, "parts": []}
    if faces:
        r.update(faces=faces, picked=True)
    elif where:
        r.update(parts=["Feature_2"], parts_text=where, picked=picked)
    return r


def _count(text, *, flow=False):
    """NOTE-24: the checklist's count row for these words, as application/part_matches.with_counts makes it."""
    from meshpipeline.agents.intake import plan_talk as PT
    (row,) = PT.count_rows({"text": text}, flow=flow)
    return row


#: (title, the message as kept, the checklist's view for it, our last reply)
REPLY_CASES = [
    ("one picked face, a size", {"text": "can you do a 0.5 mm mesh sizing in here?", "faces": [FACE23]},
     _done([_row("can you do a 0.5 mm mesh sizing in here?", "0.5 mm", faces=[FACE23])]), PLAN_LINE),
    ("several picked faces", {"text": "1 mm on these too", "faces": THREE},
     _done([_row("1 mm on these too", "1 mm", faces=THREE)]), "Sure, I've set 0.5 mm elements on Face 23."),
    ("a guessed place, with a picture", {"text": "in that big hole I want a refined mesh, see the picture",
                                         "attachments": PIC},
     _done([_row("in that big hole I want a refined mesh, see the picture", "finer",
                 where="the hole (22 mm across)")]), "Got it, those 3 faces get 1 mm elements as well."),
    ("a guessed place, no picture", {"text": "make the fillet finer"},
     _done([_row("make the fillet finer", "finer", where="the fillet (radius 8 mm)")]), PLAN_LINE),
    ("a picture with words, picked face", {"text": "finer here like in the screenshot", "attachments": PIC,
                                          "faces": [FACE7]},
     _done([_row("finer here like in the screenshot", "finer", faces=[FACE7])]), PLAN_LINE),
    ("a picture alone", {"text": "", "attachments": PIC}, _done(), PLAN_LINE),
    ("nothing readable", {"text": "make it look nice"}, _done(), PLAN_LINE),
    ("a quality limit", {"text": "keep skewness under 0.8"},
     _done([_row("keep skewness under 0.8", "", where="the whole model")]), PLAN_LINE),
    ("a whole-mesh size", {"text": "use 3 mm everywhere"},
     _done([_row("use 3 mm everywhere", "3 mm", where="the whole model")]), PLAN_LINE),
    ("finer, no number, picked face", {"text": "finer on this one", "faces": [FACE7]},
     _done([_row("finer on this one", "finer", faces=[FACE7])]), PLAN_LINE),
    ("a phrase it could not place", {"text": "1 mm on the lip"},
     _done(unsure=[{"quote": "1 mm on the lip", "words": "1 mm on the lip", "size": "1 mm"}]), PLAN_LINE),
    ("two rows in one message", {"text": "2 mm on the boss and finer on the fillet"},
     _done([_row("2 mm on the boss", "2 mm", where="the boss (36 mm across)"),
            _row("finer on the fillet", "finer", where="the fillet (radius 8 mm)")]), PLAN_LINE),
    ("a flow mesh line", {"text": "3 layers on the walls"},
     _done(lines=['From your notes: "3 layers on the walls": 3 layers on the walls.']), PLAN_LINE),
    ("a face picked, no words", {"text": "", "faces": [FACE23]}, _done(), PLAN_LINE),
    # CFD, a flow model with no checklist before the run (GET .../check/parts says "none": the view is None)
    ("CFD no checklist: a y+ ask", {"text": "y+ of 1 on the walls please"}, None, PLAN_LINE),
    ("CFD no checklist: a layers ask", {"text": "can we get 5 prism layers on the pipe wall?"}, None, PLAN_LINE),
    ("CFD no checklist: a size on a picked wall", {"text": "can you make the cells 0.5 mm on this wall?",
                                                   "faces": [{"id": "Face_1", "label": "Face 1 · round Ø40 mm"}]},
     None, PLAN_LINE),
    ("CFD no checklist: finer around the bend, picture", {"text": "finer around the bend", "attachments": PIC},
     None, PLAN_LINE),
    # NOTE-24: a whole-mesh count (the first as typed on the site, FEA solid, one cast part)
    ("a count, as typed on the site", {"text": "keep it under 1.5 million elements"},
     _done([_count("keep it under 1.5 million elements")]), PLAN_LINE),
    ("a count, about", {"text": "about 800k elements"}, _done([_count("about 800k elements")]), PLAN_LINE),
    ("a count, flow mesh, at least", {"text": "at least 2 million cells"},
     {**_done([_count("at least 2 million cells", flow=True)]), "flow": True}, PLAN_LINE),
    ("a count, max", {"text": "max 500,000 elements"}, _done([_count("max 500,000 elements")]), PLAN_LINE),
    ("a count beside a typed size (solid: the size wins)", {"text": "keep it under 1.5 million elements"},
     _done([_row("use 3 mm everywhere", "3 mm", where="the whole model"),
            _count("keep it under 1.5 million elements")]), "All right, 3 mm elements across the whole part."),
    ("a count beside a typed size (flow: the count comes first)", {"text": "at least 2 million cells"},
     {**_done([_row("use 3 mm everywhere", "3 mm", where="the whole model"),
               _count("at least 2 million cells", flow=True)]), "flow": True}, PLAN_LINE),
    # NOTE-25: words that set nothing are kept for the planning step, and a change to the CAD is told plainly
    ("words that set nothing: the analysis", {"text": "it's for a stress analysis of the bracket"}, _done(), PLAN_LINE),
    ("words that set nothing: the material", {"text": "the part is cast aluminium"}, _done(),
     "Will do, I'll keep the mesh under 1.5 million elements."),
    ("words that set nothing, but ask something of the mesh", {"text": "make it a good quality mesh please"},
     _done(), PLAN_LINE),
    ("words that set nothing, flow mesh", {"text": "make it a good quality mesh please"}, {**_done(), "flow": True},
     PLAN_LINE),
    ("a change to the CAD itself", {"text": "remove the small holes"}, _done(), PLAN_LINE),
    # NOTE-21, as typed on the site: one hole named, a screenshot, no face picked, 11 holes on the part
    ("one hole named, eleven on the part, picture", {"text": "make it finer in this hole", "attachments": PIC},
     _done(unsure=[{"quote": "make it finer in this hole", "words": "make it finer in this hole", "size": "finer",
                    "which_one": 11, "noun": "hole"}]), PLAN_LINE),
]

#: NOTE-20: (title, the question as typed, the checklist's view, the plan's numbers as
#: application/fea_check.planned_sizes gives them) - answered by agents/intake/plan_reply.answered
STEP = {"finer": 0.5, "coarser": 2.0}
SIX = [{"id": f"Face_{k}", "label": f"Face {k} · round Ø8 mm"} for k in range(1, 7)]
ANSWER_CASES = [
    ("size question, as typed on the site, plan sized",
     "so for this mesh basedon my rquirements, what would the elemwnt size be roughly",
     _done([_row("finer on these", "finer", faces=SIX), _row("2 mm on part 2", "2 mm", where="part 2"),
            _row("finer in the bore", "finer", where="the bore (12 mm across)")]),
     {"known": True, "step": STEP, "coarsest_mm": 4.1, "finest_mm": 0.62, "finest_where": "the fillets",
      "elements": "about 412,000 elements", "time": "usually about 3 minutes, up to 7 minutes",
      "before_requirements": True}),
    ("size question, as typed on the site, nothing sized before the run",
     "so for this mesh basedon my rquirements, what would the elemwnt size be roughly",
     _done([_row("finer on these", "finer", faces=SIX)]),
     {"known": False, "step": STEP, "why": "The overall element size is worked out from the measured part when the "
                                           "mesh is built, so I'll have it, and the element count, once the run "
                                           "starts."}),
    ("count question, plan sized, no requirements", "how many elements will it be", _done(),
     {"known": True, "step": STEP, "coarsest_mm": 6.0, "elements": "about 95,000 elements",
      "before_requirements": True}),
    ("what was set", "what did i set so far",
     _done([_row("1 mm on these too", "1 mm", faces=THREE)],
           [{"quote": "make it finer in this hole", "words": "make it finer in this hole", "size": "finer",
             "which_one": 11, "noun": "hole"}]),
     {"known": False, "step": STEP, "why": "The check is still sizing the mesh, so the overall element size and the "
                                           "element count will be on the stage in a moment."}),
]


def _configured() -> str:
    """"" when the intake's model can be called; else why not (no value printed)."""
    from meshpipeline.agents.intake import settings as icfg
    from meshpipeline.settings import providers as provcfg
    provider = str(getattr(icfg.INTAKE_ROUTE.primary, "provider", "") or "")
    if not provider:
        return "the intake route names no provider"
    if "deepseek" in provider.casefold():
        return "the intake route names DeepSeek, which is never used here"
    if not getattr(provcfg, f"{provider.upper()}_API_KEY", ""):
        return f"no key is set in the environment for the intake route's provider ({provider})"
    return ""


def _install() -> None:
    """The real router and an in-process capacity controller: what runtime/composition binds, minus Redis and the
    decision cache (so every call really goes to the model and its seconds are real)."""
    from meshpipeline.adapters.model_capacity.local import LocalCapacityController
    from meshpipeline.adapters.model_inference import router as real
    from meshpipeline.contracts import model_capacity, model_inference
    model_capacity.set_capacity_controller(LocalCapacityController())
    model_inference.set_model_router(real)


async def _go_part() -> tuple[int, int]:
    from meshpipeline.agents.intake import consent as C
    from meshpipeline.agents.intake import plan_go as PG
    from meshpipeline.agents.intake import plan_talk as PT
    right = 0
    print("=" * 100 + "\nTHE GO READING (agents/intake/plan_go.read)\n" + "=" * 100)
    for text, last, want in GO_CASES:
        t0 = time.monotonic()
        got = await PG.read(text, consent=C.reading(text), last_line=last, asked=PT.asked_last(
            [{"role": "assistant", "content": last}]), user_id="try-plan-talk", session_id="try")
        took = time.monotonic() - t0
        ok = got.reading == PT.QUESTION if want == "question" else (got.reading == PT.GO) == (want == "go")
        right += ok
        print(f"{'ok ' if ok else 'BAD'} {took:5.2f}s  {text!r:58} last={'start?' if last == PT.ASK_LINE else 'q' if last == ASKED_OTHER else 'plan'}"
              f"  model={got.raw!r:14} -> {got.reading:7} (by {got.by}: {got.why}); want {want}")
    return right, len(GO_CASES)


async def _reply_part() -> tuple[int, int]:
    from meshpipeline.agents.intake import plan_reply as PR
    said_by_model = 0
    print("=" * 100 + "\nTHE REPLIES (agents/intake/plan_reply.written: the model, then the guard)\n" + "=" * 100)
    for title, note, view, last in REPLY_CASES:
        t0 = time.monotonic()
        got = await PR.written(note, view, last_reply=last, session_id="try", user_id="try-plan-talk")
        took = time.monotonic() - t0
        said_by_model += got["source"] == PR.SOURCE_MODEL
        extra = (f", {len(note.get('faces') or [])} face(s) picked" if note.get("faces") else "") + (
            ", with a picture" if note.get("attachments") else "")
        print(f"--- {title} ({took:.2f}s)\n  user:   {note.get('text')!r}{extra}\n  model:  {got['raw']!r}\n"
              f"  guard:  {'accepted' if got['source'] == PR.SOURCE_MODEL else got['why'] or got['source']}\n"
              f"  shown:  {got['text']}")
    print("=" * 100 + "\nTHE ANSWERS TO QUESTIONS (agents/intake/plan_reply.answered: the model, then the guard)\n"
          + "=" * 100)
    for title, text, view, plan in ANSWER_CASES:
        t0 = time.monotonic()
        got = await PR.answered(text, view, plan, last_reply=PLAN_LINE, session_id="try", user_id="try-plan-talk")
        took = time.monotonic() - t0
        said_by_model += got["source"] == PR.SOURCE_MODEL
        print(f"--- {title} ({took:.2f}s)\n  user:   {text!r}\n  model:  {got['raw']!r}\n"
              f"  guard:  {'accepted' if got['source'] == PR.SOURCE_MODEL else got['why'] or got['source']}\n"
              f"  shown:  {got['text']}")
    return said_by_model, len(REPLY_CASES) + len(ANSWER_CASES)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--go", action="store_true", help="the go reading only")
    ap.add_argument("--replies", action="store_true", help="the replies only")
    args = ap.parse_args(argv)
    why = _configured()
    if why:
        print(f"No model is configured for the intake ({why}). Nothing was called.")
        return 0
    _install()
    both = not (args.go or args.replies)

    async def run() -> None:
        if both or args.go:
            right, n = await _go_part()
            print(f"\ngo reading: {right}/{n} as wanted\n")
        if both or args.replies:
            by_model, n = await _reply_part()
            print(f"\nreplies and answers: {by_model}/{n} said as the model wrote them (the rest: the plain sentence)")
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
