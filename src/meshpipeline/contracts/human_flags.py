# Responsibility: Own the identity and disposition vocabulary of a human's flagged mesh regions.
# Owns: the stable flag ordinal, the per-phase status vocabularies, and the validators both agents are held to.
# Boundaries: `user_dispute` remains the ONE store of the flags themselves; this types what may be said ABOUT them.
# Collaborates with: agents/reviewer/unified.py, agents/builder/tools/meshing.py and engines/quality_criteria.py.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: WHICH ARTIFACT IS UNDER REVIEW. A dispute run reviews twice: first the mesh the user is
#: complaining about, then the rebuild. The two ask different questions, and conflating them is how
#: a "the wing root is fine" about the OLD mesh could be read as proof the NEW one was fixed.
PHASE_PARENT = "parent_mesh"
PHASE_REBUILT = "rebuilt_mesh"

#: Parent phase: is the human's concern actually present on the delivered mesh?
PARENT_STATUSES = ("confirmed", "not_confirmed", "unassessable")

#: Rebuilt phase: did the rebuild answer it? `not_reproduced` is the honest answer when the concern
#: is simply not present on the new mesh - it is NOT a synonym for "I did not look", which is what
#: `unassessable` says. Both are only acceptable when grounded in an inspection of THIS mesh.
REBUILT_STATUSES = ("resolved", "not_reproduced", "unresolved", "unassessable")

#: A PASS is impossible while any flag carries one of these. `unassessable` blocks too: a region the
#: reviewer could not judge is not a region it cleared.
BLOCKING_REBUILT_STATUSES = frozenset({"unresolved", "unassessable"})


class FlagContractError(ValueError):
    pass


@dataclass(frozen=True)
class FlagFinding:

    ordinal: int
    status: str
    observation: str
    explanation: str
    measurements: str = ""

    def as_dict(self) -> dict:
        return {"ordinal": self.ordinal, "status": self.status, "observation": self.observation,
                "explanation": self.explanation, "measurements": self.measurements}


@dataclass(frozen=True)
class FlagResponse:

    ordinal: int
    intended_correction: str
    change_made: str
    affected_region: str = ""
    believed_addressed: bool = False

    def as_dict(self) -> dict:
        return {"ordinal": self.ordinal, "intended_correction": self.intended_correction,
                "change_made": self.change_made, "affected_region": self.affected_region,
                "believed_addressed": self.believed_addressed}


def flags_of(user_dispute: Mapping | None) -> list[Mapping]:
    raw = (user_dispute or {}).get("flags") or []
    return [f for f in raw if isinstance(f, Mapping)]


def expected_ordinals(user_dispute: Mapping | None) -> tuple[int, ...]:
    # THE STABLE IDENTITY, and deliberately not a new database id. The route already preserves the
    # user's flag list in order inside `user_dispute`, that list is carried verbatim through the
    # dispatch payload and the checkpointed state, and nothing appends to it - so its 1-based index
    # is an identifier every stage can derive without storing a second one.
    return tuple(range(1, len(flags_of(user_dispute)) + 1))


def describe_flag(index: int, flag: Mapping) -> str:
    # The human's own words are DATA. They are rendered inside a delimited block by the callers;
    # here they are only trimmed, never interpreted.
    bits = [f"flag {index}"]
    try:
        bits.append(f"at (x={float(flag['x']):.6g}, y={float(flag['y']):.6g}, "
                    f"z={float(flag['z']):.6g})")
    except (KeyError, TypeError, ValueError):
        bits.append("at (coordinates unavailable)")
    span = flag.get("span")
    if span:
        try:
            bits.append(f"span {float(span):.4g}")
        except (TypeError, ValueError):
            pass
    patch = str(flag.get("patch") or "").strip()
    if patch:
        bits.append(f"patch {patch[:128]}")
    note = str(flag.get("note") or "").strip()
    if note:
        bits.append(f"the user's concern: {note[:500]}")
    return ", ".join(bits)


def _statuses_for(phase: str) -> tuple[str, ...]:
    if phase == PHASE_PARENT:
        return PARENT_STATUSES
    if phase == PHASE_REBUILT:
        return REBUILT_STATUSES
    raise FlagContractError(f"unknown dispute review phase {phase!r}")


