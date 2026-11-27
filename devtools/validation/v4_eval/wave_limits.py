# Responsibility: The launch-time rules the ledger cannot enforce for itself: how many checkpoint waves and
#                 held-out waves may exist, that every shard of a wave has a unique index, and the per-shard cap.
# Owns: the wave registry (markers `slot/...` in the wave bucket (the SAME keys evalspec.claim_wave_slot takes and the report reads), created with create-if-absent) and the shard
#       index check.
# Boundaries: no network of its own. It talks to a `MarkerStore` (model_ledger) handed to it, so tests use a
#             directory and production uses the wave bucket. The caps are READ from model_ledger.WAVE_CAPS_USD.
"""Wave-count limits and shard-index arithmetic for the v4 launcher.

THE LIMITS. The plan allows at most 3 checkpoint waves and exactly one held-out wave. A wave is identified by its
`wave_id`; its first launch claims a slot with an atomic create-if-absent of `slot/held_out/1` or
`slot/checkpoint/<1..3>` (value: the wave id). A relaunch of the SAME wave id (a resume) finds its own slot and
passes; a new id with no free slot is REFUSED. Two launchers racing for the last slot cannot both win, because the
claim is the marker store's atomic create. Normal waves are not limited.

That atomicity is only as good as the bucket's conditional create, which is why `marker_probe` exists and why the
launcher refuses to start unless the probe has passed against the same bucket.

A slot is claimed at launch and not returned if the wave then fails to start; relaunching with the same id reuses
it. A wave that is abandoned for good keeps its slot (releasing one is a deliberate human edit of the marker).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CHECKPOINT_LIMIT = 3
WAVE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_UNIT_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+:(\d+)/(\d+)$")


class WaveRefused(RuntimeError):
    """The wave may not start: a limit, the probe gate, or the shard arithmetic says no."""


def _ledger() -> Any:
    name = "v4_model_ledger_table"
    if name in sys.modules:
        return sys.modules[name]
    # model_ledger imports only the standard library, so it is loaded by path: the launcher host needs no install
    path = REPO / "src" / "meshpipeline" / "cad" / "repair_v4" / "model_ledger.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


WAVE_CAPS_USD: dict[str, float] = _ledger().WAVE_CAPS_USD
# ONE slot ledger. These were `_waves/held_out` and `_waves/checkpoint-N` while evalspec.plan_wave claimed
# `slot/held_out/1` / `slot/checkpoint/N` for the same waves: two sources of truth for "has this slot been spent", so
# the launcher's count and the report's slot could disagree and a held-out split could be spent twice. They are one key
# space now; `check_wave_count` and `plan_wave` take the same marker, and a wave_id that holds it holds it for both.
HELD_OUT_KEY = "slot/held_out/1"


def checkpoint_key(n: int) -> str:
    return f"slot/checkpoint/{n}"


def cap_per_shard(wave_type: str, n_shards: int) -> float:
    """The slice each shard gets: the table's cap for the wave type over N (the arithmetic for_shard uses)."""
    if wave_type not in WAVE_CAPS_USD:
        raise WaveRefused(f"unknown wave type {wave_type!r}; the wave types are {sorted(WAVE_CAPS_USD)}")
    return WAVE_CAPS_USD[wave_type] / n_shards


def shard_assignments(units: list[str], n_shards: int) -> list[int]:
    """The 0-based ledger shard index of every unit (`<arm>:<kind>:I/N` -> I-1), refusing a unit that repeats, one
    whose I is outside 1..N, or one whose N is not the wave's N. Unique indexes are what makes the per-shard slices
    add up to the wave cap, so a plan that cannot guarantee them does not launch."""
    seen: set[int] = set()
    out: list[int] = []
    for unit in units:
        m = _UNIT_RE.match(unit)
        if not m:
            raise WaveRefused(f"unit {unit!r} is not <arm>:<kind>:I/N")
        i, n = int(m.group(1)), int(m.group(2))
        if n != n_shards:
            raise WaveRefused(f"unit {unit} was split for {n} shards but --shards-total is {n_shards}: every "
                              "shard of a wave must share one N")
        if not 1 <= i <= n:
            raise WaveRefused(f"unit {unit} has an index outside 1..{n}")
        if i - 1 in seen:
            raise WaveRefused(f"shard index {i - 1} appears twice in the plan ({unit}): two shards would share "
                              "one ledger slice")
        seen.add(i - 1)
        out.append(i - 1)
    return out


def check_wave_count(markers: Any, wave_type: str, wave_id: str, *, claim: bool) -> str | None:
    """None if this wave may start, else the reason it may not. With `claim`, a first launch takes its slot (and
    a lost race for the last slot is a refusal); without it nothing is written."""
    if wave_type not in WAVE_CAPS_USD:
        return f"unknown wave type {wave_type!r}; the wave types are {sorted(WAVE_CAPS_USD)}"
    if wave_type == "normal":
        return None
    if wave_type == "held_out":
        owner = markers.get(HELD_OUT_KEY)
        if owner is None:
            if not claim:
                return None
            if markers.create(HELD_OUT_KEY, wave_id):
                return None
            owner = markers.get(HELD_OUT_KEY)
        if owner == wave_id:
            return None
        return (f"the held-out wave was already launched as {owner!r}: the plan has exactly one held-out "
                f"wave and {wave_id!r} would be a second")
    if any(markers.get(checkpoint_key(n)) == wave_id for n in range(1, CHECKPOINT_LIMIT + 1)):
        return None                                   # a resume of a wave that already holds a slot
    owners: list[str] = []
    for n in range(1, CHECKPOINT_LIMIT + 1):
        owner = markers.get(checkpoint_key(n))
        if owner is None:
            if not claim:
                return None
            if markers.create(checkpoint_key(n), wave_id):
                return None
            owner = markers.get(checkpoint_key(n))    # lost the race for this slot: whose is it now?
            if owner == wave_id:
                return None
        owners.append(str(owner))
    return (f"the plan allows at most {CHECKPOINT_LIMIT} checkpoint waves and they are already "
            f"{', '.join(owners)}: {wave_id!r} would be a fourth")
