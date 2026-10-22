# Responsibility: Say, step by step and in plain words, how far the geometry check has got - the
# worker records each step as it starts, and the API turns that record into the live line the
# chat shows: the step now running, the steps done with their times, a "still working" note once a
# step runs long for the size of the file, and a plain message with the way on when it fails.
# Boundaries: the worker half writes one small JSON object beside the check and never raises; the
# view is pure. Neither decides the check's outcome: the stored check and its time box
# (application/geometry_check) do that, and this only reports it.
from __future__ import annotations

import contextvars
import json
import logging
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: The steps, in order. The scout runs the first five the moment the upload lands; the naming
#: runs once the user has said what the part is.
QUEUED, READING, MEASURING, OPENINGS, PICTURES, NAMING = (
    "queued", "reading", "measuring", "openings", "pictures", "naming")
SCOUT_STEPS = (QUEUED, READING, MEASURING, OPENINGS, PICTURES)
#: What each step is called while it runs, and once it is done. A thermal model (an ECXML upload)
#: is read from its own file: no openings are searched for, and its "naming" sets up its panel.
_WORDS = {
    QUEUED: ("Starting…", "Queued"),
    READING: ("Reading your CAD…", "Read your CAD"),
    MEASURING: ("Measuring the shape…", "Measured the shape"),
    OPENINGS: ("Finding the openings…", "Found the openings"),
    PICTURES: ("Drawing the pictures…", "Drew the pictures"),
    NAMING: ("Naming what it sees…", "Named what it sees"),
}
_THERMAL_WORDS = {
    READING: ("Reading your thermal model…", "Read your thermal model"),
    MEASURING: ("Sizing the grid…", "Sized the grid"),
    PICTURES: ("Drawing the model…", "Drew the model"),
    NAMING: ("Setting up the panel…", "Set up the panel"),
}
#: AN FEA PART (the user's words sent it to stress analysis) has no openings and nothing to name: the
#: step that reads the drawn surface is said as that, and the "naming" is where its walls and hot
#: spots are measured (application/geometry_check._name). Areen's F1 car showed "Found the openings"
#: on its FEA check (21 Oct): no CFD words on the FEA path.
_FEA_WORDS = {
    OPENINGS: ("Reading the surface…", "Read the surface"),
    NAMING: ("Measuring the walls and hot spots…", "Found the walls and hot spots"),
}
#: What a failed step is said as: "Reading your CAD took too long" / "Couldn't finish reading your CAD".
_DOING = {QUEUED: "starting", READING: "reading your CAD", MEASURING: "measuring the shape",
          OPENINGS: "finding the openings", PICTURES: "drawing the pictures", NAMING: "naming what it sees"}
_FEA_DOING = {OPENINGS: "reading the surface", NAMING: "measuring the walls and hot spots"}

#: After this long with no worker on the scout, the line says it is waiting to start; after the
#: second, why. Production always has a worker on the queue; a dev stack may not.
WAITING_AFTER_S, WAITING_NOTE_AFTER_S = 10.0, 60.0
#: A step past its soft limit gets a "still working" note; past this many soft limits, a note that
#: it is far slower than it should be and when it stops by itself.
VERY_SLOW_FACTOR = 3.0
#: Notes say "big file" from this size up.
BIG_FILE_MB = 20.0


def progress_key(session_id: str) -> str:
    """Where the record lives: beside the check's scout.json (application/geometry_check)."""
    return f"sessions/{session_id}/geometry_check/progress.json"


