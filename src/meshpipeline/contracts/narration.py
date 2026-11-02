# Responsibility: Say what Hexera is doing and what it decided, in short plain words, on the run's live stream.
# Owns: the narration phrase table (what each kind of work is called), the machine sentence, the leak guard
# every narrated line passes, and the helpers that publish a step row or a commentary line.
# Boundaries: words only - it decides nothing and reads only its arguments. A line that fails the guard is
# dropped, never published; a publisher that cannot narrate (an older double) is skipped.
# Collaborates with: events (the NARRATE type), contracts/mesh_progress (the mesher's stages), engines/fea_driver
# (machine, plan and retry lines), ui/js/render/run_progress.js (draws them).
from __future__ import annotations

import re
from typing import Any

from meshpipeline.contracts.event_stream import EventStreamError

#: The kinds of work a step row can be. Each has its own status icon in the UI; "say" is a commentary line.
KINDS = ("read", "measure", "plan", "refine", "layer", "fill", "check", "export", "machine", "say")

#: What the running mesher is doing, by the stage key engines/mesher_stage and runtime/mesh_runner report.
#: The user reads THESE words, never the mesher's own label: those name tools and phases ("castellating").
STAGES: dict[str, tuple[str, str]] = {
    "uploading":    ("machine", "Sending your part to the mesh machine"),
    "starting":     ("machine", "Getting the mesher ready"),
    "background":   ("refine",  "Laying out the starting cells"),
    "features":     ("measure", "Finding the sharp edges"),
    "castellating": ("refine",  "Refining the cells around your part"),
    "octree":       ("refine",  "Sizing the cells around your part"),
    "sizing":       ("refine",  "Setting the element sizes"),
    "snapping":     ("refine",  "Fitting the cells to your surfaces"),
    "surface":      ("refine",  "Meshing the surfaces"),
    "layers":       ("layer",   "Building boundary layers"),
    "cells":        ("fill",    "Filling the volume"),
    "volume":       ("fill",    "Filling the volume"),
    "optimising":   ("check",   "Improving the elements"),
    "improving":    ("check",   "Improving cell quality"),
    "curving":      ("refine",  "Curving the elements onto the surface"),
    "balancing":    ("check",   "Finishing up"),
    "splitting":    ("machine", "Splitting the work across processors"),
    "joining":      ("machine", "Joining the pieces back together"),
    "patches":      ("export",  "Naming the boundaries"),
    "checking":     ("check",   "Checking the cells"),
    "returning":    ("export",  "Sending the finished mesh back"),
    # the FEA ladder's last rung (engines/fea_tetwild): its fill, its improvement rounds, its face names
    "ftetwild":           ("fill",   "Filling the volume a second way"),
    "ftetwild_improving": ("check",  "Improving the elements, round by round"),
    "ftetwild_sizing":    ("refine", "Refining the elements to the sizes asked for"),
    "ftetwild_faces":     ("export", "Naming the faces after your CAD"),
}
#: A stage key this table does not know still narrates - as plain meshing, never as its raw label.
UNKNOWN_STAGE = ("fill", "Meshing your part")

#: The longest line a row or a commentary bubble carries.
MAX_CHARS = 240

# THE LEAK GUARD. A line the user reads never names a file or a path, a rule id, a mesher, a mesher's
# algorithm, a model or its provider, a prompt, or a config key. One list, tested.
_LEAKS = [
    re.compile(r"[\\/][\w.-]+[\\/]"),                                         # a path
    re.compile(r"\b[\w-]+\.(?:json|py|log|ya?ml|txt|sh|msh|inp|stl|step|stp|vtk|cdb|bdf|dict)\b", re.I),
    re.compile(r"\b[A-Z]{2,}(?:-[a-z]+)?-\d+\b"),                             # a rule id: RUN-4, PLAN-fea-13
    re.compile(r"\[[A-Z_]{3,}\]"),                                            # a gate tag: [FEA_PLAN]
    re.compile(r"\b\w+_\w+\b"),                                               # a snake_case key or a raw name
    re.compile(r"[{}<>=`]"),                                                  # markup, code, config
    re.compile(r"\b(?:snappy\w*|blockmesh|cartesianmesh|castellat\w*|cfmesh|gmsh|netgen|tetgen|ftetwild|"
               r"tetwild|mmg\w*|salome|meshadapt|delaunay|hxt|frontal|decomposepar|reconstructparmesh|"
               r"checkmesh|surfacefeature\w*)\b", re.I),                     # meshers and their internals
    re.compile(r"\b(?:gpt|openai|deepseek|deepinfra|claude|anthropic|llama|qwen|mistral|gemini|terra|"
               r"llm|prompt\w*|tokens?|model)\b", re.I),                     # models and their plumbing
]


