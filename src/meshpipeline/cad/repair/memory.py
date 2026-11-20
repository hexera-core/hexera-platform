# Responsibility: WHAT A REPAIR RUN REMEMBERS - the in-run working notes one region leaves for the
#                 next, and the cross-run, fingerprint-keyed record of "this face, this op, worked
#                 or failed".
# Owns: `Note` and `RunNotes` (in-memory, per run), `Outcome`, the `OutcomeStore` protocol, the
#       `NullOutcomeStore` that is the default everywhere, the file-backed `JsonlOutcomeStore` the
#       gym injects, the privacy scrubber both go through, and the posture read.
# Boundaries: MEMORY IS ADVICE, NEVER AN INSTRUCTION. Nothing here applies an operation, bypasses
#             a cap, speaks to the reviewer or touches declaration accounting; a remembered
#             outcome can only inform a proposal, and a model reply about memory stays DATA - an
#             outcome word this module does not know becomes `unknown` rather than being obeyed.
#             It holds NO GEOMETRY: no shape, no bytes, no path, no file or customer name - only
#             fingerprints, operation names, codes, counts and measurements. It imports no kernel,
#             starts no process, opens no socket and knows no database. It never raises on a
#             caller's behalf: a malformed line is counted and skipped, a non-finite measurement
#             becomes None.
# Collaborates with: cad/repair/identity.py (the content-derived fingerprints that make a note
#                    mean anything across runs), cad/repair/tournament.py (`plan_key`, and the
#                    tie-break `op_demerits` offers), cad/repair/investigator.py (the `notes` and
#                    `history` data turns), devtools/validation/repair_corpus.py (the ONLY place
#                    that may build a `JsonlOutcomeStore`).
from __future__ import annotations

import json
import logging
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import meshpipeline.settings.cad_repair as rcfg

logger = logging.getLogger(__name__)

# WHY MEMORY IS SAFE TO HAVE AT ALL.
#
# The dangerous version of "the repair loop remembers" is a cache of decisions: last time this
# shape of defect came up we committed op X, so commit it again. That version bypasses the caps,
# the reviewer and the declaration accounting, because the thing it remembers is an ANSWER.
#
# This module remembers EVIDENCE instead: a fingerprint, an op, and what measurably happened. An
# author may read it and propose differently; nothing downstream of the author changes. Every
# package a remembered outcome inspires still goes through `validate_package`, the caps, the
# reviewer and the accounting exactly as a package nobody remembered anything about does. So the
# worst a poisoned record can do is waste a proposal.

#: The on-value of the memory posture. Anything else - unset, "on", "true", "Live" - reads off,
#: the same single spelling the other v3 postures use. Flag off means byte-identical behaviour:
#: `notes_turn` and `history_turn` both return `{}`, so no key is added to any conversation.
_ON = "live"

#: At most this many notes may ride in one investigator turn. Eight is a turn a model can hold in
#: mind beside the observation; more is a wall of history that crowds out the measurement.
NOTES_PER_TURN = 8
#: At most this many exact cross-run hits ride in one turn, for the same reason.
EXACT_PER_TURN = 12
#: How many notes the report keeps. The report is read by a human and logged, not reasoned over.
NOTES_IN_REPORT_MAX = 64
#: The most characters a code-shaped field keeps. A code is a word, never a sentence.
CODE_MAX = 64
#: The most numbers one note's `measured` keeps.
MEASURED_MAX = 12

#: The outcome words a note may carry. `rejected:<field>` is the reviewer's objection FIELD NAME
#: and never the reviewer's prose - the reviewer stays sealed, here as everywhere.
KEPT = "kept"
COMMITTED = "committed"
ROLLED_BACK = "rolled_back"
REVERTED = "reverted"
DECLINED = "declined"
REJECTED = "rejected"
UNKNOWN = "unknown"

#: What counts as this op having worked, and what counts as it having failed. A word outside both
#: sets is `unknown` and earns NOTHING - neither credit nor a demerit.
GOOD_OUTCOMES = frozenset({KEPT, COMMITTED})
BAD_OUTCOMES = frozenset({ROLLED_BACK, REVERTED, DECLINED})

