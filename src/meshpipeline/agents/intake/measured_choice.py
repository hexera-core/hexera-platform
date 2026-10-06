# Responsibility: Give the intake the measured engine recommendation for the uploaded geometry, and the words it is put in.
# Boundaries: advice and wording; it confirms nothing on its own - the user's yes, or their naming of an engine, does.
# Collaborates with: engines/fitness.py (the ranking), cad/shape_traits.py (the measurement), agents/intake/agent.py,
#                    agents/intake/executor.py (the proposal), agents/intake/engine_selection.py (the question).
"""THE MEASURED RECOMMENDATION, AT THE INTAKE.

Areen's rule: the system recommends the best engine for THIS geometry and says why; the user picks.
Before, the intake model picked an engine from a paragraph of prose per engine. Now the uploaded
file is measured (cad/shape_traits.py), every engine that can take it for the confirmed flow is
ranked by its lab record on shapes measured to be like it (engines/fitness.py), and the ONE engine
question the application asks names the best one, with its evidence, and lists the others with
their fit. The model presents it and explains it when asked; it does not pick another on its own.
The user's answer decides: a yes takes the recommendation, naming any other engine takes that one.

The measurement runs off the event loop and is waited for a bounded time: a large CAD file may
take longer on its first turn, and then the turn goes on without it (the measurement finishes in
the background and is ready, cached, for the next turn).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: How long a turn waits for the geometry to be measured before going on without it.
MEASURE_WAIT_S = 15.0


def staged_upload(session_id: str) -> str:
    """The upload as staged beside the session (the file cad/regions reads), or ''."""
    try:
        from pathlib import Path

        import meshpipeline.settings.runtime as rtcfg
        from meshpipeline.contracts.intake_formats import format_for_suffix

        root = Path(rtcfg.JOBS_DIR) / str(session_id or "")
        staged = sorted(p for p in root.iterdir()
                        if p.is_file() and format_for_suffix(p.suffix.lower()))
        return str(staged[0]) if staged else ""
    except Exception:  # noqa: BLE001 - no staged file simply leaves the name to answer
        return ""


def upload_form(staged: str, source_ref) -> str:
    """WHAT KIND OF FILE the user uploaded - a CAD solid or a surface mesh - asked of the one
    reading (engines/capability.geometry_form): the staged file itself first, so a STEP that is
    really a faceted mesh reads as the surface it is, then the approved source's own name. ''
    claims nothing."""
    from meshpipeline.engines.capability import geometry_form
    for candidate in (staged, getattr(source_ref, "suffix_hint", ""),
                      getattr(source_ref, "original_filename", "")):
        form = geometry_form(candidate)
        if form:
            return form
    return ""


def able_engines(engines: Iterable[str], purpose: str, input_kind: str, form: str) -> list[str]:
    """The engines that can take this file for this confirmed case (the capability filter, read
    through the intake's own admission preview)."""
    from meshpipeline.agents.intake.validation import preview_admission
    return [e for e in engines
            if preview_admission(e, purpose, input_kind, dimensionality="3D",
                                 geometry_form=form or None).get("verdict") != "impossible"]


def rank(*, path: str, purpose: str, input_kind: str, form: str, engines: Sequence[str],
         patches: Sequence[Mapping] = (), said: str = "", region_count: int | None = None):
    """The recommendation for this upload and case, or None when nothing can be ranked (no flow,
    no engine that takes the file). An unmeasurable file is still ranked, on its flow alone, and
    its reasons say how little that is."""
    from meshpipeline.cad.shape_traits import ShapeTraits, measure_file
    from meshpipeline.engines.capability import flow_of
    from meshpipeline.engines.fitness import brief_facts, recommend

    flow = flow_of(purpose)
    if not flow:
        return None
    able = able_engines(engines, purpose, input_kind, form)
    if not able:
        return None
    traits = measure_file(path, flow=flow, input_kind=input_kind, patches=list(patches)) if path \
        else ShapeTraits(flow=flow, input_kind=input_kind, notes=("no file to measure",))
    if not traits.form and form:
        traits = ShapeTraits.from_dict({**traits.as_dict(), "form": form})
    brief = brief_facts(request_txt=said, patches=list(patches), region_count=region_count)
    return recommend(traits, able, brief=brief)


async def rank_in_time(*, timeout_s: float = MEASURE_WAIT_S, **kw):
    """`rank` off the event loop, waited for at most `timeout_s`; None when it is not ready (the
    measurement keeps going and is cached for the next turn) or fails."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(rank, **kw), timeout=timeout_s)
    except TimeoutError:
        logger.info("Intake: the geometry is still being measured - no engine recommendation "
                    "this turn")
    except Exception as exc:  # noqa: BLE001 - advice never breaks a turn
        logger.warning("Intake: engine recommendation unavailable (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
    return None


# #
# the words
# #

def _display(engine: str) -> str:
    from meshpipeline.agents.intake import vocabulary as _vocab
    return _vocab.to_display(_vocab.ENGINE, engine)


def statement(rec, engine: str) -> str:
    """The ONE engine question, with the evidence: the engine proposed and why, then the other
    engines that can take the file with their fit, then the plain way to answer."""
    fit = rec.fit_of(engine) if rec is not None else None
    if fit is None:
        from meshpipeline.agents.intake.engine_selection import render_selection_statement
        return render_selection_statement(engine)
    head = f"This looks like {rec.shape}. " if rec.shape else ""
    lab = (f"in our lab it {fit.reason}" if fit.reason.startswith("passed")
           else f"in our lab there is {fit.reason}" if fit.reason.startswith("little")
           else f"in our lab: {fit.reason}")
    lines = [f"{head}I'd mesh it with {_display(engine)} - {lab}."]
    if fit.heads_up:
        lines.append(f"Heads-up: {fit.heads_up.rstrip('.')}.")
    others = [f for f in rec.fits if f.engine != engine]
    if others:
        listed = "; ".join(f"{_display(f.engine)} ({_fit(f)})" for f in others)
        lines.append(f"Also able to mesh it: {listed}.")
    lines.append("OK, or pick another engine?")
    return " ".join(lines)


def _fit(fit) -> str:
    from meshpipeline.engines.fitness import fit_words
    words = fit_words(fit)
    ev = fit.evidence
    if ev.level >= 0 and not ev.low:
        words += f", passed {ev.passed} of {ev.runs}"
    elif ev.near_runs:
        words += f", passed {ev.near_passed} of {ev.near_runs} on the few shapes like this"
    elif ev.level >= 0:
        words += ", not yet tried on shapes like this"
    if not fit.layers:
        words += ", no reliable near-wall layers"
    return words


def ranking_rows(rec) -> list[dict]:
    """The ranking as the comparison tool reports it: one row per engine, in plain words."""
    return [{"rank": f.rank, "engine": _display(f.engine), "fit": _fit(f), "reason": f.reason,
             "heads_up": f.heads_up, "recommended": f.recommended} for f in rec.fits]


def prompt_block(rec, *, settled: bool) -> str:
    """The per-turn system note: what the application measured and how each engine ranks, so the
    model can present the recommendation and answer questions about the alternatives."""
    if rec is None or not rec.fits:
        return ""
    rows = "\n".join(
        f"  {f.rank}. {_display(f.engine)} - {_fit(f)}: {f.reason}"
        + (f". Heads-up: {f.heads_up}" if f.heads_up else "")
        + (" [RECOMMENDED]" if f.recommended else "") for f in rec.fits)
    shape = f"  Shape: {rec.shape}.\n" if rec.shape else ""
    if settled:
        use = ("The engine is already settled by the user; this ranking is background for "
               "questions only - never re-open the choice.")
    else:
        use = ("When you settle the engine (ENGINE FIRST rule 3), call propose_engine_selection "
               f"with {_display(rec.engine)} - the application words the question with this "
               "evidence and lists the alternatives. If the user asks why, or about another "
               "engine, answer from these lines in plain words. The user picks: any engine "
               "listed here that they name is theirs, without argument.") if rec.engine else (
               "No engine here is suited by its own declaration; if the user names one, take it.")
    return ("\n\nMEASURED ENGINE RECOMMENDATION (the application measured the uploaded geometry "
            "and compared it with lab runs of every engine on shapes measured to be like it - "
            "evidence, not opinion):\n" + shape + rows + "\n" + use)


def choice_payload(gate) -> dict | None:
    """WHAT THE CONSOLE DRAWS under the open engine question: the engine proposed, and every
    engine that can take the file with its fit, its reason and the reply that picks it. None
    unless the question is open and was asked with a measured recommendation."""
    from meshpipeline.agents.intake.engine_selection import PROPOSED, state_of
    sel = gate.get("selection") if isinstance(gate, Mapping) else None
    if state_of(sel) != PROPOSED or not isinstance(sel, Mapping):
        return None
    rec = sel.get("recommendation")
    if not isinstance(rec, Mapping) or not rec.get("fits"):
        return None
    engines = []
    for f in rec.get("fits") or ():
        if not isinstance(f, Mapping) or not f.get("engine"):
            continue
        label = str(f.get("label") or _display(str(f["engine"])))
        engines.append({
            "engine": f["engine"], "label": label, "fit": f.get("fit") or "",
            "reason": f.get("reason") or "", "heads_up": f.get("heads_up") or "",
            "recommended": bool(f.get("recommended")),
            "outside": bool(f.get("outside")), "layers": bool(f.get("layers", True)),
            "evidence": dict(f.get("evidence") or {}),
            "proposed": f["engine"] == sel.get("engine"),
            "reply": f"Use {label}"})
    return {"proposed": sel.get("engine"), "recommended": rec.get("engine") or "",
            "shape": rec.get("shape") or "", "engines": engines,
            "table_version": rec.get("table_version") or ""}


def compact(rec) -> dict | None:
    """What the session keeps of a recommendation (the console draws the engine choice from it)."""
    if rec is None:
        return None
    d = rec.as_dict()
    d.pop("traits", None)
    for f in d.get("fits") or ():
        f["label"] = _display(f.get("engine", ""))
    return d


__all__ = ["MEASURE_WAIT_S", "able_engines", "choice_payload", "compact", "prompt_block", "rank", "rank_in_time",
           "ranking_rows", "staged_upload", "upload_form",
           "statement"]
