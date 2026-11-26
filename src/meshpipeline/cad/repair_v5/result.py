# Responsibility: The CRS v5 beta's RESULT CONTRACT - `RepairResultV5`, what a job's repair says to the customer - and
#                 the plain-language mapping from v5's internal defects, statuses and events to it.
# Owns: `RepairResultV5`, `Problem`, the class -> plain type table, the status/reason wording, `ProgressMapper` (the v5
#       event stream as plain `repair_progress` events).
# Boundaries: no geometry, no kernel. It reads what the loop already recorded (`SeqCounters.history`, `MatchLog`, the
#             verdicts) and never decides anything about the part. THE HARD RULE (beta UX spec, "User-facing
#             language"): no text built here names internal architecture - no tournament, entrant, heat, rule seed,
#             fallback, conformal pass, class identifier, op name or exception text. Admin views read the full trace,
#             which travels beside this contract, never inside its text.
# Collaborates with: repair_v5/engine.py (builds it), cad/repair/conservative.py (carries it on the RepairReport),
#                    docs/superpowers/specs/2026-11-26-crs-v5-beta-ux.md ("Contract").
"""The CRS v5 beta result contract and its plain-language mapping."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

#: the defect class -> (the plain `type` key the contract carries, the words a user reads). Spec table, verbatim.
PROBLEM_TYPES: dict[str, tuple[str, str]] = {
    "close_faces": ("thin_gap", "thin gap"),
    "overlap_self_intersection": ("overlapping_parts", "overlapping parts"),
    "surface_crossing": ("intersecting_surfaces", "intersecting surfaces"),
    "facet_crossing_local": ("folded_surface", "folded surface"),
    "low_quality": ("tiny_feature", "tiny or sliver feature"),
    "open_shell_volume": ("hole_or_open_surface", "hole or open surface"),
    "invalid_brep_tolerance": ("damaged_geometry", "damaged geometry"),
    "closed_periodic_face": ("seam_problem", "seam problem"),
    "unknown": ("unidentified", "problem we couldn't identify"),
}
UNKNOWN_TYPE = PROBLEM_TYPES["unknown"]

#: the plain `type` key -> the words a user reads (what the UI shows for `Problem.type`)
TYPE_TEXT: dict[str, str] = dict(PROBLEM_TYPES.values())

ENGINES = ("v5", "v3")
CHOICES = ("meshing", "standalone", "off")
OUTCOMES = ("repaired", "partly_repaired", "unchanged", "unrepairable")
STATUSES = ("fixed", "not_fixed", "left_as_is")
STAGES = ("checking", "repairing", "broader_repair", "verifying", "done")

FIXED_TEXT = "Fixed"
#: why a problem was not fixed, by the loop's `skipped_why` key. Every key the loop can count is here or falls to the
#: tried-and-failed wording below (an op that declined or failed, a candidate that broke something else).
_LEFT_AS_IS: dict[str, str] = {
    "wall": "Left as is: there wasn't enough time to repair this one",
    "slice": "Left as is: there wasn't enough time to repair this one",
    "no_rule": "Left as is: we don't have a safe automatic repair for this kind of problem yet",
    "no_entrant": "Left as is: we don't have a safe automatic repair for this kind of problem yet",
    "unaimable": "Left as is: we don't have a safe automatic repair for this kind of problem yet",
    "unrepairable": "Left as is: this can't be repaired automatically without changing your design",
    "local_check_unavailable": "Left as is: we couldn't check a repair here safely",
    "start_verdict_inconclusive": "Left as is: we couldn't check a repair here safely",
    "layer1_entity_unmapped": "Left as is: we couldn't pin down exactly where this problem is",
    "unlocatable_after_earlier_repair": "Left as is: we couldn't pin down exactly where this problem is",
    "cluster_cap": "Left as is: there wasn't enough time to repair every part of this one",
}
NOT_FIXED_TEXT = "Couldn't fix this one: every repair we tried either didn't hold or caused a new problem"
NOT_REACHED_TEXT = "Left as is: there wasn't enough time to repair this one"
NOT_KEPT_TEXT = ("We found a repair for this, but the repaired part as a whole didn't pass our final check, "
                 "so your original is kept")
UNCHECKED_REPAIR_TEXT = "Left as is: we couldn't identify this problem precisely enough to repair it"

#: words the user may NEVER read in any text this module builds (the spec's hard rule), on top of
#: `contracts.narration.leaks` (paths, snake_case keys, meshers, models)
INTERNAL_WORDS = re.compile(
    r"\b(?:tournament\w*|entrant\w*|heats?|rule seed|seed|fallback|conformal\w*|search_ai|exception|traceback|"
    r"error|kernel|occt|opencascade|v[345])\b|\w+(?:Error|Exception)\b", re.I)


def is_plain(text: str) -> bool:
    """Whether `text` may be shown to a user: no internal word and nothing `narration.leaks` refuses."""
    from meshpipeline.contracts.narration import leaks

    return bool(text) and not INTERNAL_WORDS.search(text) and not leaks(text)


@dataclass(frozen=True)
class Location:
    faces: tuple[str, ...] = ()
    bodies: tuple[str, ...] = ()
    points: tuple[tuple[float, float, float], ...] = ()


@dataclass(frozen=True)
class Problem:
    id: str
    type: str
    location: Location
    status: str
    reason_text: str
    change_mm: float | None = None


@dataclass(frozen=True)
class RepairResultV5:
    """The beta's result contract ("Contract", docs/superpowers/specs/2026-11-26-crs-v5-beta-ux.md). PR 1 defines it;
    the API (PR 2) and the UI (PR 3) read it from the job's repair result (`repair_attempt.repair_result_v5`)."""

    engine: str
    choice: str
    outcome: str
    problems: tuple[Problem, ...] = ()
    meshers_passing: int | None = None
    meshers_total: int | None = None
    max_change_mm: float | None = None
    allowed_change_mm: float | None = None
    volume_ratio: float | None = None
    size_ratio: float | None = None
    flags: tuple[str, ...] = ()
    seconds: float = 0.0
    spend_usd: float = 0.0
    needs_review: bool = False
    auto_accepted: bool = False

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["problems"] = [{**asdict(p), "location": {"faces": list(p.location.faces),
                                                      "bodies": list(p.location.bodies),
                                                      "points": [list(x) for x in p.location.points]}}
                           for p in self.problems]
        out["flags"] = list(self.flags)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepairResultV5:
        """The contract back from `to_dict` (the v5 child process hands its result over as JSON)."""
        problems = tuple(
            Problem(id=str(p["id"]), type=str(p["type"]),
                    location=Location(faces=tuple(p["location"].get("faces") or ()),
                                      bodies=tuple(p["location"].get("bodies") or ()),
                                      points=tuple((float(x[0]), float(x[1]), float(x[2]))
                                                   for x in p["location"].get("points") or ())),
                    status=str(p["status"]), reason_text=str(p["reason_text"]), change_mm=p.get("change_mm"))
            for p in d.get("problems") or ())
        return cls(**{**{k: v for k, v in d.items() if k not in ("problems", "flags")},
                      "problems": problems, "flags": tuple(d.get("flags") or ())})

    @property
    def fixed(self) -> int:
        return sum(1 for p in self.problems if p.status == "fixed")


