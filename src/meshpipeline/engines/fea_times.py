# Responsibility: WHERE AN FEA SOLID RUN'S TIME WENT (RUN-11): every step the run notes - the driver's (measuring, planning, each pass, delivering) and the mesher's own (engines/gmsh/fea_adapter.note_stage: sizes, surfaces, the fill, the improving passes, curving, the solver files) - kept as one line each in the workspace, then summed per step for the run's record, the report's "Where the time went" table and, when one step took most of the run, one line in the closing message.
# Boundaries: plain files and arithmetic; never fails a run (a line that cannot be kept is lost). The clock is the one the stage file already carries (`at`, engines/gmsh/fea_adapter.note_stage): no step is timed twice. quality.json carries no time (the same mesh gives the same record): the times live in their own file.
# Collaborates with: engines/gmsh/fea_adapter.py (notes the mesher's steps and the fill / improving details), engines/fea_driver.py (its own steps, and the summary at the end of the run), engines/fea_results.py (the `times` block), render/mesh_report.py (the table), contracts/user_notes.record_remark (the closing line).
"""THE RUN'S STEP LOG, AND WHAT IT SUMS TO.

Each line of LOG_FILE is {"key", "label", "at"} (a step began at `at`, epoch seconds), or {"key": "detail", ...}
(what a step did: the fill's algorithm and threads, each improving pass with its seconds and what it changed). A
step lasts until the next one begins; the last until the run's end. A "comparison" line (MESH-24: the pass meshed
again with its short edges as drawn) or a "refill" line (MESH-25: the fill failed and the pass is filled again the
other way) makes every step after it, up to the next pass, part of that one step."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

RULE = "RUN-11"
LOG_FILE = "fea_stage_log.jsonl"
TIMES_FILE = "fea_times.json"
#: one step over this share of the run is said in the closing message
MOST_SHARE = 0.5
#: the steps in plain words (the report's table), in the order a run takes them
NAMES: dict[str, str] = {
    "measuring": "Measuring the part",
    "planning": "Planning the element sizes",
    "sizing": "Setting the element sizes",
    "surface": "Meshing the surfaces",
    "volume": "Filling the solid with elements",
    "optimising": "Improving the elements (smoothing and swapping)",
    "quality_pass": "Improving the elements (the quality pass)",
    "curving": "Curving the elements onto the surface",
    "settling": "Checking the curved elements",
    "writing": "Measuring the elements and writing the solver files",
    "comparison": "A second mesh to compare, short edges as drawn",
    "refill": "Filling the solid again another way",
    "delivering": "Checking and delivering the mesh",
}
#: steps that hold every step after them, up to the next pass
_PHASES = ("comparison", "refill")
#: the fill's algorithm in plain words (no mesher's name reaches the user)
FILLS = {"hxt": "the fast fill, on {threads} threads", "delaunay": "the standard fill, on one thread"}
_LAST: dict[str, str] = {}


def log(ws, key: str, label: str = "", at: float | None = None, **detail) -> None:
    """One step's line (a repeat of the step just logged is not a new step: the stage file is re-noted while a
    step runs). Best effort."""
    try:
        path = Path(ws) / LOG_FILE
        if key != "detail" and _LAST.get(str(path)) == key:
            return
        row: dict = {"key": key, **({"label": label} if label else {}),
                     "at": round(float(time.time() if at is None else at), 2)}
        row.update(detail)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        if key != "detail":
            _LAST[str(path)] = key
    except (OSError, TypeError, ValueError):
        pass


def start(ws) -> None:
    """A run's first step: the log begins again (an earlier attempt's lines in the same workspace are not this
    run's), with measuring."""
    path = Path(ws) / LOG_FILE
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    _LAST.pop(str(path), None)
    log(ws, "measuring", "measuring the part")


def read(ws) -> list[dict]:
    """The log's lines, in order (a line that cannot be read is skipped)."""
    out: list[dict] = []
    try:
        text = (Path(ws) / LOG_FILE).read_text(encoding="utf-8")
    except OSError:
        return out
    for ln in text.splitlines():
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict) and "at" in row:
            out.append(row)
    return out


