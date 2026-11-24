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
    #: per-body kept fractions, for the row and the trace
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
    try:
        got = pool.call("v4_volume_loss", {"original": str(original_brep), "repaired": str(repaired_brep),
                                           "max_bodies": MAX_BODIES}, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - an unmeasured state is delivered as it was before this rule existed
        return _unmeasured(f"error: {type(exc).__name__}: {exc}")
    if not got.ok or got.value is None:
        return _unmeasured(f"{got.status}: {got.error}")
    return from_kernel(got.value, keep_fraction=keep_fraction)


def from_kernel(value: dict, *, keep_fraction: float = KEEP_FRACTION) -> LossReport:
    """A `v4_volume_loss` payload as a `LossReport`. The THRESHOLD IS APPLIED HERE, not in the worker: the
    kernel measures and this file decides, so the rule lives in one place."""
    if value.get("solidless"):
        return LossReport(measured=True, solidless=True, bodies_total=int(value.get("bodies_total") or 0),
                          volume_ratio=value.get("volume_ratio"), detail={"bodies": {}})
    bodies: dict[str, Any] = dict(value.get("bodies") or {})
    kept = {k: float(v) for k, v in bodies.items() if v is not None}
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
        detail={"bodies": bodies, "unmeasured_bodies": [k for k, v in bodies.items() if v is None]},
    )


def _body_index(name: str) -> tuple[int, str]:
    try:
        return int(name[1:]), name
    except ValueError:
        return 1 << 30, name


__all__ = ["CALL_TIMEOUT_S", "KEEP_FRACTION", "MAX_BODIES", "LossReport", "check_loss", "from_kernel"]