# --------------------------------------------------------------------------- problems


def plain_type(cls: str | None) -> str:
    return PROBLEM_TYPES.get(str(cls or "unknown"), UNKNOWN_TYPE)[0]


def type_words(cls: str | None) -> str:
    return PROBLEM_TYPES.get(str(cls or "unknown"), UNKNOWN_TYPE)[1]


MAX_POINTS = 20


def location_of(defect: Any) -> Location:
    ids = [str(e) for e in (getattr(defect, "entities", ()) or ())]
    for pair in getattr(defect, "pairs", ()) or ():
        ids.extend(str(e) for e in pair)
    ids = list(dict.fromkeys(ids))
    pts = tuple(tuple(float(c) for c in p)[:3] for p in (getattr(defect, "xyz", ()) or ())[:MAX_POINTS])
    return Location(faces=tuple(i for i in ids if i.startswith("F")),
                    bodies=tuple(i for i in ids if i.startswith("B")),
                    points=tuple((p[0], p[1], p[2]) for p in pts if len(p) == 3))


def _key(defect: Any) -> tuple[str, str]:
    return str(getattr(defect, "code", "")), str(getattr(defect, "id", ""))


def _status_of(last: dict[str, Any] | None) -> tuple[str, str]:
    """(status, reason_text) from a defect's LAST history entry (`SeqCounters.history`)."""
    if last is None or last["what"] in ("found", "unskipped"):
        return "left_as_is", NOT_REACHED_TEXT
    if last["what"] in ("fixed", "resolved"):
        return "fixed", FIXED_TEXT
    why = str(last.get("why") or "")
    if why in _LEFT_AS_IS:
        return "left_as_is", _LEFT_AS_IS[why]
    return "not_fixed", NOT_FIXED_TEXT