#: Keys a record may never carry out of the process, whatever nests them. Named rather than
#: sniffed: a key called `brep` holding a str is a geometry leak just as much as one holding
#: bytes. This is the same list `devtools/validation/repair_corpus._GEOMETRY_KEYS` uses, kept here
#: because `cad/` cannot import `devtools/`, widened with the keys that leak a NAME rather than a
#: shape - a path or a filename identifies a customer as surely as their part does.
_BANNED_KEYS = frozenset({
    "shape", "shapes", "brep", "brep_bytes", "step", "step_bytes", "stl", "stl_bytes",
    "geometry", "original_shape", "region_shape", "scratch_shape", "tshape", "mesh", "points",
    "vertices", "triangles", "box", "bbox", "centroid",
    "path", "paths", "file", "files", "filename", "file_name", "filepath", "file_path",
    "name", "part_name", "source", "uri", "url", "job", "customer", "user", "email", "tenant",
})

#: What a code-shaped field may be made of. Deliberately excludes `/`, `\`, `.` and whitespace, so
#: a path, a filename and a sentence cannot survive as one: `sew_faces` and `rejected:deviation_mm`
#: do, `/Users/someone/parts/secret.step` does not.
_CODE_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_:-")


def live() -> bool:
    """Whether the memory posture is on. FAILS CLOSED: only the exact `live` is on.

    Read defensively because the settings row (`CAD_REPAIR_MEMORY` / `cad-repair-memory`) is
    another lane's to plumb. With no attribute present this is False and behaviour is today's.
    """
    return str(getattr(rcfg, "CAD_REPAIR_MEMORY", "off") or "off") == _ON


# --------------------------------------------------------------------------- the privacy gate

def scrub(value: object) -> object:
    """`value` with geometry, names and raw bytes removed, at ANY nesting depth.

    Three rules, applied everywhere: a banned key is dropped whatever it holds, raw bytes become
    None whatever key holds them, and a non-finite float becomes None rather than raising or
    becoming a string - a `seconds` that came back as nan is a missing measurement, and None is
    how the rest of this module says missing. Anything else exotic is stringified, so the result
    always survives `json.dumps(..., allow_nan=False)`.
    """
    if isinstance(value, Mapping):
        return {str(k): scrub(v) for k, v in value.items()
                if str(k).strip().lower() not in _BANNED_KEYS}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None  # raw bytes are the geometry leak this guard exists for
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def code(value: object, *, limit: int = CODE_MAX) -> str:
    """`value` as a code-shaped word: lowercased, clipped, and stripped to `_CODE_OK`.

    This is the second half of the privacy rule. The first half drops banned KEYS; this one makes
    sure a banned VALUE cannot ride in a field that is allowed. A CODE IS ONE WORD, so only the
    first whitespace-delimited token survives - that is what stops a sentence of the reviewer's
    prose, or a path with a space in it, from arriving compacted into one long "code". Then every
    character outside `_CODE_OK` goes, which is what stops a path: `sew_faces` and
    `rejected:deviation_mm` survive, `/Users/someone/parts/secret.step` does not.
    """
    text = str(value if value is not None else "").strip().lower()
    token = text.split()[0] if text.split() else ""
    return "".join(ch for ch in token[:limit] if ch in _CODE_OK)


def face(value: object) -> str:
    """One entity fingerprint, code-shaped. `identity.fingerprint_of` already returns hex-ish
    text; this only refuses anything that is not."""
    return code(value, limit=CODE_MAX)


def measured_of(mapping: Mapping | None) -> dict:
    """`mapping` as NUMBERS ONLY, scrubbed, capped and ordered.

    A bool is not a measurement and a string is not a measurement: `measured` is the part of a
    note a later run may compare arithmetically, so anything that is not a finite-or-missing
    number is dropped rather than coerced. A non-finite number is KEPT AS None, because "we
    measured this and it came back nan" is a different fact from "we never measured it".
    """
    if not isinstance(mapping, Mapping):
        return {}
    out: dict = {}
    for raw_key, raw in mapping.items():
        key = code(raw_key)
        if not key or key in _BANNED_KEYS or len(out) >= MEASURED_MAX:
            continue
        if isinstance(raw, bool) or isinstance(raw, (bytes, bytearray, memoryview)):
            continue
        if isinstance(raw, int):
            out[key] = int(raw)
        elif isinstance(raw, float):
            out[key] = float(raw) if math.isfinite(raw) else None
        elif raw is None:
            out[key] = None
    return out


def _outcome_word(value: object) -> str:
    """One outcome word, or `unknown`.

    THE ADVICE BOUNDARY. A model may write anything here; what this module stores is one of the
    six words it knows, or `unknown`. An `unknown` is recorded faithfully and counts for nothing,
    so a reply that says `definitely_commit_this` cannot become an instruction by being remembered.
    """
    text = code(value)
    if text in GOOD_OUTCOMES or text in BAD_OUTCOMES:
        return text
    if text.startswith(REJECTED + ":"):
        field = text.split(":", 1)[1]
        return f"{REJECTED}:{field}" if field else UNKNOWN
    return UNKNOWN


