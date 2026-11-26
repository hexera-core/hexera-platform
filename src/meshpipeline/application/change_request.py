# Responsibility: Turn "Request a change" on a delivered mesh (marks on the model plus words) into a requirement of a new run of the same part.
# Boundaries: pure readers and builders - the route owns the transaction and the dispatch; the reply is written by agents/intake/change_reply.
# Collaborates with: cad/face_map (the upload's own faces), application/face_pick (the session's face map), contracts/user_notes (the note), application/dispatch_contract (the new run's payload).
"""A CHANGE ASKED ON A DELIVERED MESH IS A REQUIREMENT, AND IT REBUILDS (docs/RULES.md VER-9..VER-12).

The person marks spots on the finished mesh, types what they want, and sends it. When the words ask for a
change ("finer here", "2 mm on this hole") it is a mesh requirement for a NEW run of the same part, exactly
like a message sent with faces picked at the plan step (NOTE-17): each mark is tied to the CAD face(s) under
it through the upload's own face map, the words become one more of the user's notes carrying those faces,
and the new run keeps everything the finished run had (its dispatch payload: the confirmed setup, the saved
settings as read then, every earlier note). The reviewer does not decide whether the change happens; it
reviews the new mesh afterwards with the change in its brief.

Words that only doubt the mesh ("this looks bad, look again") ask for a second look, not a change: they keep
the reviewer's re-inspection of the same mesh (VER-11).
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: VER-9: a change asked on a delivered mesh is a requirement of a new run, never a review of the old one
RULE_CHANGE_RUN = "VER-9"
#: VER-10: each mark is tied to the CAD face(s) under it, in the upload's own frame and unit
RULE_MARK_FACES = "VER-10"
#: VER-11: words that only doubt the mesh keep the reviewer's re-inspection, with marks it can show
RULE_DOUBT_REVIEW = "VER-11"
#: VER-12: the reply to a change request is written by the model, under the plan step's guard
RULE_REPLY = "VER-12"

#: what a request is, as the route answers it
KIND_CHANGE = "change"      # a new run with the requirement added
KIND_REVIEW = "review"      # the reviewer looks again at the same mesh (the old dispute path)
KIND_ASK = "ask"            # nothing could be tied to the part: nothing starts, we ask for the spot again

#: a face other than the one under the mark is meant only when the whole face is about the mark's size: the
#: wall of a hole or a fillet inside the circle, never a large face that merely passes under it
SMALL_FACE_SHARE = 4.0
#: the most faces one request ties (face_pick.MAX_PICKS, the plan step's own cap)
MAX_FACES = 20

# words that ASK FOR A CHANGE of the mesh: a number, a size, finer / coarser, layers, a count, quality words
_CHANGE = re.compile(
    r"\d|\b(?:finer|coarser|coarse|refine\w*|denser|dense|density|smaller|bigger|larger|more|fewer|less|"
    r"size|sizes|sizing|resolution|resolve|layers?|inflation|elements?|cells?|tets?|hex\w*|smooth\w*|"
    r"quality|curvature|mm|cm|um|micron\w*)\b", re.I)
# words that only DOUBT it and ask for a second look
_DOUBT = re.compile(
    r"\b(?:look again|take (?:another|a second) look|re-?check|check (?:it|this|that|again)|re-?review|"
    r"review (?:it|this|that|again)|inspect|verify|double[- ]check|is (?:this|it|that) (?:ok|okay|right|fine|good)|"
    r"looks? (?:bad|wrong|off|odd|weird|strange|broken)|seems? (?:bad|wrong|off|odd)|i (?:think|doubt|suspect))\b",
    re.I)
# a change the doubt words come with ("I think this should be finer") is still a change
_CHANGE_VERB = re.compile(r"\d|\b(?:finer|coarser|refine\w*|denser|smaller|bigger|larger|more|fewer|layers?)\b",
                          re.I)
# words that name ONE place, in the singular (NOTE-21's sense)
_ONE_PLACE = re.compile(r"\b(?:this|that|the)\s+(?:\w+\s+)?(?:hole|bore|fillet|edge|face|notch|corner|spot|area|"
                        r"region|slot|boss|rib|wall|groove|chamfer)\b(?!s)", re.I)


def _words(text: Any) -> str:
    return " ".join(str(text or "").split())


def is_change(comment: Any) -> bool:
    """VER-9 / VER-11, decided in code from the words, as the plan step's reader tells a requirement from a
    question (agents/intake/plan_talk): words that ask for a change of the mesh are a requirement; words that only
    doubt it, or ask a question about it, or no words at all, are a review. A doubt that also names the change
    ("I think it should be finer here") is a change."""
    from meshpipeline.agents.intake import plan_talk as PT

    words = _words(comment)
    if not words:
        return False
    if _DOUBT.search(words):
        return bool(_CHANGE_VERB.search(words)) and not PT.mesh_question(words)
    if PT.mesh_question(words) or (PT.asks(words) and not _CHANGE_VERB.search(words)):
        return False
    return bool(_CHANGE.search(words)) or PT.requirement_content(words)


#: VER-9 / PLAN-fea-36: "finer" / "coarser" with no number, on a change request - the one step against the size the
#: delivered mesh had at the marked faces
_FINER_WORDS = re.compile(r"\b(?:finer|refine\w*|denser|smaller|more elements|more cells)\b", re.I)
_COARSER_WORDS = re.compile(r"\b(?:coarser|coarsen\w*|bigger|larger|fewer elements|fewer cells)\b", re.I)
SIZE_STEP_FINER = "about half the size the last mesh had there"
SIZE_STEP_COARSER = "about twice the size the last mesh had there"


def size_step(comment: Any) -> str:
    """What a change request's words do to the size at the marked faces, in a few plain words: "finer" with no
    number halves what the delivered mesh had there, "coarser" doubles it (PLAN-fea-36 on a change request); a typed
    size (a number) is the size asked, said by its own words - "" then, and for words that set no size."""
    words = _words(comment)
    if not words or re.search(r"\d", words):
        return ""
    if _FINER_WORDS.search(words):
        return SIZE_STEP_FINER
    if _COARSER_WORDS.search(words):
        return SIZE_STEP_COARSER
    return ""


def one_place_words(comment: Any) -> bool:
    """The words name one place in the singular ("this hole", "that edge")."""
    return bool(_ONE_PLACE.search(_words(comment)))


# ------------------------------------------------------------------------------------- marks -> faces ----
@dataclass(frozen=True)
class MarkFaces:
    """One mark, tied: the faces it means ({id, label, sig}), the face right under it first. `faces` empty when
    the mark is on no face of the part (off the part, on a cut through it)."""

    ordinal: int
    faces: tuple[dict, ...] = ()
    radius_m: float = 0.0

    @property
    def tied(self) -> bool:
        return bool(self.faces)


def _point(flag: Mapping) -> tuple[float, float, float] | None:
    try:
        p = (float(flag["x"]), float(flag["y"]), float(flag["z"]))
    except (KeyError, TypeError, ValueError):
        return None
    return p if all(math.isfinite(v) for v in p) else None


def radius_of(flag: Mapping, *, span_factor: float) -> float:
    """The mark's radius in metres: the viewer sends `span` = its radius times the span factor
    (ui/js/viewer/viewer.js placeMarker, settings VIEWER_FLAG_SPAN_FACTOR)."""
    try:
        span = float(flag.get("span") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(span) or span <= 0 or span_factor <= 0:
        return 0.0
    return span / span_factor


def _near_faces(fm, p, r: float) -> dict[int, float]:
    """Face index -> its nearest distance to p (map metres), for every face that comes within r of p."""
    import numpy as np

    from meshpipeline.cad import face_map as FM

    if r <= 0 or not len(fm.tris):
        return {}
    xyz = fm.xyz.astype(float)
    a, b, c = (xyz[fm.tris[:, k]] for k in range(3))
    q = np.asarray(p, dtype=float)
    lo = np.minimum(np.minimum(a, b), c) - r
    hi = np.maximum(np.maximum(a, b), c) + r
    near = np.nonzero(np.all((lo <= q) & (q <= hi), axis=1))[0]
    if not len(near):
        return {}
    closest = FM._closest_on_triangles(q, a[near], b[near], c[near])
    dist = np.linalg.norm(closest - q, axis=1)
    out: dict[int, float] = {}
    for t, d in zip(near, dist, strict=True):
        if d <= r:
            k = int(fm.label[t])
            out[k] = min(out.get(k, math.inf), float(d))
    return out


def resolve_marks(fm, flags: Sequence[Mapping], *, scale: float = 1.0, span_factor: float) -> list[MarkFaces]:
    """VER-10: each mark tied to the CAD face(s) under it. A mark's point is in the delivered mesh's frame, in
    metres (contracts/mesh_units: every delivered mesh is in metres, in the part's own frame); the face map is in
    the skin's metres when it was made, and `scale` is the skin's unit now over the map's (face_pick.current), so
    the point is divided by it exactly as a click on the stage is (cad/face_map.face_at). The face right under the
    point is always meant; another face within the mark's radius is meant too when the whole face is about the
    mark's size (the wall of the hole the circle is drawn round, a fillet inside it) - a big face that only passes
    under the circle is not. A mark on no face (off the part, or on a cut face inside it) ties nothing."""
    from meshpipeline.cad import face_map as FM

    out: list[MarkFaces] = []
    s = float(scale) if scale and math.isfinite(scale) and scale > 0 else 1.0
    for i, flag in enumerate(flags, 1):
        p = _point(flag)
        r_m = radius_of(flag, span_factor=span_factor)
        if p is None or fm is None:
            out.append(MarkFaces(i, (), r_m))
            continue
        q = tuple(v / s for v in p)          # the map's own metres
        r_map = r_m / s
        normal = flag.get("normal") if isinstance(flag.get("normal"), (list, tuple)) else None
        under = FM.face_at(fm, p, normal, scale=s)
        near = _near_faces(fm, q, r_map)
        if under is None and near:
            # the point sits just off the skin (a shell's mid-surface, a cell centre on a curved wall): the
            # nearest face inside the circle is the one under it
            under = min(near, key=lambda k: near[k])
        if under is None:
            out.append(MarkFaces(i, (), r_m))
            continue
        chosen = [under]
        r_mm = r_map * 1000.0
        for k in sorted(near, key=lambda k: near[k]):
            if k != under and float(max(fm.extent_mm[k])) <= SMALL_FACE_SHARE * 2.0 * r_mm:
                chosen.append(k)
        faces = tuple({"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k, scale=s)}
                      for k in chosen)
        out.append(MarkFaces(i, faces, r_m))
    return out


def faces_of(marks: Sequence[MarkFaces]) -> list[dict]:
    """Every face the marks tie, once each, in mark order, at most MAX_FACES."""
    seen: set[str] = set()
    out: list[dict] = []
    for m in marks:
        for f in m.faces:
            if f["id"] not in seen and len(out) < MAX_FACES:
                seen.add(f["id"])
                out.append(dict(f))
    return out


def separate_spots(marks: Sequence[MarkFaces]) -> int:
    """How many separate places the tied marks are on: marks that share a face are one place (two marks round
    the same hole), marks with no face in common are two."""
    groups: list[set[str]] = []
    for m in marks:
        if not m.tied:
            continue
        ids = {f["id"] for f in m.faces}
        touching = [g for g in groups if g & ids]
        for g in touching:
            ids |= g
            groups.remove(g)
        groups.append(ids)
    return len(groups)


# ------------------------------------------------------------------------------------- the new run ----
def change_note(comment: str, faces: Sequence[Mapping], *, of_job_id: str = "") -> dict:
    """The user's words as one more of their notes, carrying the faces the marks tie (NOTE-17): word for word
    (NOTE-1), from the console - stamped with the finished run it was asked on (`change_of`, VER-9), so its "finer"
    or "coarser" is against the size that run had at those faces (PLAN-fea-36), never the mesh round them."""
    from meshpipeline.contracts import user_notes as UN

    return UN.make(str(comment or "")[:UN.MAX_NOTE_CHARS], source=UN.SOURCE_CONSOLE, faces=list(faces),
                   change_of=str(of_job_id or ""))


def brief_line(comment: str, faces: Sequence[Mapping], *, of_job_id: str) -> str:
    """What the reviewer of the NEW mesh is told about the change (it reviews the new mesh as any mesh, with this
    in its brief)."""
    where = ", ".join(str(f.get("label") or f.get("id")) for f in faces[:6]) or "the places they marked"
    return ("\n\nCHANGE ASKED ON THE PREVIOUS RESULT (job " + str(of_job_id)[:8] + "): the engineer marked "
            + where + " on the delivered mesh and asked: " + _words(comment)[:600]
            + "\nThis run was built with that as a requirement on those faces only. Check the mesh there "
              "and say what you measured; everything else was meant to stay as it was.")


def change_payload(parent_payload: Mapping, *, job_id: str, note: Mapping, brief: str) -> dict:
    """VER-9: the new run's dispatch payload - the finished run's own payload (the confirmed setup, the saved
    settings as read then, every earlier note), with this note added and the change in the review brief. Nothing
    else changes: the engine, the purpose, the patches, the unit and the fingerprint the approval bound stay."""
    from meshpipeline.application.dispatch_contract import build
    from meshpipeline.contracts.user_notes import merge

    fields = copy.deepcopy({k: v for k, v in dict(parent_payload or {}).items() if k != "schema_version"})
    fields.pop("user_dispute", None)
    fields["job_id"] = str(job_id)
    fields["user_notes"] = merge(fields.get("user_notes") or [], [dict(note)])
    fields["review_brief_txt"] = str(fields.get("review_brief_txt") or "") + brief
    return build(**fields)


def minutes_of(started, ended) -> int | None:
    """The finished run's time in whole minutes (at least 1), or None when it is not known."""
    try:
        secs = (ended - started).total_seconds()
    except Exception:  # noqa: BLE001 - an unknown time is said as unknown
        return None
    return max(1, round(secs / 60.0)) if secs > 0 else None


# ------------------------------------------------------------------------------------- what is said ----
@dataclass
class ChangeFacts:
    """What the system really did with the request - the reply is written from this and checked against it."""

    kind: str
    words: str
    faces: list[dict] = field(default_factory=list)
    marks: int = 0
    untied: list[int] = field(default_factory=list)
    spots: int = 0
    one_place: bool = False
    flow_cannot_size_a_face: bool = False
    last_run_minutes: int | None = None
    #: "about half the size the last mesh had there" (size_step), "" when the words set no such step
    size_change: str = ""

    def where(self) -> str:
        labels = [str(f.get("label") or f.get("id")) for f in self.faces]
        if not labels:
            return ""
        if len(labels) <= 3:
            return ", ".join(labels)
        return f"{', '.join(labels[:2])} and {len(labels) - 2} more faces"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "message": {"words": self.words, "faces_picked": [str(f.get("label") or f.get("id")) for f in self.faces]},
            "what_happens": {KIND_CHANGE: "a new run of the same part starts now, with their words as a requirement "
                                          "on the faces they marked and nothing else changed",
                             KIND_ASK: "nothing starts: no mark is on the part, so we ask them to mark the spot on "
                                       "the part again"}[self.kind],
            "marks": self.marks,
        }
        if self.kind == KIND_CHANGE and self.faces:
            out["applied"] = [{"what": "what their words ask", "where": self.where(),
                               "place": "they marked it on the model"}]
            out["nothing_else_changes"] = True
            if self.size_change:
                out["size_change_there"] = self.size_change
        elif self.kind == KIND_CHANGE:
            # no spot marked: the words are one more requirement of the new run, placed by the run's own reader
            # from the words (NOTE-2, NOTE-8) - kept, never said as set
            out["kept_for_the_run"] = True
            out["no_marks"] = ("no spot was marked: their words are kept as a requirement of the new run, the run "
                               "places them from the words, and the result will say what came of it")
        if self.untied:
            out["marks_not_on_the_part"] = {"which": self.untied,
                                            "said": "these marks are on no face of the part (off it, or on a cut "
                                                    "through it), so nothing was set there"}
        if self.one_place and self.spots > 1:
            out["one_place_words_several_spots"] = {
                "spots": self.spots, "done": "the change goes on every spot they marked; say so, and that they can "
                                             "remove a mark and send again if they meant one"}
        if self.flow_cannot_size_a_face:
            out["flow_limit"] = ("this flow mesher cannot size one face on its own; the run will refine what it can "
                                 "there and the result will say what it did")
        if self.last_run_minutes:
            out["last_run_took_minutes"] = self.last_run_minutes
        out["plain_version_that_is_exactly_right"] = plain_reply(self)
        return out