def change_by_defect(matches: Iterable[Any]) -> dict[str, float]:
    """The largest deviation of a WINNING candidate per defect id (mm): how far that repair moved the part."""
    out: dict[str, float] = {}
    for m in matches:
        if getattr(m, "result", None) == "won" and getattr(m, "deviation_mm", None) is not None:
            did = str(m.defect_id)
            out[did] = max(out.get(did, 0.0), float(m.deviation_mm))
    return out


def problems_from(history: Iterable[dict[str, Any]], *, matches: Iterable[Any] = (), delivered: bool,
                  clean: bool) -> tuple[Problem, ...]:
    """One `Problem` per distinct defect the loop found (code + site id, first seen first), with its final status.

    THE FINAL STATE OVERRIDES the per-defect history both ways, because the delivered part is what the customer gets:
    a part delivered CLEAN has every problem fixed (the broader repair may have fixed what the class repairs left), and
    a part NOT delivered has nothing fixed - a repair that held locally but was not kept is `not_fixed`, said so."""
    order: list[tuple[str, str]] = []
    defects: dict[tuple[str, str], Any] = {}
    last: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in history:
        k = _key(entry["defect"])
        if k not in defects:
            order.append(k)
        defects[k] = entry["defect"]          # the latest diagnosis carries the freshest class and location
        last[k] = entry
    moved = change_by_defect(matches)
    out: list[Problem] = []
    for n, k in enumerate(order, start=1):
        d = defects[k]
        status, reason = _status_of(last.get(k))
        if delivered and clean:
            status, reason = "fixed", FIXED_TEXT
        elif not delivered and status == "fixed":
            status, reason = "not_fixed", NOT_KEPT_TEXT
        change = moved.get(str(getattr(d, "id", ""))) if status == "fixed" and delivered else None
        out.append(Problem(id=f"p{n}", type=plain_type(getattr(d, "cls", None)), location=location_of(d),
                           status=status, reason_text=reason, change_mm=None if change is None else round(change, 6)))
    return tuple(out)


# --------------------------------------------------------------------------- the whole result


VOLUME_FLAG = 0.25     # the spec's gaming flags: a volume change over 25% ...
SHRINK_FLAG = 0.10     # ... or a bounding box that shrank by over 10% are reported (never gating)


def flags_of(volume_ratio: float | None, size_ratio: float | None) -> tuple[str, ...]:
    flags: list[str] = []
    if volume_ratio is not None and abs(volume_ratio - 1.0) > VOLUME_FLAG:
        flags.append("volume_changed")
    if size_ratio is not None and size_ratio < 1.0 - SHRINK_FLAG:
        flags.append("size_shrank")
    return tuple(flags)


def outcome_of(*, delivered: bool, clean: bool, problems: tuple[Problem, ...], input_clean: bool) -> str:
    if delivered:
        return "repaired" if clean else "partly_repaired"
    if input_clean or not problems:
        return "unchanged"
    return "unrepairable"


def review_of(*, delivered: bool, max_change_mm: float | None, threshold_mm: float | None,
              flags: tuple[str, ...]) -> tuple[bool, bool]:
    """(needs_review, auto_accepted). A delivered repair that moved the part beyond the auto-accept threshold, or
    carries a flag, goes to review; anything else delivered is auto-accepted. Nothing delivered needs no review."""
    if not delivered:
        return False, False
    beyond = (max_change_mm is not None and threshold_mm is not None and max_change_mm > threshold_mm)
    needs = bool(beyond or flags)
    return needs, not needs