# --------------------------------------------------------------------------- in-run notes

@dataclass(frozen=True, slots=True)
class Note:
    """One thing a region learned, as the next region may read it.

    `measured` holds numbers only and `outcome` is one of this module's words. It never holds
    geometry and never holds the reviewer's text: a rejection is remembered as the FIELD the
    reviewer objected to, which is what a later proposal can act on, and not as the sentence.

    Build one through `note()`, which scrubs. `to_dict` scrubs AGAIN, so a hand-constructed Note
    cannot leak either - the gate is on the way out, not only on the way in.
    """

    region_index: int
    fingerprints: tuple[str, ...]
    op: str
    defect_code: str
    outcome: str
    measured: dict

    def to_dict(self) -> dict:
        return {"region_index": int(self.region_index),
                "fingerprints": [face(f) for f in self.fingerprints],
                "op": code(self.op),
                "defect_code": code(self.defect_code),
                "outcome": _outcome_word(self.outcome),
                "measured": measured_of(self.measured)}

    @property
    def failed(self) -> bool:
        """Whether this note says the op did not stick. An `unknown` is NOT a failure."""
        word = self.outcome
        return word in BAD_OUTCOMES or word.startswith(REJECTED + ":")


def note(*, region_index: int = 0, fingerprints: Iterable[str] = (), op: str = "",
         defect_code: str = "", outcome: str = "", measured: Mapping | None = None) -> Note:
    """A scrubbed `Note`. This is the only constructor a caller should use."""
    try:
        index = int(region_index)
    except (TypeError, ValueError):
        index = -1
    return Note(region_index=index,
                fingerprints=tuple(f for f in (face(x) for x in fingerprints) if f),
                op=code(op), defect_code=code(defect_code), outcome=_outcome_word(outcome),
                measured=measured_of(measured))


class RunNotes:
    """The working notes of ONE repair run, in memory, owned by the author factory.

    Not a cache and not a store: it is born with the run and dies with it, it holds no geometry and
    no reviewer prose, and it answers one question - what has this run already tried on something
    like this. `for_region` is most-recent-first, because the latest evidence about an op is the
    evidence worth acting on.
    """

    __slots__ = ("_notes",)

    def __init__(self) -> None:
        self._notes: list[Note] = []

    def __len__(self) -> int:
        return len(self._notes)

    def add(self, item: Note) -> Note:
        """Record `item`, rescrubbed. Returns what was actually stored, never what was passed."""
        clean = note(region_index=item.region_index, fingerprints=item.fingerprints,
                     op=item.op, defect_code=item.defect_code, outcome=item.outcome,
                     measured=item.measured)
        self._notes.append(clean)
        return clean

    def for_region(self, fingerprints: Iterable[str],
                   defect_codes: Iterable[str] = ()) -> tuple[Note, ...]:
        """At most `NOTES_PER_TURN` notes relevant to this region, MOST RECENT FIRST.

        Relevant means it names one of these fingerprints OR one of these defect codes. The `or`
        is the point of pillar 5: region two must be able to learn that `sew_faces` already came
        apart on an `open_shell` THIS RUN, even though it is looking at different faces.
        """
        wanted = {f for f in (face(x) for x in fingerprints) if f}
        codes = {c for c in (code(x) for x in defect_codes) if c}
        hits = [n for n in reversed(self._notes)
                if (wanted & set(n.fingerprints)) or (n.defect_code and n.defect_code in codes)]
        return tuple(hits[:NOTES_PER_TURN])

    def all(self) -> tuple[Note, ...]:
        """Every note, oldest first. For the report and for `outcomes_of`, not for a turn."""
        return tuple(self._notes)

    def to_dict(self) -> dict:
        """The report's `notes`, clipped. `total` is the honest count before the clip."""
        return {"total": len(self._notes),
                "notes": [n.to_dict() for n in self._notes[:NOTES_IN_REPORT_MAX]]}


def notes_turn(notes: RunNotes | None, fingerprints: Iterable[str],
               defect_codes: Iterable[str] = ()) -> dict:
    """`{"notes": [...]}` for the investigator's first turn on a region, or `{}`.

    `{}` when the posture is off, when there is no notes object, or when nothing is remembered -
    three different reasons, one answer, because an EMPTY key would tell the model "the run has
    tried nothing", which is a claim this module should not make for free.
    """
    if notes is None or not live():
        return {}
    hits = notes.for_region(fingerprints, defect_codes)
    return {"notes": [n.to_dict() for n in hits]} if hits else {}


