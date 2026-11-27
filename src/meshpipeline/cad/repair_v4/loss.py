# Responsibility: CATASTROPHIC LOSS - the one fidelity rule that REFUSES (owner ruling 2026-11-24, lane B22).
# Owns: LossReport, the thresholds, and `check_loss` (the parent side of the `v4_volume_loss` kernel call).
# Boundaries: no OCCT here; the measurement is the kernel call (loss_kernel). The delivery gate and the
#             search's ranking read `LossReport.catastrophic` and nothing else.
"""Did the repair still deliver the customer's part, or a fragment of it?

Everything else layer 3 measures (deviation, area, volume and bbox ratios, the 25%/10% gaming limits) is
REPORTED and gates nothing. This one refuses, because the states it describes are not repairs: v4's
`search_ai` delivered sha 3e7c2a1197c3 at 0.00069x the input's volume - 0.07% of the part, valid and
meshable, and worthless.

THE RULE, against the ORIGINAL INPUT and never against the parent state (a chain of individually mild
losses is the way a part disappears):

1. the part: the output may not keep under `KEEP_FRACTION` of the input's volume;
2. a body: no input body may have under `KEEP_FRACTION` of ITS OWN volume left.

ORIENTATION IS NOT GEOMETRY, and getting that wrong was this rule's one real hole (lane C20, B26): a solid
whose faces point inward encloses the same points, `GProp` calls its volume negative, and OCCT's `Common`
against it returns its COMPLEMENT - so everything OUTSIDE it counted as KEPT. On 0051_00510567 this rule read
`part_kept` 0.697 for a delivery holding 0.07% of the part, and refused the file only because four other bodies
happened to fall outside it; an inverted solid covering the rest would have made the rule DELIVER a destroyed
fragment. Every Boolean now runs on positively oriented solids, and an output solid that cannot be oriented
positive makes the whole report UNMEASURED rather than a measured complement.

A SELF-INTERSECTING INPUT BODY IS MEASURED BY TESSELLATION (owner decision A, 2026-11-25; the rule is unchanged,
only its measurement). OCCT's Common on a self-interfering argument returns ~0 (b25999e0f8be read "kept 0.0" on a
1e-5 volume change), so for a body layer 1's own rule calls self-intersecting the kernel samples interior points of
the body's triangulation and counts how many the output's triangulation still contains (`repair_v5.tess_kernel`).
The same measurement decides whenever Common CONTRADICTS the plain volumes - it calls a body lost while the part's
volume barely moved (step 7: a pure split of a valid body read "kept 0.85%" at a 5e-14 volume change). Being a real
measurement, it cannot let a real loss through: a deleted body's interior points are not in the output.

"KEPT" IS COMMON VOLUME, NOT A VOLUME RATIO. The measure is `volume(Common(input body, output))`, how much
of that body the output still occupies, for two reasons. A plain ratio counts volume the output has
anywhere, so a "repair" that deleted a wing and doubled a boss could pass; and the overlap repairs this
engine exists for legitimately REDUCE total volume, because two interpenetrating bodies count their shared
volume twice before the fuse and once after.

WHICH IS WHY A FUSE IS NOT A LOSS, and why the rule is not written on the body COUNT: fusing 8 touching
bodies into 4 leaves every input body's volume exactly where it was, so every body keeps ~1.0 and nothing
is refused. A body is lost when its volume is GONE, not when its identity was merged into a neighbour.

A solid-less input (loose faces, a shell) has no volume to keep and cannot trip this rule: a correct heal
that makes it a solid must never be refused for it (the same exemption `faithful_kernel` makes).

FAIL-OPEN, DELIBERATELY. Anything that stops the measurement - a failed Boolean, a timeout, a dead pool,
more bodies than the cap - leaves `measured` False and refuses NOTHING. A refusal is a claim about the
part, so it is made only on evidence; an unmeasured state is delivered exactly as it was before this rule
existed, and the row says the measurement did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The floor. An output keeping under this fraction of the input part's volume, or of any one input body's
#: volume, is not a repair of that part. 10% is the owner's ruling of 2026-11-24.
KEEP_FRACTION = 0.10
#: Input bodies whose kept fraction is measured, largest by volume first. A part with more bodies than this
#: is measured as far as the cap and reports `bodies_measured`; the part-level number still covers them all.
MAX_BODIES = 64
#: One measurement's wall. It is a bounded number of Booleans (one per body, plus the part), but on a big
#: assembly each is seconds, and a timeout must never cost the search its state - it only stops the claim.
CALL_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class LossReport:
    """What the output kept of the input. `measured` False means nothing here may refuse anything."""

    measured: bool
    #: common volume with the input, over the input's volume: 1.0 keeps the part, 0.0 keeps none of it.
    #: None when not measured.
    part_kept: float | None = None
    #: the plain volume ratio (output over input), REPORTED only - it is not what the rule reads. A fuse of
    #: overlapping bodies drops it below 1.0 without losing anything.
    volume_ratio: float | None = None
    #: input bodies (`S<k>`, 1-based solid indices of the input) keeping under `KEEP_FRACTION` of their own volume
    lost_bodies: tuple[str, ...] = ()
    #: the smallest kept fraction over the measured bodies (None when none were measured)
    kept_min: float | None = None
    bodies_total: int = 0
    bodies_measured: int = 0
    #: the input has no solids, so it has no volume and no body to keep: this rule does not apply to it
    solidless: bool = False
    #: why it was not measured, when it was not (`error: ...`, `timeout`, `too_many_bodies`)
    unmeasured_why: str | None = None
    #: per-body kept fractions, for the row and the trace, and what had to be re-oriented (`inverted`)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def catastrophic(self) -> bool:
        """True only on MEASURED evidence that rule 1 or rule 2 was broken."""
        if not self.measured or self.solidless:
            return False
        if self.part_kept is not None and self.part_kept < KEEP_FRACTION:
            return True
        return bool(self.lost_bodies)

    @property
    def reason(self) -> str | None:
        """Which rule refused: `part_volume_lost`, `body_lost`, or None."""
        if not self.catastrophic:
            return None
        if self.part_kept is not None and self.part_kept < KEEP_FRACTION:
            return "part_volume_lost"
        return "body_lost"

    def row(self) -> dict[str, Any]:
        """The fields a row carries, so `report.py` can count refusals and show what was kept."""
        return {"measured": self.measured, "catastrophic": self.catastrophic, "reason": self.reason,
                "part_kept": self.part_kept, "volume_ratio": self.volume_ratio,
                "lost_bodies": list(self.lost_bodies), "kept_min": self.kept_min,
                "bodies_total": self.bodies_total, "bodies_measured": self.bodies_measured,
                "solidless": self.solidless, "unmeasured_why": self.unmeasured_why}


UNMEASURED = LossReport(measured=False, unmeasured_why="not_measured")


def _unmeasured(why: str) -> LossReport:
    return LossReport(measured=False, unmeasured_why=why)


def check_loss(pool: Any, original_brep: str, repaired_brep: str, *,
               timeout_s: float = CALL_TIMEOUT_S, keep_fraction: float = KEEP_FRACTION) -> LossReport:
    """How much of the input BREP the output BREP still keeps (`v4_volume_loss`). NEVER raises: every failure
    comes back as an unmeasured report, which refuses nothing."""
    import time

    from meshpipeline.cad.repair_v5 import events as EV

    t0 = time.monotonic()
    report = _check_loss(pool, original_brep, repaired_brep, timeout_s=timeout_s, keep_fraction=keep_fraction)
    EV.emit("loss_check", severity="WARNING" if report.catastrophic else "INFO", original=str(original_brep),
            repaired=str(repaired_brep), keep_fraction=keep_fraction, **report.row(),
            inverted=report.detail.get("inverted"), seconds=round(time.monotonic() - t0, 3),
            screened=len(report.detail.get("screened") or ()), bodies=report.detail.get("bodies"))
    return report


def _check_loss(pool: Any, original_brep: str, repaired_brep: str, *, timeout_s: float,
                keep_fraction: float) -> LossReport:
    payload = {"original": str(original_brep), "repaired": str(repaired_brep), "max_bodies": MAX_BODIES,
               "keep_fraction": keep_fraction, "defer_over": INLINE_BODIES}
    try:
        got = pool.call("v4_volume_loss", payload, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - an unmeasured state is delivered as it was before this rule existed
        return _unmeasured(f"error: {type(exc).__name__}: {exc}")
    if not got.ok or got.value is None:
        return _unmeasured(f"{got.status}: {got.error}")
    value = got.value
    if value.get("deferred"):
        value = _measure_deferred(pool, payload, value, timeout_s=timeout_s)
    return from_kernel(value, keep_fraction=keep_fraction)


#: CRS v5 r2h: a candidate that changed at most this many input bodies is measured in the screening call itself;
#: more, and they are measured in up to `PARALLEL_CALLS` kernel calls at once (the pool queues what it cannot run).
#: Not 1: each extra call re-reads both BREPs and waits for a worker, and the tournament's parallel heats keep the pool
#: busy (r2h VM run, 5accc12fa47d: 2-3 changed bodies fanned out took 131-168 s in place; the same candidates measured
#: in one call, alone, with the input side cached, took 8-11 s)
INLINE_BODIES = 4
PARALLEL_CALLS = 3


def _measure_deferred(pool: Any, payload: dict, first: dict, *, timeout_s: float) -> dict:
    """`first` (the screening call: the unchanged bodies kept, the changed ones `deferred`) completed by measuring the
    deferred bodies in parallel calls, merged into one payload. A chunk that fails leaves ITS bodies unmeasured
    (None), exactly as a body whose Boolean failed inside one call always was."""
    from concurrent.futures import ThreadPoolExecutor

    names = list(first["deferred"])
    width = max(1, min(PARALLEL_CALLS, len(names)))
    chunks = [names[k::width] for k in range(width)]

    def one(chunk: list[str]) -> dict | None:
        try:
            got = pool.call("v4_volume_loss", {**payload, "bodies": chunk, "defer_over": None}, timeout_s=timeout_s)
        except Exception:  # noqa: BLE001 - this chunk's bodies are unmeasured; the rest still stand
            return None
        return got.value if got.ok and got.value is not None else None

    with ThreadPoolExecutor(max_workers=width) as ex:
        parts = list(ex.map(one, chunks))
    merged = {**first, "bodies": dict(first.get("bodies") or {}), "tessellated": dict(first.get("tessellated") or {}),
              "self_intersecting": list(first.get("self_intersecting") or []), "deferred": []}
    kept_sum, vol_sum = float(first.get("kept_sum") or 0.0), float(first.get("vol_sum") or 0.0)
    for chunk, part in zip(chunks, parts, strict=True):
        if part is None:
            merged["bodies"].update(dict.fromkeys(chunk))
            continue
        merged["bodies"].update(part.get("bodies") or {})
        merged["tessellated"].update(part.get("tessellated") or {})
        merged["self_intersecting"] += list(part.get("self_intersecting") or [])
        kept_sum += float(part.get("kept_sum") or 0.0)
        vol_sum += float(part.get("vol_sum") or 0.0)
        merged["seconds"] = max(float(merged.get("seconds") or 0.0), float(part.get("seconds") or 0.0))
    merged["kept_sum"], merged["vol_sum"] = kept_sum, vol_sum
    merged["part_kept"] = None if vol_sum <= 0.0 else kept_sum / vol_sum
    merged["self_intersecting"] = sorted(set(merged["self_intersecting"]), key=_body_index)
    return merged


def from_kernel(value: dict, *, keep_fraction: float = KEEP_FRACTION) -> LossReport:
    """A `v4_volume_loss` payload as a `LossReport`. The THRESHOLD IS APPLIED HERE, not in the worker: the
    kernel measures and this file decides, so the rule lives in one place."""
    if value.get("solidless"):
        return LossReport(measured=True, solidless=True, bodies_total=int(value.get("bodies_total") or 0),
                          volume_ratio=value.get("volume_ratio"), detail={"bodies": {}})
    bodies: dict[str, Any] = dict(value.get("bodies") or {})
    kept = {k: float(v) for k, v in bodies.items() if v is not None}
    if value.get("unmeasured_why") and value.get("part_kept") is None:
        # the kernel measured nothing it would stand behind (an output solid it could not orient positive):
        # the rule claims nothing, and layer 1 reports the negative solid on its own account
        return LossReport(measured=False, unmeasured_why=str(value["unmeasured_why"]),
                          volume_ratio=value.get("volume_ratio"), bodies_total=int(value.get("bodies_total") or 0),
                          detail={"inverted": value.get("inverted") or {}})
    if not kept and value.get("part_kept") is None:
        # a payload that measured nothing claims nothing: `measured` True with no number in it would be a report
        # that refuses nothing while LOOKING like evidence, and the next rule written on top of it would be wrong
        return _unmeasured("nothing_measured")
    lost = tuple(sorted((k for k, v in kept.items() if v < keep_fraction), key=_body_index))
    part_kept = value.get("part_kept")
    return LossReport(
        measured=True,
        part_kept=None if part_kept is None else float(part_kept),
        volume_ratio=value.get("volume_ratio"),
        lost_bodies=lost,
        kept_min=min(kept.values()) if kept else None,
        bodies_total=int(value.get("bodies_total") or 0),
        bodies_measured=len(kept),
        unmeasured_why=value.get("unmeasured_why"),
        detail={"bodies": bodies, "unmeasured_bodies": [k for k, v in bodies.items() if v is None],
                "inverted": value.get("inverted") or {},
                # v5 step 8: bodies layer 1 says self-intersect are measured by tessellation, not by Common
                "self_intersecting": list(value.get("self_intersecting") or []),
                "tessellated": dict(value.get("tessellated") or {}),
                # CRS v5 r2h: the bodies the exact screen kept unchanged, with no Boolean
                "screened": list(value.get("screened") or [])},
    )


def _body_index(name: str) -> tuple[int, str]:
    try:
        return int(name[1:]), name
    except ValueError:
        return 1 << 30, name


__all__ = ["CALL_TIMEOUT_S", "KEEP_FRACTION", "MAX_BODIES", "LossReport", "check_loss", "from_kernel"]