def leaks(text: str) -> bool:
    """Whether `text` says something the user must never read (see _LEAKS)."""
    line = str(text or "")
    return any(rx.findall(line) for rx in _LEAKS)


def plain(text: str) -> str | None:
    """`text` as one tidy line, or None when it is empty, too long, or fails the leak guard."""
    line = " ".join(str(text or "").split())
    if not line or len(line) > MAX_CHARS or leaks(line):
        return None
    return line


def stage_words(key: str) -> tuple[str, str]:
    """(kind, words) for a mesher stage key, never the mesher's own label."""
    return STAGES.get(str(key or "").strip().lower(), UNKNOWN_STAGE)


def machine_words(machine: dict | None) -> str:
    """The machine sentence: "Meshing on a large machine (256 GB)", from the run's machine decision
    (contracts/mesh_machine). Its name and route are internal; its size is what the user can weigh."""
    m = machine or {}
    try:
        gib = float(m.get("memory_gib") or 0)
    except (TypeError, ValueError):
        gib = 0.0
    size = f" ({gib:.0f} GB)" if gib > 0 else ""
    return f"Meshing on a large machine{size}" if m.get("where") == "gce" else f"Meshing on a mesh machine{size}"


def still_words(words: str, elapsed_s: float) -> str:
    """The heartbeat on a stage that has not moved: "Still filling the volume · 34 min"."""
    minutes = max(1, int(round(float(elapsed_s or 0) / 60)))
    head = words[:1].lower() + words[1:] if words else "working"
    return f"Still {head} · {minutes} min"


def _publisher(publish: Any):
    return getattr(publish, "anarrate", None) if publish is not None else None


async def astep(publish: Any, kind: str, words: str, *, n: int | None = None, of: int | None = None,
                unit: str = "", op_id: str = "") -> bool:
    """Publish a step row. False when nothing was said (no publisher, an unknown kind, a leaking line).
    A lost claim (EventStreamError) is raised, as at every fenced boundary; any other failure is swallowed:
    narration is a courtesy, never a reason to fail a run."""
    line = plain(words)
    say = _publisher(publish)
    if say is None or line is None or kind not in KINDS:
        return False
    try:
        await say(kind, line, n=n, of=of, unit=unit, op_id=op_id)
    except EventStreamError:
        raise
    except Exception:  # noqa: BLE001 - the live line is a courtesy
        return False
    return True


async def asay(publish: Any, text: str, *, op_id: str = "") -> bool:
    """Publish one commentary line: what was decided and why, in the user's terms."""
    return await astep(publish, "say", text, op_id=op_id)


# # what the FEA driver says as it decides

#: Words in a decision's own sentence that read better for a user.
# the plan says "87 walls zone(s)" (its own group names are plural): one plural is enough - "87 wall areas"
_SOFTEN = [(re.compile(r"^Version \d+ meshes"), "Meshing"),
           (re.compile(r"\b(wall|hot spot|capped corner|round)s zone\(s\)"), r"\1 areas"), (re.compile(r"\bzone\(s\)"), "areas"), (re.compile(r"\belement\(s\)"), "elements"),
           (re.compile(r"\bpair\(s\)"), "pairs"), (re.compile(r"\bface\(s\)"), "faces")]


def soften(text: str) -> str:
    for rx, to in _SOFTEN:
        text = rx.sub(to, text)
    return text


def plan_lines(decisions: list[dict] | None, *, limit: int = 3) -> list[str]:
    """Up to `limit` commentary lines from a plan's decision records: their own words, softened, and
    only those that pass the guard. The records are the source of truth; nothing here is invented."""
    out: list[str] = []
    for d in decisions or []:
        words = plain(soften(str((d or {}).get("words") or "")))
        if words and words not in out:
            out.append(words)
        if len(out) >= limit:
            break
    return out


def retry_line(n: int, lever: dict | None) -> str:
    """Why pass `n` runs: the lever's own sentence when it reads plainly, else the plain fact."""
    words = plain(soften(str((lever or {}).get("words") or "")))
    if words:
        return f"Pass {n - 1} did not pass every check. {words[:1].upper()}{words[1:]}"
    return f"Pass {n - 1} did not pass every check, so pass {n} changes the sizes where it fell short."