def op_demerits(notes: RunNotes | None, ops: Iterable[str],
                defect_codes: Iterable[str] = ()) -> int:
    """How many of `ops` this run has already SEEN FAIL on one of `defect_codes`.

    A plain non-negative count, for a caller to negate and append to the END of its lexicographic
    key. That position is the whole safety argument: a demerit can only separate candidates that
    are otherwise exactly equal, so memory reorders ties and nothing else. It is also posture-
    independent on purpose - the caller decides whether to ask, so a test of candidate order
    cannot be confused by a stale flag.

    An empty `defect_codes` means "any code", which is what a caller with an unlocated code has.
    """
    if notes is None:
        return 0
    wanted = {c for c in (code(x) for x in ops) if c}
    codes = {c for c in (code(x) for x in defect_codes) if c}
    return sum(1 for n in notes.all()
               if n.failed and n.op in wanted and (not codes or n.defect_code in codes))


# --------------------------------------------------------------------------- cross-run outcomes

@dataclass(frozen=True, slots=True)
class Outcome:
    """One remembered fact: on THIS part, THIS face, THIS op worked or failed.

    `part_sha256` is the caller's, and `fingerprint` is content-derived (`identity`), so the same
    part uploaded twice keys the same way and `exact` hits. Across DIFFERENT parts the fingerprint
    means nothing on its own - that is what `family` is for, which asks the weaker and more useful
    question: across every part, how did this op fare on this defect code at this stage.

    As with `Note`, `to_dict` is the privacy gate and scrubs whatever the dataclass was handed.
    """

    part_sha256: str
    fingerprint: str
    defect_code: str
    mesh_kind: str
    stage: str
    op: str
    outcome: str
    measured: dict

    def to_dict(self) -> dict:
        return {"part_sha256": code(self.part_sha256, limit=64),
                "fingerprint": face(self.fingerprint),
                "defect_code": code(self.defect_code),
                "mesh_kind": code(self.mesh_kind),
                "stage": code(self.stage),
                "op": code(self.op),
                "outcome": _outcome_word(self.outcome),
                "measured": measured_of(self.measured)}


def outcome(*, part_sha256: str = "", fingerprint: str = "", defect_code: str = "",
            mesh_kind: str = "", stage: str = "", op: str = "", outcome: str = "",
            measured: Mapping | None = None) -> Outcome:
    """A scrubbed `Outcome`. The only constructor a caller should use."""
    return Outcome(**_from_dict_fields(Outcome(
        part_sha256=part_sha256, fingerprint=fingerprint, defect_code=defect_code,
        mesh_kind=mesh_kind, stage=stage, op=op, outcome=outcome,
        measured=dict(measured or {})).to_dict()))


def _from_dict_fields(row: Mapping) -> dict:
    """The `Outcome` kwargs a scrubbed dict carries, with every field present."""
    return {"part_sha256": str(row.get("part_sha256") or ""),
            "fingerprint": str(row.get("fingerprint") or ""),
            "defect_code": str(row.get("defect_code") or ""),
            "mesh_kind": str(row.get("mesh_kind") or ""),
            "stage": str(row.get("stage") or ""),
            "op": str(row.get("op") or ""),
            "outcome": str(row.get("outcome") or ""),
            "measured": dict(row.get("measured") or {})}


def outcomes_of(notes: RunNotes | None, *, part_sha256: str, mesh_kind: str = "",
                stage: str = "") -> tuple[Outcome, ...]:
    """This run's notes as cross-run `Outcome`s, one per (note, fingerprint).

    A note names the faces it touched; an outcome names ONE face, because that is the key a later
    run looks up. `part_sha256` is the caller's - this module hashes nothing and reads no file.
    """
    if notes is None:
        return ()
    out: list[Outcome] = []
    for n in notes.all():
        for fp in n.fingerprints:
            out.append(outcome(part_sha256=part_sha256, fingerprint=fp,
                               defect_code=n.defect_code, mesh_kind=mesh_kind, stage=stage,
                               op=n.op, outcome=n.outcome, measured=n.measured))
    return tuple(out)


@runtime_checkable
class OutcomeStore(Protocol):
    """What a reader of cross-run memory may ask. TWO QUESTIONS, BOTH READ-ONLY.

    Note what is NOT here: no `write`. A reader cannot write, so a store injected for reading
    cannot grow a write path by accident, and `NullOutcomeStore` satisfies the protocol without
    owning a file at all.
    """

    def exact(self, part_sha256: str, fingerprint: str, op: str) -> tuple[Outcome, ...]:
        """Every remembered outcome of `op` on this exact face of this exact part."""
        ...

    def family(self, defect_code: str, mesh_kind: str, stage: str, op: str) -> dict:
        """`{outcome word: count}` across every part, for this defect code / kind / stage / op."""
        ...