def _opt(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def from_v5(*, choice: str, input_verdict: Any, final_verdict: Any, delivered: bool, history: Iterable[dict],
            matches: Iterable[Any], seconds: float, spend_usd: float,
            auto_accept_mm: float | None = None) -> RepairResultV5:
    """The contract for a v5 run. `final_verdict` is the delivered part's full verdict (the input's when nothing was
    delivered)."""
    clean = bool(getattr(final_verdict, "clean", False))
    problems = problems_from(history, matches=matches, delivered=delivered, clean=clean)
    panel = list(getattr(final_verdict, "panel", None) or ())
    faithful = getattr(final_verdict, "faithful", None) if delivered else None
    max_change = _opt(getattr(faithful, "max_dev_mm", None))
    allowed = _opt(getattr(faithful, "bound_mm", None))
    if allowed is None:
        allowed = _opt(getattr(getattr(input_verdict, "faithful", None), "bound_mm", None))
    volume_ratio = _opt(getattr(faithful, "volume_ratio", None))
    size_ratio = _opt(getattr(faithful, "bbox_ratio", None))
    flags = flags_of(volume_ratio, size_ratio)
    threshold = auto_accept_mm if auto_accept_mm is not None else allowed
    needs_review, auto_accepted = review_of(delivered=delivered, max_change_mm=max_change, threshold_mm=threshold,
                                            flags=flags)
    return RepairResultV5(
        engine="v5", choice=choice,
        outcome=outcome_of(delivered=delivered, clean=clean, problems=problems,
                           input_clean=bool(getattr(input_verdict, "clean", False))),
        problems=problems,
        meshers_passing=sum(1 for m in panel if getattr(m, "status", None) == "pass") if panel else None,
        meshers_total=len(panel) if panel else None,
        max_change_mm=max_change, allowed_change_mm=allowed, volume_ratio=volume_ratio, size_ratio=size_ratio,
        flags=flags, seconds=round(float(seconds), 1), spend_usd=round(float(spend_usd), 4),
        needs_review=needs_review, auto_accepted=auto_accepted)


def from_v3(*, choice: str, delivered: bool, seconds: float, max_change_mm: float | None = None,
            auto_accept_mm: float | None = None, nothing_needed: bool = False) -> RepairResultV5:
    """The contract for a run v3 finished after v5 failed (`engine` "v3"). v3 does not diagnose by class, so it lists
    no problems; the customer still learns whether their part was repaired."""
    needs_review, auto_accepted = review_of(delivered=delivered, max_change_mm=max_change_mm,
                                            threshold_mm=auto_accept_mm, flags=())
    outcome = "repaired" if delivered else "unchanged" if nothing_needed else "unrepairable"
    return RepairResultV5(engine="v3", choice=choice, outcome=outcome,
                          max_change_mm=max_change_mm, seconds=round(float(seconds), 1),
                          needs_review=needs_review, auto_accepted=auto_accepted)


# --------------------------------------------------------------------------- progress


@dataclass
class ProgressMapper:
    """The v5 event log (`repair_v5.events`, one record per event) as plain `repair_progress` events:
    {stage, problem_index, problem_total, problem_type, text}. Feed it every record (`events.add_listener`); it
    returns the progress event a record means, or None (most records mean nothing to a user, and a line that would
    repeat the last one is not repeated)."""

    found: int | None = None
    last_text: str = ""
    seen: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        got = self._map(rec)
        if got is None or got["text"] == self.last_text or not is_plain(got["text"]):
            return None
        self.last_text = got["text"]
        self.seen.append(got)
        return got

    @staticmethod
    def _event(stage: str, text: str, index: int | None = None, total: int | None = None,
               ptype: str | None = None) -> dict[str, Any]:
        return {"stage": stage, "problem_index": index, "problem_total": total, "problem_type": ptype, "text": text}

    def _map(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        event, phase = rec.get("event"), rec.get("phase")
        if event == "v5_start":
            return self._event("checking", "Checking your CAD")
        if event == "diagnosed":
            n = int(rec.get("defects") or 0)
            if self.found is None:
                self.found = n
                return self._event("checking", f"Found {n} problem{'s' if n != 1 else ''} to fix" if n
                                   else "No problems left to fix", total=n)
            return self._event("checking", f"Checking again: {n} problem{'s' if n != 1 else ''} left", total=n)
        if event == "phase" and phase == "conformal":
            return self._event("repairing", "Lining up parts that should touch")
        if event == "phase" and phase == "tournament":
            words = type_words(rec.get("defect_class"))
            index, total = rec.get("problem_index"), rec.get("problem_total")
            text = f"Repairing a {words}" if not words.startswith("problem") else "Repairing a problem"
            if index and total:
                text += f" - problem {index} of {total}"
            return self._event("repairing", text, index, total, plain_type(rec.get("defect_class")))
        if event == "defect_result":
            ptype = plain_type(rec.get("defect_class"))
            return self._event("repairing", "Fixed" if rec.get("what") in ("fixed", "resolved")
                               else "Couldn't fix this one - left as is", ptype=ptype)
        if event == "phase" and phase == "fallback":
            return self._event("broader_repair", "Trying a broader repair")
        if event == "phase" and phase in ("round_panel", "deliver"):
            return self._event("verifying", "Checking the repaired part")
        if event == "v5_done":
            fixed, total = int(rec.get("fixed") or 0), int(rec.get("total") or 0)
            text = (f"Repair finished: {fixed} of {total} fixed" if total else "Repair finished: nothing needed fixing")
            return self._event("done", text, total=total)
        return None


__all__ = ["CHOICES", "ENGINES", "OUTCOMES", "PROBLEM_TYPES", "STAGES", "STATUSES", "TYPE_TEXT", "Location", "Problem",
           "ProgressMapper", "RepairResultV5", "from_v3", "from_v5", "is_plain", "problems_from"]