# ------------------------------------------------------------------------- the worker half ----
class Tracker:
    """One task's record: which step runs now and when each began. Every write is best effort -
    a store that refuses it costs the user the live line, never the check - and after a few
    refusals it stops trying, so a broken store does not slow the check down."""

    _MAX_FAILURES = 3

    def __init__(self, session_id: str, phase: str):
        self.session_id, self.phase = session_id, phase      # phase: "scout" or "naming"
        self._record: dict = {}
        self._failures = 0

    def begin(self, *, work: dict | None = None, **fields) -> None:
        """The worker picked the task up. A scout starts the whole record again; a naming keeps
        the scout's steps beside its own."""
        now = time.time()
        current = (read_progress(self.session_id) or {}) if self.phase != "scout" else {}
        current.pop(self.phase, None)
        self._record = current
        self._record[self.phase] = {"picked_at": now, "steps": {}, "current": None, **fields,
                                    "work": {k: v for k, v in (work or {}).items() if v is not None}}
        self._write()

    def work(self, **fields) -> None:
        """How big the job is, as soon as it is known: the file's size, the skin's triangles."""
        part = self._record.get(self.phase)
        if part is None:
            return
        part.setdefault("work", {}).update({k: v for k, v in fields.items() if v is not None})

    def step(self, key: str, **work) -> None:
        """A step starts; the one before it ends now."""
        part = self._record.get(self.phase)
        if part is None:
            return
        if work:
            self.work(**work)
        now = time.time()
        was = part.get("current")
        if was and was in part["steps"]:
            part["steps"][was].setdefault("end", now)
        part["steps"][key] = {"at": now}
        part["current"] = key
        self._write()

    def finish(self, ok: bool) -> None:
        """The task ended. A failure keeps the step it failed in as `current`."""
        part = self._record.get(self.phase)
        if part is None:
            return
        now = time.time()
        part["ended_at"] = now
        part["ok"] = bool(ok)
        was = part.get("current")
        if ok and was and was in part["steps"]:
            part["steps"][was].setdefault("end", now)
        self._write()

    def _write(self) -> None:
        if self._failures >= self._MAX_FAILURES:
            return
        try:
            from meshpipeline.contracts.object_storage import get_object_store

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(self._record, fh)
                tmp = Path(fh.name)
            try:
                get_object_store().upload_file(local_path=tmp, object_key=progress_key(self.session_id))
            finally:
                tmp.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - the live line is a courtesy; the check goes on
            self._failures += 1
            logger.info("geometry progress: could not record the step (%s) - session=%s",
                        type(exc).__name__, self.session_id)


_ACTIVE: contextvars.ContextVar[Tracker | None] = contextvars.ContextVar("geometry_progress", default=None)


def track(tracker: Tracker | None):
    """Make `tracker` the one `mark` writes to, for the task running in this context."""
    return _ACTIVE.set(tracker)


def untrack(token) -> None:
    _ACTIVE.reset(token)


def mark(key: str, **work) -> None:
    """The step `key` starts, in whichever task is running here; nothing when none is tracked
    (a test, a caller outside the worker)."""
    tracker = _ACTIVE.get()
    if tracker is not None:
        tracker.step(key, **work)


def note_work(**work) -> None:
    tracker = _ACTIVE.get()
    if tracker is not None:
        tracker.work(**work)