def cfd_plan_lines(*, internal: bool, cells: int | None = None, layers: int | None = None,
                   wake: bool = False, boxes: int = 0, cells_across: int | None = None) -> list[str]:
    """What a CFD plan decided, in plain words, from its own numbers (engines/snappy/drivers)."""
    from meshpipeline.events import cells_words

    out = ["The fluid fills the inside of your part, so I'm meshing the space within it." if internal
           else "The flow goes around your part, so I'm meshing the space around it."]
    if internal and cells_across:
        out.append(f"At least {int(cells_across)} cells go across the narrowest passage, so the flow there is resolved.")
    if layers:
        out.append(f"{int(layers)} thin wall layers follow every wall, where the flow changes fastest.")
    if wake:
        out.append("The wake behind the part gets finer cells.")
    if boxes:
        out.append(f"{int(boxes)} more {'area gets' if int(boxes) == 1 else 'areas get'} finer cells where the flow needs them.")
    if cells:
        out.append(f"I expect about {cells_words(cells)} cells.")
    return [line for line in (plain(x) for x in out) if line]


def _criteria(keys) -> str:
    words = [str(k).replace("_", " ").strip() for k in keys if str(k).strip()]
    return ", ".join(words[:4]) + (f" and {len(words) - 4} more" if len(words) > 4 else "")


#: OpenFOAM's own checkMesh limits: past these the solver needs care (more correctors, a smaller step).
NON_ORTHO_LIMIT = 70.0
SKEWNESS_LIMIT = 4.0


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def quality_line(q: dict | None) -> str | None:
    """What the mesh's MEASURED quality says, in one plain line: the executor's own record
    (quality.json / the manifest's quality), never anything the reviewer wrote. None when there is
    nothing measured to say. FEA: inverted elements, the worst element (scaled Jacobian, 1 is
    perfect), elements through the thinnest wall. CFD: cells, worst non-orthogonality and skewness."""
    from math import floor

    from meshpipeline.events import cells_words

    q = q if isinstance(q, dict) else {}
    fea: dict = q["fea"] if isinstance(q.get("fea"), dict) else {}
    sicn, no, sk = _num(q.get("min_sicn")), _num(q.get("max_non_ortho")), _num(q.get("max_skewness"))
    cells = int(_num(q.get("cells")) or 0)
    if sicn is not None or fea:
        inverted = int(_num(q.get("fea_inverted")) or 0)
        if inverted > 0:
            return plain(f"{inverted} elements are inside out, so no solver can use this mesh yet.")
        bits = [f"{cells_words(cells)} elements"] if cells else []
        # the worst element is the scaled Jacobian, the figure the result screen and the report
        # show beside its limit (dev-areen 2026-10-30: the line said 0.33, the SICN, beside a 0.198)
        worst = _num(q.get("fea_min_scaled_jacobian"))
        worst = worst if worst is not None else sicn
        if worst is not None:
            bits.append(f"worst element {worst:.3g} (1 is perfect)")
        tw = _num(fea.get("through_wall_min"))
        if tw is not None:
            bits.append(f"every wall at least {max(1, floor(tw))} elements thick" if tw >= 2
                        else f"the thinnest wall {tw:.2g} elements thick")
        if not bits:
            return None
        notes = int(_num(q.get("elements_under_floor")) or 0) > 0 or bool(q.get("fea_rules_short")) \
            or (tw is not None and tw < 2)
        head = "The mesh is valid; some figures are past a limit and listed in the report" if notes \
            else "Quality looks good"
        return plain(f"{head}: {', '.join(bits)}.")
    if no is None and sk is None:
        return None
    bits = [f"{cells_words(cells)} cells"] if cells else []
    if no is not None:
        bits.append(f"worst non-orthogonality {no:.0f}°")
    if sk is not None:
        bits.append(f"worst skewness {sk:.2g}")
    steep = (no is not None and no > NON_ORTHO_LIMIT) or (sk is not None and sk > SKEWNESS_LIMIT)
    head = "The mesh is built, with a few steep cells the solver needs care with" if steep else "Quality looks good"
    return plain(f"{head}: {', '.join(bits)}.")


def review_line(*, passed: bool, failed_axes=(), rebuild_axes=(), blocking_axes=()) -> str:
    """The review's finding in one plain line, from the criteria it checked (never the reviewer's prose)."""
    if passed:
        return "The review checked the mesh against your brief, and it passes."
    if blocking_axes:
        return f"The review found this mesh answers a different problem, on: {_criteria(blocking_axes)}."
    if rebuild_axes:
        return f"The review asked for one more pass, to fix: {_criteria(rebuild_axes)}."
    if failed_axes:
        return f"The review noted points the delivery will list: {_criteria(failed_axes)}."
    return "The review noted points the delivery will list."


def pass_words(n: int, most: int) -> str:
    """A counted pass: "Pass 2 of up to 6"."""
    return f"Pass {int(n)} of up to {int(most)}"


__all__ = ["KINDS", "MAX_CHARS", "STAGES", "UNKNOWN_STAGE", "asay", "astep", "leaks", "machine_words",
           "pass_words", "plain", "plan_lines", "quality_line", "retry_line", "review_line", "cfd_plan_lines", "soften",
           "stage_words", "still_words"]
