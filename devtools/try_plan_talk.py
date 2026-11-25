"""Try the plan step's conversation against the REAL intake model (NOTE-18, NOTE-19): the go reading and the reply
writer, through the same functions the product calls (agents/intake/plan_go.read, agents/intake/plan_reply.written
with its guard), on cases built here in memory. Nothing is stored; no session, no database, no worker.

The model settings are read the way the app reads them: from the environment (meshpipeline.settings, the intake's
route in agents/intake/settings). No setting value is printed. With no model configured it says so and exits 0.

Run from the worktree, with the environment the app runs with:
    PYTHONPATH=src python devtools/try_plan_talk.py            # both parts
    PYTHONPATH=src python devtools/try_plan_talk.py --go       # the go reading only
    PYTHONPATH=src python devtools/try_plan_talk.py --replies  # the replies only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

PLAN_LINE = "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."
ASKED_OTHER = "Do you want the fillets finer as well?"

#: (message, our last line, what it should be) - "go" means it must start the run, "no go" that it must not
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
        ok = (got.reading == PT.GO) == (want == "go")
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
    return said_by_model, len(REPLY_CASES)


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
            print(f"\nreplies: {by_model}/{n} said as the model wrote them (the rest: the plain sentence)")
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