def skin_triangles(skin: Path) -> int | None:
    """A binary STL's triangle count from its size alone (84 bytes, then 50 per triangle)."""
    try:
        return max(0, (Path(skin).stat().st_size - 84) // 50)
    except OSError:
        return None


def read_progress(session_id: str) -> dict | None:
    """The record as it stands; None when there is none or it cannot be read."""
    try:
        from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

        try:
            value = json.loads(get_object_store().get_bytes(object_key=progress_key(session_id)))
        except ObjectNotFound:
            return None
        return value if isinstance(value, dict) else None
    except Exception as exc:  # noqa: BLE001 - a line without the steps is still a true line
        logger.info("geometry progress: unreadable (%s) - session=%s", type(exc).__name__, session_id)
        return None


# ------------------------------------------------------------------------------- the view ----
def soft_limit_s(key: str, work: dict | None) -> float:
    """How long a step may take for this much work before the line says it is still working.
    Scaled by what the step reads: the file's size for reading and measuring, the skin's
    triangles for finding the openings and drawing. Generous on purpose - the corpus sweep saw a
    0.2 MB STEP take twelve minutes - so the note means "longer than most", never "stuck"."""
    w = work or {}
    mb = max(0.0, float(w.get("size_bytes") or 0) / 1e6)
    ktris = max(0.0, float(w.get("triangles") or 0) / 1000.0)
    if key == QUEUED:
        return WAITING_NOTE_AFTER_S
    if key == READING:
        return 20.0 + 2.0 * mb
    if key == MEASURING:
        return 30.0 + 4.0 * mb
    if key == OPENINGS:
        return 20.0 + 0.2 * ktris
    if key == PICTURES:
        return 30.0 + 0.1 * ktris
    if key == NAMING:
        return 60.0
    return 60.0


def _words(key: str, thermal: bool, fea: bool = False) -> tuple[str, str]:
    return ((_THERMAL_WORDS.get(key) if thermal else None) or (_FEA_WORDS.get(key) if fea else None)
            or _WORDS[key])


def is_fea(stored: dict | None, requested: dict | None) -> bool:
    """Whether the check is an FEA part's: its measurement is stored, or the words it was named
    with (the request's, else the stored check's) send it to stress analysis."""
    facts = (stored or {}).get("facts")
    if isinstance(facts, dict):
        from meshpipeline.cad.fea_measure import FACT_KEY

        if FACT_KEY in facts:
            return True
    text = str((requested or {}).get("purpose_text") or (stored or {}).get("purpose_text") or "")
    if not text.strip():
        return False
    from meshpipeline.application.geometry_check import is_fea_purpose

    return is_fea_purpose(text)


def _size_words(work: dict) -> str:
    mb = float(work.get("size_bytes") or 0) / 1e6
    if mb <= 0:
        return ""
    return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


def _slow_note(key: str, work: dict, thermal: bool, fea: bool = False) -> str:
    """What the line adds once a step runs past its soft limit."""
    if fea and key == NAMING:
        return "Still measuring - a part with many faces takes longer"
    if fea and key == OPENINGS:
        return "Still reading - a part with many faces takes longer"
    size = _size_words(work)
    big = float(work.get("size_bytes") or 0) / 1e6 >= BIG_FILE_MB
    if key == READING:
        return (f"Still reading - big file ({size}), this can take a few minutes" if big
                else "Still reading - this file takes longer than most")
    if key == MEASURING:
        if thermal:
            return "Still sizing - a model with many parts takes longer"
        return (f"Still measuring - big file ({size}), this can take a few minutes" if big
                else "Still measuring - a detailed part takes longer")
    if key == OPENINGS:
        return "Still looking - a part with many faces takes longer"
    if key == PICTURES:
        return "Still drawing - a big part takes longer to draw"
    if key == NAMING:
        return "Still naming - the model is slow right now"
    return ""


def _advice(step: str, suffix: str, timed_out: bool, can_retry: bool, fea: bool = False) -> str:
    """What to do about a failed step, in one or two short sentences."""
    s = (suffix or "").lower()
    if step == NAMING and fea:
        return ("The part is drawn: check its size and unit and press Proceed - the walls and hot spots "
                "are measured again when it is meshed" + (", or try again." if can_retry else "."))
    if step == NAMING:
        return ("The picture is ready: name the openings on it yourself and press Proceed"
                + (", or try again." if can_retry else "."))
    if not can_retry:
        return "Carry on in the chat - I'll ask about the part there."
    if s in (".igs", ".iges") and step in (READING, MEASURING):
        return "Try again. IGES files often trip it up - export the part as STEP and upload that instead."
    if timed_out:
        return "Try again. A smaller or simpler file is quicker - or carry on in the chat."
    if s in (".step", ".stp") and step in (READING, MEASURING):
        return "Try again. If it fails again, export a fresh STEP from your CAD tool, or carry on in the chat."
    if s in (".stl", ".obj", ".vtp"):
        return "Try again. If it fails again, check the surface is closed (no holes or gaps), or carry on in the chat."
    return "Try again, or carry on in the chat."


def _plain_reason(reason: str) -> str:
    """The stored reason as a sentence, without the exception's class name in front of it."""
    text = str(reason or "").strip()
    head, sep, rest = text.partition(": ")
    if sep and head.replace("_", "").isalnum() and head[:1].isupper() and " " not in head:
        text = rest.strip()
    text = text[:240].rstrip(" .")
    return f"{text[:1].upper()}{text[1:]}." if text else ""


def _seconds(step: dict | None, now: float) -> float | None:
    if not isinstance(step, dict) or step.get("at") is None:
        return None
    end = step.get("end")
    return max(0.0, float(end if end is not None else now) - float(step["at"]))


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def duration_words(seconds: float) -> str:
    """14 s, 2 min 5 s, 12 min."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    m, r = divmod(s, 60)
    return f"{m} min {r} s" if r and m < 10 else f"{m} min"


def progress_view(stored: dict | None, requested: dict | None, progress: dict | None,
                  now: float | None = None, *, scout_limit_s: float, naming_limit_s: float,
                  confirmed: bool = False, fea: bool | None = None) -> dict | None:
    """THE LIVE LINE, as the chat shows it, from the check as reported (through its time box), the
    naming request and the worker's record. Pure. `state` is one of queued, running, waiting (on
    the user's answer), done, failed or unsupported; `live` says whether the mark animates - never
    on a check that waits on the user, is done or failed, so nothing spins forever. Every step
    carries its seconds; `note` is the "still working" line, and a failure carries `message`
    (as `current`), `reason` and `advice`. `scout_limit_s` / `naming_limit_s` are the worker's own
    limits, after which a step stops by itself. A check the user has `confirmed` is done whatever
    the naming did: the user went on without it. `fea` says the part is on the FEA path (its steps
    are said in FEA words); None works it out from the check. None when there is no check, or it
    is off."""
    if not stored:
        return None
    at = time.time() if now is None else now
    status = str(stored.get("status") or "")
    rec = progress if isinstance(progress, dict) else {}
    scout = rec.get("scout") if isinstance(rec.get("scout"), dict) else None
    naming = rec.get("naming") if isinstance(rec.get("naming"), dict) else None
    written_at = float(stored.get("written_at") or 0.0)
    if status == "pending" and scout is not None and not _this_attempt(scout, written_at):
        # a record from an earlier attempt: no worker has picked this one up yet
        scout, naming = None, None
    asked_at = float((requested or {}).get("requested_at") or 0.0)
    if naming is not None and (not requested or float(naming.get("picked_at") or 0.0) < asked_at - 30.0):
        naming = None                    # an earlier naming's record, not this request's
    thermal = stored.get("check_mode") == "thermal" or (scout or {}).get("mode") == "thermal"
    # an FEA part's steps are said in its own words: the route the API settled from the user's
    # words (`fea`), else what the stored check and the naming request say
    fea = not thermal and (fea if fea is not None else is_fea(stored, requested))
    work = dict((scout or {}).get("work") or {})
    suffix = str(work.get("suffix") or (stored.get("source") or {}).get("suffix_hint") or "")
    scout_steps = (scout or {}).get("steps") or {}
    naming_steps = (naming or {}).get("steps") or {}

    steps: list[dict] = []
    for key in [k for k in SCOUT_STEPS if not (thermal and k == OPENINGS)] + [NAMING]:
        doing, done = _words(key, thermal, fea)
        seconds = _seconds((naming_steps if key == NAMING else scout_steps).get(key), at)
        steps.append({"key": key, "label": done, "doing": doing, "state": "todo", "seconds": _round(seconds)})
    by_key = {s["key"]: s for s in steps}
    if scout is not None and scout.get("marker"):
        by_key[QUEUED]["seconds"] = _round(max(0.0, float(scout["picked_at"]) - float(scout["marker"])))
    view: dict = {"steps": steps, "thermal": thermal, "fea": fea, "note": "", "live": False}

    if status == "unsupported":
        view.update(state="unsupported", current="This file can't be checked here",
                    reason=_plain_reason(str(stored.get("reason") or "")),
                    advice="Carry on in the chat - I'll ask about the part there.", can_retry=False)
        return view
    if status == "pending":
        return _scouting(view, by_key, scout, scout_steps, work, thermal, at, written_at, scout_limit_s, fea)
    if status == "failed" and stored.get("step") != "naming":
        return _scout_failed(view, stored, by_key, scout, scout_steps, suffix, fea)
    if status not in ("scouted", "ready", "failed"):
        return None

    # the part is measured and drawn: every scout step is done, the naming is what is left
    for s in steps:
        if s["key"] != NAMING:
            s["state"] = "done"
    if scout is None and stored.get("seconds") is not None:
        # a check stored before the steps were recorded: its whole scout time, on the first step
        by_key[READING]["seconds"] = _round(float(stored["seconds"]))
    nm = by_key[NAMING]
    if confirmed and status != "ready":
        nm.update(state="skipped", seconds=None)    # the user proceeded on the measuring step's names
    if status == "ready" or confirmed:
        if status == "ready":
            nm["state"] = "done"
        if nm["state"] == "done" and nm["seconds"] is None and stored.get("naming_seconds") is not None:
            nm["seconds"] = _round(float(stored["naming_seconds"]))
        total = sum(float(s["seconds"] or 0.0) for s in steps if s["key"] != QUEUED)
        view.update(state="done", current=f"Checked in {duration_words(total)}", total_s=_round(total))
        return view
    if status == "failed":
        return _failed(view, stored, NAMING, nm, suffix, fea)
    if not requested or requested.get("withdrawn"):
        nm["state"] = "waiting"
        view.update(state="waiting", current=("Model ready - answer in the chat and I'll set up the panel" if thermal
                                              else "Picture ready - answer in the chat and I'll name what I see"))
        return view
    nm["state"] = "active"
    if naming is None:
        waited = max(0.0, at - asked_at) if asked_at else 0.0
        nm["seconds"] = _round(waited)
        view.update(state="running", live=True, elapsed_s=_round(waited),
                    current=nm["doing"] if waited < WAITING_AFTER_S else "Waiting to start naming…")
        if waited >= WAITING_NOTE_AFTER_S:
            view["note"] = "No worker is free yet. This can take a few minutes when it's busy."
        return view
    picked = float(naming.get("picked_at") or at)
    started = (naming_steps.get(NAMING) or {}).get("at")
    elapsed = max(0.0, at - float(started if started is not None else picked))
    nm["seconds"] = _round(elapsed)
    view.update(state="running", live=True, current=nm["doing"], elapsed_s=_round(elapsed))
    _add_slow_note(view, NAMING, elapsed, work, thermal, remaining=picked + naming_limit_s - at, fea=fea)
    return view


def _this_attempt(scout: dict, written_at: float) -> bool:
    """Whether the worker's record belongs to the pending scout as stored: it names the marker it
    started from, or - when it could not read one - began after the marker was written."""
    marker = scout.get("marker")
    if marker is not None:
        return abs(float(marker) - written_at) < 1e-3
    return float(scout.get("picked_at") or 0.0) >= written_at - 1.0


def _scouting(view, by_key, scout, scout_steps, work, thermal, at, written_at, scout_limit_s, fea=False) -> dict:
    """The scout is still running - or no worker has picked it up yet."""
    queued = by_key[QUEUED]
    if scout is None:
        waited = max(0.0, at - written_at) if written_at else 0.0
        queued.update(state="active", seconds=_round(waited))
        view.update(state="queued", live=True, elapsed_s=_round(waited),
                    current="Waiting to start…" if waited >= WAITING_AFTER_S else queued["doing"])
        if waited >= WAITING_NOTE_AFTER_S:
            view["note"] = "No worker is free yet. This can take a few minutes when it's busy."
        return view
    queued["state"] = "done"
    active = scout.get("current") if scout.get("current") in by_key else READING
    reached = False
    for s in by_key.values():
        if s["key"] in (QUEUED, NAMING):
            continue
        if s["key"] == active:
            s["state"], reached = "active", True
        elif not reached:
            s["state"] = "done" if s["key"] in scout_steps else "skipped"
    act = by_key[active]
    if act["seconds"] is None:
        act["seconds"] = _round(max(0.0, at - float(scout.get("picked_at") or at)))
    elapsed = float(act["seconds"] or 0.0)
    view.update(state="running", live=True, current=act["doing"], elapsed_s=_round(elapsed))
    _add_slow_note(view, active, elapsed, work, thermal,
                   remaining=float(scout.get("picked_at") or at) + scout_limit_s - at, fea=fea)
    return view


def _scout_failed(view, stored, by_key, scout, scout_steps, suffix, fea=False) -> dict:
    """The scout failed: in the step its worker was in, else before any worker started it."""
    current = (scout or {}).get("current")
    failed_key = str(current) if current in by_key and current != NAMING else (READING if scout else QUEUED)
    for s in by_key.values():
        if s["key"] == failed_key:
            break
        if s["key"] == QUEUED or s["key"] in scout_steps:
            s["state"] = "done"
    return _failed(view, stored, failed_key, by_key[failed_key], suffix, fea)


def _add_slow_note(view: dict, key: str, elapsed: float, work: dict, thermal: bool, *, remaining: float,
                   fea: bool = False) -> None:
    limit = soft_limit_s(key, work)
    if elapsed < limit:
        return
    if elapsed >= VERY_SLOW_FACTOR * limit:
        when = (f"it will stop by itself within {int(remaining // 60) + 1} min" if remaining > 60
                else "it will stop by itself any moment now")
        view["note"] = f"This is taking much longer than usual. If it's stuck, {when}. You can carry on in the chat meanwhile."
        view["slow"] = "very"
    else:
        view["note"] = _slow_note(key, work, thermal, fea)
        view["slow"] = "yes"


def _failed(view: dict, stored: dict, key: str, step: dict, suffix: str, fea: bool = False) -> dict:
    timed_out = bool(stored.get("timed_out"))
    can_retry = bool(stored.get("retry"))
    step["state"] = "failed"
    if key == QUEUED:
        message = "No worker picked it up in time." if timed_out else "Couldn't start the check."
    else:
        doing = (_FEA_DOING.get(key) if fea else None) or _DOING.get(key, "the check")
        message = f"{doing[:1].upper()}{doing[1:]} took too long." if timed_out else f"Couldn't finish {doing}."
    view.update(state="failed", current=message,
                reason="" if timed_out else _plain_reason(str(stored.get("reason") or "")),
                advice=_advice(key, suffix, timed_out, can_retry, fea), can_retry=can_retry)
    return view


__all__ = ["MEASURING", "NAMING", "OPENINGS", "PICTURES", "QUEUED", "READING", "SCOUT_STEPS", "Tracker",
           "duration_words", "is_fea", "mark", "note_work", "progress_key", "progress_view", "read_progress",
           "skin_triangles", "soft_limit_s", "track", "untrack"]