def plain_reply(f: ChangeFacts) -> str:
    """The code sentence said when the model's reply is refused, fails or is slow (VER-12's fallback)."""
    if f.kind == KIND_ASK:
        return ("I couldn't find the part under your marks, so nothing has started. Mark the spot on the part "
                "itself and send it again.")
    if f.faces and f.size_change:
        step = "coarser" if f.size_change == SIZE_STEP_COARSER else "finer"
        out = [f"Got it: {step} on {f.where()} only, {f.size_change}, and nothing else changes."]
    elif f.faces:
        out = [f"Got it: I'll make that change on {f.where()} only, and nothing else changes."]
    else:
        out = ["Noted. I've kept that as a requirement of the new run, and the result will say what came of it."]
    if f.untied:
        n = len(f.untied)
        out.append(f"{'One mark is' if n == 1 else f'{n} marks are'} not on the part, so I left "
                   f"{'it' if n == 1 else 'them'} out.")
    if f.one_place and f.spots > 1:
        out.append(f"You marked {f.spots} separate spots, so it goes on all of them.")
    if f.flow_cannot_size_a_face:
        out.append("This flow mesher can't size one face on its own, so the result will say what it did there.")
    out.append("Starting a new run" + (f"; the last one took {f.last_run_minutes} min." if f.last_run_minutes
                                       else "."))
    return " ".join(out)


def said_in_chat(comment: Any, *, marks: int, where: str = "") -> str:
    """The person's side of a change request as the conversation keeps it: their words as typed, then one plain
    line for the marks ("2 spots marked: Face 12 · round Ø15 mm"), with the face labels the reply names."""
    words = str(comment or "").strip()
    if not marks:
        return words
    note = f"{marks} spot{'' if marks == 1 else 's'} marked" + (f": {where}" if where else "")
    return f"{words}\n\n{note}" if words else note


__all__ = ["KIND_ASK", "KIND_CHANGE", "KIND_REVIEW", "MAX_FACES", "RULE_CHANGE_RUN", "RULE_DOUBT_REVIEW",
           "RULE_MARK_FACES", "RULE_REPLY", "SIZE_STEP_COARSER", "SIZE_STEP_FINER", "SMALL_FACE_SHARE", "ChangeFacts", "MarkFaces", "brief_line",
           "change_note", "change_payload", "faces_of", "is_change", "minutes_of", "one_place_words", "plain_reply",
           "radius_of", "resolve_marks", "said_in_chat", "separate_spots", "size_step"]