class NullOutcomeStore:
    """THE DEFAULT, EVERYWHERE, AND WHAT PRODUCTION GETS.

    This is the null-by-construction half of pillar 5. Production does not get a store that
    returns empty results - it gets a store that has no file, no path and no write method, so
    "production reads no cross-run memory" is a property of the object graph and not of a flag. A
    `JsonlOutcomeStore` is built in `devtools/` only, and a test asserts that statically.
    """

    __slots__ = ()

    def exact(self, part_sha256: str, fingerprint: str, op: str) -> tuple[Outcome, ...]:
        return ()

    def family(self, defect_code: str, mesh_kind: str, stage: str, op: str) -> dict:
        return {}


def default_store() -> OutcomeStore:
    """The store a caller gets when nobody injected one. Always the null one."""
    return NullOutcomeStore()


class JsonlOutcomeStore:
    """A file-backed store: one JSON object per line, no geometry, loaded once.

    READ-ONLY AFTER LOAD, on purpose. `write` appends to the file; it does not feed this
    instance's reads, so a run cannot read its own half-written memory back and mistake it for
    history. The gym loads a fresh store per pass, which is exactly the generation boundary it
    wants.

    A malformed line is SKIPPED AND COUNTED (`malformed`). A memory file is machine-written data
    that a human may have edited, a disk may have truncated and a previous version may have
    written differently; one bad line must not take a corpus pass down.
    """

    __slots__ = ("path", "malformed", "_rows")

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.malformed = 0
        self._rows: list[Outcome] = []
        self._load()

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return  # a missing or unreadable file is an empty memory, never an error
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                self.malformed += 1
                continue
            if not isinstance(row, dict) or not row.get("op") or not isinstance(row["op"], str):
                self.malformed += 1
                continue
            self._rows.append(Outcome(**_from_dict_fields(scrub(row))))  # type: ignore[arg-type]

    def exact(self, part_sha256: str, fingerprint: str = "", op: str = "") -> tuple[Outcome, ...]:
        part, key, want = code(part_sha256, limit=64), face(fingerprint), code(op)
        return tuple(r for r in self._rows
                     if r.part_sha256 == part and r.fingerprint == key and r.op == want)

    def family(self, defect_code: str, mesh_kind: str = "", stage: str = "",
               op: str = "") -> dict:
        key = (code(defect_code), code(mesh_kind), code(stage), code(op))
        counts = Counter(r.outcome for r in self._rows
                         if (r.defect_code, r.mesh_kind, r.stage, r.op) == key)
        return dict(sorted(counts.items()))

    def write(self, outcomes: Iterable[Outcome]) -> int:
        """APPEND `outcomes` as scrubbed JSON lines; return how many were written.

        It never raises: a memory file that cannot be written is a lost generation of advice, not
        a failed repair, and the caller is a harness whose real job is the part.
        """
        rows = [o.to_dict() for o in outcomes]
        if not rows:
            return 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("could not write %d repair outcomes to %s: %s", len(rows),
                           self.path, exc)
            return 0
        return len(rows)


def history_turn(store: OutcomeStore | None, *, part_sha256: str,
                 fingerprints: Iterable[str], defect_code: str, mesh_kind: str = "",
                 stage: str = "", ops: Iterable[str] = ()) -> dict:
    """`{"history": {"exact": [...], "family": {op: counts}}}`, or `{}`.

    `{}` - no key at all - when the posture is off, when there is no store, or WHEN THE STORE IS
    THE NULL ONE. The last is the production path and it is checked by type, not by result: a
    production run does not ask the question, so there is nothing to answer.
    """
    if store is None or isinstance(store, NullOutcomeStore) or not live():
        return {}
    wanted = tuple(c for c in (code(x) for x in ops) if c)
    exact: list[dict] = []
    for fp in (face(x) for x in fingerprints):
        if not fp:
            continue
        for op_name in wanted or ("",):
            for row in store.exact(part_sha256, fp, op_name):
                exact.append(row.to_dict())
    family = {op_name: counts for op_name in wanted
              if (counts := store.family(defect_code, mesh_kind, stage, op_name))}
    if not exact and not family:
        return {}
    return {"history": {"exact": exact[:EXACT_PER_TURN], "family": family}}
