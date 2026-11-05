# Responsibility: Say a whole thermal model's job in an electronics-cooling engineer's words - the
# job sheet the user approves - and hold every thermal-model text to that vocabulary: no generic
# CFD words (inlet, outlet, stickers, "60% sure", triangles, a far field, a fluid "flowing through").
# Boundaries: pure. It takes the check's thermal view (application/thermal_view) and the settled
# requirements, and returns words; it reads no store and decides nothing about the mesh.
# Collaborates with: application/intake_brief.py (the job sheet on the brief), agents/intake
# (the reply guard), tests that hold the chat, the declaration and the brief to the vocabulary.
from __future__ import annotations

import re
from collections.abc import Iterable

#: WORDS A THERMAL MODEL'S TEXT NEVER USES, outside the file's own names. They belong to the
#: generic flow (a part with openings found on its shape), and an electronics-cooling engineer
#: reads them as a tool that did not understand the model: six "outlet" rows marked "60% sure" on
#: a set-top box (2026-10-12) is what this vocabulary exists to stop.
#: Read at letter boundaries, so a name part counts ("outlet_2") and a longer word does not (the
#: "sure" in "pressure").
FORBIDDEN = (
    ("inlet", r"(?<![a-z])inlets?(?![a-z])"),
    ("outlet", r"(?<![a-z])outlets?(?![a-z])"),
    ("sticker", r"(?<![a-z])stickers?(?![a-z])"),
    ("sure", r"(?<![a-z])sure(?![a-z])"),
    ("triangle", r"(?<![a-z])triangles?(?![a-z])"),
    ("far field", r"(?<![a-z])far[ _-]?fields?(?![a-z])"),
    ("flows through", r"(?<![a-z])flows? through(?![a-z])"),
)


def forbidden_words(text: str, names: Iterable[str] = ()) -> list[str]:
    """The generic-CFD words `text` uses, each once, in FORBIDDEN's order - [] when it speaks a
    thermal model's language. The file's own names (a part, a fan, a vent called "Inlet_grille")
    are the file's words, not ours: they are taken out before the text is read."""
    said = str(text or "")
    for name in sorted({str(n) for n in names if str(n).strip()}, key=len, reverse=True):
        said = re.sub(re.escape(name), " ", said, flags=re.IGNORECASE)
    return [word for word, rx in FORBIDDEN if re.search(rx, said, re.IGNORECASE)]


def file_names(view: dict | None) -> list[str]:
    """Every name the thermal model's file gives: its title, parts, materials, fans, vents,
    flow resistances, heat sources, compact models, probes and the air box's sides."""
    v = view or {}
    names = [str(v.get("title") or "")]
    for key in ("tree", "materials", "fans", "vents", "resistances", "sources", "compact_models",
                "probes"):
        for row in v.get(key) or []:
            if isinstance(row, dict):
                names.extend(str(row.get(k) or "") for k in ("name", "path", "region"))
    for side in ((v.get("domain") or {}).get("sides") or []):
        if isinstance(side, dict):
            names.append(str(side.get("name") or ""))
            names.extend(str(d) for d in side.get("devices") or [])
    return [n for n in names if n.strip()]


#: The detail levels, in the panel's words, by the mesher's fidelity words.
LEVEL_WORDS = {"draft": "Quick look", "standard": "Standard", "max": "Fine"}
#: What a snap-grid mesh of a thermal model is delivered as (engines/snapgrid deliverable).
_DELIVERED = "Delivered as an OpenFOAM multi-region case plus the thermal model."


def _num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    f = round(f, 3)
    return f"{f:g}"


def _cells(n) -> str:
    """A cell budget as an engineer says it: 2M, 500k."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return str(n)
    if v >= 1_000_000:
        return f"{v / 1_000_000:g}M"
    if v >= 1_000:
        return f"{v / 1_000:g}k"
    return str(v)


def job_sheet(view: dict | None, *, level: str = "", engine: str = "") -> str:
    """THE JOB, as an engineer's job sheet - what the user approves before the run:

    "Mesh “Set-top box, vented” (Flotherm, 8 parts + air, 8.7 W, 35 °C ambient) for a conjugate
    heat-transfer solve. Snap-grid hex mesh, Standard: up to 2M cells, at least 2 cells through
    every layer. Delivered as an OpenFOAM multi-region case plus the thermal model."

    Every number is the file's or the detail level's own. "" when there is no thermal view."""
    v = view or {}
    if not v:
        return ""
    t = v.get("totals") or {}
    n = int(t.get("parts") or 0)
    facts = [str(v.get("producer") or "").strip(),
             f"{n} part{'s' if n != 1 else ''} + air",
             f"{_num(t.get('power_W') or 0)} W"]
    amb = (v.get("ambient") or {}).get("temperature_C")
    if amb is not None:
        facts.append(f"{_num(amb)} °C ambient")
    title = str(v.get("title") or "").strip()
    said = [f"Mesh {('“' + title + '”') if title else 'the thermal model'} "
            f"({', '.join(f for f in facts if f)}) for a conjugate heat-transfer solve."]
    lvl = str(level or (v.get("detail") or {}).get("default") or "standard").strip().lower()
    words = LEVEL_WORDS.get(lvl, lvl.capitalize())
    from meshpipeline.engines.fitness import reads_own_format

    # the engine that reads the thermal model's file itself meshes it on the file's own grid
    # plans; another engine the user named is said by its name and the level only
    if not engine or reads_own_format(str(engine), "ecxml"):
        plan = (v.get("detail") or {}).get(lvl) or {}
        if not plan:
            from meshpipeline.engines.snapgrid.runner import FIDELITY_PLANS

            plan = FIDELITY_PLANS.get(lvl) or {}
        limits = []
        if plan.get("max_cells"):
            limits.append(f"up to {_cells(plan['max_cells'])} cells")
        k = plan.get("min_cells_across")
        if k:
            limits.append(f"at least {int(k)} cell{'s' if int(k) != 1 else ''} through every layer")
        g = plan.get("gap_cells")
        if g:
            limits.append(f"{int(g)} across every air gap and a fine warm-air layer on every "
                          "heated surface")
        said.append(f"Snap-grid hex mesh, {words}" + (f": {', '.join(limits)}." if limits else "."))
        said.append(_DELIVERED)
    else:
        from meshpipeline.engines.registry import engine_label

        said.append(f"{engine_label(str(engine))} mesh, {words} detail.")
    return " ".join(said)


__all__ = ["FORBIDDEN", "LEVEL_WORDS", "file_names", "forbidden_words", "job_sheet"]