def summary(rows: Sequence[Mapping], end_at: float) -> dict | None:
    """The run's times: {rule, total_s, passes, stages: [{key, name, seconds}], fill?, improving?}; None with no
    step logged."""
    steps = [r for r in rows if r.get("key") != "detail"]
    if not steps:
        return None
    steps = sorted(steps, key=lambda r: float(r["at"]))
    secs: dict[str, float] = {}
    phase = ""
    passes = 0
    for i, r in enumerate(steps):
        key = str(r["key"])
        nxt = float(steps[i + 1]["at"]) if i + 1 < len(steps) else float(end_at)
        if key == "pass":
            passes += 1
            phase = ""
            continue
        if key in _PHASES:
            phase = key
        elif key in ("delivering", "measuring", "planning"):
            phase = ""
        name = phase or key
        secs[name] = secs.get(name, 0.0) + max(0.0, nxt - float(r["at"]))
    # a pass's own line takes no time of its own: its time is its steps'
    total = max(0.0, float(end_at) - float(steps[0]["at"]))
    out: dict = {"rule": RULE, "total_s": round(total, 1), "passes": passes,
                 "stages": [{"key": k, "name": NAMES.get(k, k.replace("_", " ").capitalize()),
                             "seconds": round(s, 1)} for k, s in secs.items()]}
    details = [r for r in rows if r.get("key") == "detail"]
    fill = next((r["fill"] for r in reversed(details) if r.get("fill")), None)
    if fill:
        out["fill"] = dict(fill)
    improving = next((r["improving"] for r in reversed(details) if r.get("improving") is not None), None)
    if improving:
        out["improving"] = list(improving)
    return out


def write(ws, end_at: float | None = None) -> dict | None:
    """The run's summary into TIMES_FILE (and returned)."""
    s = summary(read(ws), time.time() if end_at is None else float(end_at))
    if s is not None:
        try:
            (Path(ws) / TIMES_FILE).write_text(json.dumps(s, indent=1))
        except OSError:
            pass
    return s


def load(ws) -> dict | None:
    try:
        s = json.loads((Path(ws) / TIMES_FILE).read_text())
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) else None


def _clock(seconds: float, total: float) -> str:
    return f"{seconds / 60:.0f}" if total >= 120 else f"{seconds:.0f}"


def most_words(s: Mapping | None) -> str:
    """The closing message's line when one step took more than MOST_SHARE of the run, else ""."""
    if not s or not s.get("stages"):
        return ""
    total = float(s.get("total_s") or 0.0)
    top = max(s["stages"], key=lambda r: float(r["seconds"]))
    if total <= 0 or float(top["seconds"]) <= MOST_SHARE * total:
        return ""
    unit = "min" if total >= 120 else "s"
    return (f"Most of the time, {_clock(float(top['seconds']), total)} of {_clock(total, total)} {unit}, went to "
            f"{str(top['name'])[0].lower()}{str(top['name'])[1:]}.")


def table_rows(s: Mapping | None) -> list[list[str]]:
    """The report's rows: [step, time] - the fill with how it ran, each improving pass with what it did."""
    if not s:
        return []
    rows: list[list[str]] = []
    fill = s.get("fill") or {}
    for r in s.get("stages") or []:
        name = str(r["name"])
        if r["key"] == "volume" and fill.get("algorithm") in FILLS:
            name += f" ({FILLS[str(fill['algorithm'])].format(threads=int(fill.get('threads') or 1))})"
        said = [str(p.get("changed") or "") for p in s.get("improving") or [] if p.get("stage") == r["key"]]
        if said and said[0]:
            name += f": {said[0]}"
        rows.append([name, _duration(float(r["seconds"]))])
    rows.append(["The whole run", _duration(float(s.get("total_s") or 0.0))])
    return rows


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{int(seconds // 60)} min {int(round(seconds % 60)):02d} s"


__all__ = ["FILLS", "LOG_FILE", "MOST_SHARE", "NAMES", "RULE", "TIMES_FILE", "load", "log", "most_words", "read",
           "summary", "table_rows", "write"]