def _parse_rows(raw, phase: str, expected: tuple[int, ...],
                fields: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return [], [f"flag results must be a list, got {type(raw).__name__}"]

    rows: list[dict] = []
    seen: dict[int, int] = {}
    for i, row in enumerate(raw):
        where = f"flag result[{i}]"
        if not isinstance(row, Mapping):
            problems.append(f"{where} must be an object, got {type(row).__name__}")
            continue
        unknown = sorted(set(row) - set(fields))
        if unknown:
            problems.append(f"{where} has unknown field(s) {', '.join(unknown)}")
        missing = sorted(set(fields) - set(row))
        if missing:
            problems.append(f"{where} is missing required field(s) {', '.join(missing)}")
        if unknown or missing:
            continue
        ordinal = row["ordinal"]
        if type(ordinal) is not int or isinstance(ordinal, bool):
            problems.append(f"{where}.ordinal must be an integer, got {ordinal!r}")
            continue
        if ordinal not in expected:
            problems.append(
                f"{where}.ordinal {ordinal} is not one of the flags the user raised "
                f"({', '.join(map(str, expected)) or 'none'})")
            continue
        if ordinal in seen:
            problems.append(f"{where}.ordinal {ordinal} is a duplicate of flag result"
                            f"[{seen[ordinal]}] - each flag is judged exactly once")
            continue
        seen[ordinal] = i
        rows.append(dict(row))

    covered = set(seen)
    for missing_ordinal in sorted(set(expected) - covered):
        problems.append(f"flag {missing_ordinal} has no result - every flag the user raised must "
                        "be judged explicitly, and silence is not a pass")
    return rows, problems


def parse_flag_findings(raw, *, user_dispute: Mapping | None,
                        phase: str) -> tuple[tuple[FlagFinding, ...], tuple[str, ...]]:
    expected = expected_ordinals(user_dispute)
    statuses = _statuses_for(phase)
    fields = ("ordinal", "status", "observation", "explanation", "measurements")
    rows, problems = _parse_rows(raw, phase, expected, fields)

    out: list[FlagFinding] = []
    for row in rows:
        where = f"flag {row['ordinal']}"
        status = row["status"]
        if not isinstance(status, str) or status not in statuses:
            problems.append(f"{where}.status must be one of {', '.join(statuses)}, got {status!r}")
            continue
        obs = row["observation"]
        exp = row["explanation"]
        if not isinstance(obs, str) or not obs.strip():
            problems.append(f"{where}.observation must say what you actually saw at this region")
            continue
        if not isinstance(exp, str) or not exp.strip():
            problems.append(f"{where}.explanation must be a non-empty string")
            continue
        meas = row["measurements"] if isinstance(row["measurements"], str) else ""
        # A cleared flag has to be grounded. `not_reproduced` and `resolved` both assert something
        # about THIS mesh, so they need a measurement; `unresolved`/`unassessable` are already
        # blocking and need no extra proof to be believed.
        if phase == PHASE_REBUILT and status in ("resolved", "not_reproduced") and not meas.strip():
            problems.append(
                f"{where}.status={status} clears the user's concern, so it needs the measurements "
                "you took on the REBUILT mesh - an unmeasured clearance is not an inspection")
            continue
        out.append(FlagFinding(ordinal=row["ordinal"], status=status, observation=obs.strip(),
                               explanation=exp.strip(), measurements=meas.strip()))
    return tuple(sorted(out, key=lambda f: f.ordinal)), tuple(problems)


def parse_flag_responses(raw, *,
                         user_dispute: Mapping | None) -> tuple[tuple[FlagResponse, ...],
                                                                tuple[str, ...]]:
    expected = expected_ordinals(user_dispute)
    fields = ("ordinal", "intended_correction", "change_made", "affected_region",
              "believed_addressed")
    rows, problems = _parse_rows(raw, PHASE_REBUILT, expected, fields)

    out: list[FlagResponse] = []
    for row in rows:
        where = f"flag {row['ordinal']}"
        intent = row["intended_correction"]
        change = row["change_made"]
        if not isinstance(intent, str) or not intent.strip():
            problems.append(f"{where}.intended_correction must say what you set out to correct")
            continue
        if not isinstance(change, str) or not change.strip():
            problems.append(f"{where}.change_made must name the authoring or parameter change you "
                            "actually made - 'nothing' is a valid answer, silence is not")
            continue
        believed = row["believed_addressed"]
        if type(believed) is not bool:
            problems.append(f"{where}.believed_addressed must be the JSON boolean true or false")
            continue
        region = row["affected_region"] if isinstance(row["affected_region"], str) else ""
        out.append(FlagResponse(ordinal=row["ordinal"], intended_correction=intent.strip(),
                                change_made=change.strip(), affected_region=region.strip(),
                                believed_addressed=believed))
    return tuple(sorted(out, key=lambda r: r.ordinal)), tuple(problems)


def blocking_flag_reasons(findings: Sequence[FlagFinding], *, phase: str,
                          user_dispute: Mapping | None) -> tuple[str, ...]:
    # THE GATE. Called after parsing, so shape problems are already reported; this is only about
    # whether the DISPOSITION permits a PASS.
    if phase != PHASE_REBUILT:
        return ()
    expected = set(expected_ordinals(user_dispute))
    if not expected:
        return ()
    reasons: list[str] = []
    by_ordinal = {f.ordinal: f for f in findings}
    for ordinal in sorted(expected):
        f = by_ordinal.get(ordinal)
        if f is None:
            reasons.append(f"flag {ordinal} was never judged")
        elif f.status in BLOCKING_REBUILT_STATUSES:
            reasons.append(f"flag {ordinal} is {f.status}")
    return tuple(reasons)


def as_dicts(rows: Sequence) -> list[dict]:
    return [r.as_dict() if hasattr(r, "as_dict") else dict(r) for r in rows]


def findings_from_state(rows) -> tuple[FlagFinding, ...]:
    # State survives JSON: a checkpoint or a hosted reconstruction hands these back as plain dicts.
    out: list[FlagFinding] = []
    for r in rows or []:
        if isinstance(r, FlagFinding):
            out.append(r)
        elif isinstance(r, Mapping) and "ordinal" in r:
            out.append(FlagFinding(ordinal=int(r["ordinal"]), status=str(r.get("status", "")),
                                   observation=str(r.get("observation", "")),
                                   explanation=str(r.get("explanation", "")),
                                   measurements=str(r.get("measurements", ""))))
    return tuple(sorted(out, key=lambda f: f.ordinal))


def responses_from_state(rows) -> tuple[FlagResponse, ...]:
    out: list[FlagResponse] = []
    for r in rows or []:
        if isinstance(r, FlagResponse):
            out.append(r)
        elif isinstance(r, Mapping) and "ordinal" in r:
            out.append(FlagResponse(
                ordinal=int(r["ordinal"]),
                intended_correction=str(r.get("intended_correction", "")),
                change_made=str(r.get("change_made", "")),
                affected_region=str(r.get("affected_region", "")),
                believed_addressed=bool(r.get("believed_addressed", False))))
    return tuple(sorted(out, key=lambda r: r.ordinal))
