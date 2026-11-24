#!/usr/bin/env python3
# Responsibility: Turn the collected rows of an ARM wave into the comparison the sprint is judged on:
#                 per arm the clean rate, the defect-kind breakdown, regressions, the inconclusive
#                 count (loud, separate), model spend and wall-clock; and the AI / deterministic marginals.
# Owns: `ArmRow`, `load_wave` (the only way to a `WaveResult`), `build_report`, `render_text`.
# Boundaries: no CAD, no model, no bucket. Rows come from the arm harness; every refusal collect.py makes
#             about a wave (a missing or failed shard, a mixed commit or image, a partition mismatch) is
#             made by collect's own code, called from here. The held-out report is the primary artifact
#             and the dev report is the SAME code path with a different split.
"""The arm-wave report.

    report.py --from <dir with shard-I-of-N/> --wave heldout --shards N \\
        --split devtools/validation/corpora/v4/heldout.json --frozen-commit <40-hex> [--json out.json]

THE ROW. One `ArmRow` per (file, arm): the INPUT's clean flag and panel passes, and the OUTPUT's `valid`,
`faithful_ok` and `panel_passes` of `panel_size`, judged at the FULL tier, plus model dollars and wall
seconds. Clean is DERIVED here (`valid AND every panel member passes`, the verdict's
definition); a row whose stored `clean` disagrees is refused, so a harness cannot hand over a verdict the
parts do not support. A row the checker could not judge (a CheckerUnavailable, a pool death, a missing
mesher) is `inconclusive`: it carries no verdict and counts as NEITHER a pass NOR a failure, anywhere.

INCONCLUSIVE IS LOUD. An arm with more than MAX_INCONCLUSIVE_FRACTION inconclusive files is NOT MEASURED:
its clean rate and gain are withheld (None) and the report says so in its first lines. 30% inconclusive
means the arm was not measured, whatever the surviving 70% scored.

WHAT CANNOT BE REPORTED. `build_report` takes only a `WaveResult`, and `WaveResult` is built only by
`load_wave`, which refuses: a partial wave, a failed shard, a mixed commit or image, a partition mismatch
(collect.py's gates), any source outside ALLOWED_SOURCES (Fusion 360 raises `DisallowedSource`), a
(file, arm) with no row or two, a model arm without a ledger, spend above the wave's cap, an input that
disagrees with the split's own verdict or between two arms, an `input_only` arm that changed its input,
the held-out split without the frozen commit, and the held-out split for a tuning purpose; a held-out or
checkpoint wave whose shards do not carry the `slot` plan_wave claimed (checked against the marker store when
one is given); a search row that says "conclusive" while its own reason / candidate counts say nothing was
measured; a per-file model spend over the arm's ceiling; an input_only row that changed any layer.

THE DECLINED STRATUM is a THIRD category, neither measured-and-failed nor unmeasured: files whose injected defect
no op may repair by design (`face_delete_large`: the deleted face's loop spans more than the fill cap, and the
faithful layer exists to stop a fill inventing most of a part). Their expected outcome is "correctly declined". A
split row declares it through its own `kind_status == "declined_stratum"` (the injector's label, carried by the pool
entry); `load_wave` refuses a row of a declined kind that does not carry that status. Those files are REMOVED from
every arm's files, conclusive and inconclusive counts, clean rate, clean gain, regressions, layers, per-kind rates,
the paired set and the marginals, and appear ONLY under `declined_stratum`, on their own line.

THE EXCLUDED STRATUM is the other third category, and for the opposite reason: a file the harness reached NO
verdict on, because the per-file wall or the checker's one-call wall killed it, or a worker died, or the input
could not be read. Those files are inconclusive for every arm, so they are already outside every clean rate,
gain and regression - but counting them only in `inconclusive` left them nameless. `excluded_stratum` counts
them, NAMES them, and prints the two wall values that produced them, on its own line, every run. It is built by
collect.py's `excluded_stratum`, the same code the re-probe's own report uses: one mechanism, two waves.

NOT MEASURED WITHHOLDS EVERYTHING a rate can be recovered from (counts, layers, regressions, per-kind rates),
and the JSON says `numbers_valid: false` whenever the status is UNRELIABLE. PER-ARM rates use each arm's own
dropouts and are NOT comparable across arms; the PAIRED columns (files every measured arm judged) are.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as _collect  # noqa: E402  (loads run_arm -> evalspec, reprobe, pool)

ev = _collect.evalspec
rp = _collect.rp

#: PROVISIONAL, owned by the owner (not a settled constant): above this share of an arm's files inconclusive,
#: the arm is NOT MEASURED and its rate is withheld. Deliberately strict: collect.py already refuses a pool
#: with 2% excluded. Under the limit the rate is STILL printed on every arm, always, so 0.5% and 9.5% differ.
MAX_INCONCLUSIVE_FRACTION = 0.10
#: Fewer conclusive files than this and the constant-outcome warning is not raised (too few to call). 10 is a
#: JUDGEMENT, not a calibrated constant: below it a coin-flip arm is constant by chance far too often (n=5:
#: 2 x 0.5^5 = 6%), at 10 that chance is ~0.2%, and a wave of dev/held-out size (100s of files) is far above it.
#: Anything from ~8 to ~20 would be defensible; do not read it as a threshold the data picked.
MIN_FILES_FOR_CONSTANT_WARNING = 10
#: PROVISIONAL, owner's to set: the paired set (files EVERY measured arm judged) must cover at least this share of
#: the split, else the report warns and says the numbers are UNRELIABLE. Four measured arms each dropping ~10% on
#: different files leave ~66% of the split, which is under this limit on purpose: that is the case it exists for.
MIN_PAIRED_FRACTION = 0.80
#: The arms whose rows come from a best-first SEARCH and so carry SearchResult's reason / candidate counts. A
#: conclusive row of one of these without them cannot be told from "never measured" and is refused.
SEARCH_ARMS = ("search_det", "search_ai")
#: What a false positive of the constant/identical detector looks like; printed with every such warning.
FALSE_POSITIVE_NOTE = ("this is ALSO what a real result looks like when two deterministic arms truly agree on every "
                       "shared file, or when an arm is perfect on the clean-control split: check that the harness "
                       "varies the arm before discarding the numbers")
SPEND_EPS_USD = 1e-6
#: The injector's status for a defect no op may repair by design (inject.KIND_STATUS); the one string the report
#: reads, and `load_wave` checks it against the injector's own declined kinds.
DECLINED_STATUS = "declined_stratum"
#: lane C14b: a file whose input verdict contradicts its split label is reported as LABEL DRIFT (its own stratum) up to
#: this share of the split; beyond it the wave is refused, because a checker that disagrees with most labels (e.g. one
#: that calls everything clean) is broken, not merely newer. v4's dev drifted on 4 of 150 (2.7%).
MAX_LABEL_DRIFT_FRACTION = 0.25
#: The cells of a search row's `cheap_full` table; the same names `arms.CHEAP_FULL_CELLS` writes and
#: `reprobe.agreement` reports (not imported from arms.py, which would pull the arm dispatch into the report).
CHEAP_FULL_CELLS = ("cheap_pass_full_pass", "cheap_pass_full_fail", "cheap_fail_full_pass", "cheap_fail_full_fail")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


class ReportRefused(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------- the row

@dataclass(frozen=True)
class ArmRow:
    name: str
    sha256: str
    source: str
    arm: str
    inconclusive: bool
    model_usd: float
    wall_s: float
    in_clean: bool | None = None
    in_panel_passes: int | None = None
    out_valid: bool | None = None
    out_faithful_ok: bool | None = None
    out_panel_passes: int | None = None
    panel_size: int | None = None
    out_tier: str | None = None
    out_clean: bool | None = None  # as the harness stored it; load refuses it if the parts disagree
    #: the input's own layer results; REQUIRED on input_only (so a changed out_valid / out_faithful_ok is caught)
    in_valid: bool | None = None
    in_faithful_ok: bool | None = None
    #: SearchResult's own words (Task 18b), carried so "never measured" cannot be written down as "measured and
    #: failed". `inconclusive` is the harness's file-level flag; these are what it is checked AGAINST.
    reason: str | None = None
    verdict_candidates: int | None = None
    inconclusive_candidates: int | None = None
    infrastructure_failed: bool | None = None
    #: the search stopped on the CLOCK and still delivered its best state's FULL verdict (B18's anytime rule, owner
    #: ruling 2026-11-24). The row COUNTS - `out_tier: "full"` is what makes it a measurement - and this flag is
    #: here so a reader can keep such rows out of a clean-rate denominator if they choose. Reported per arm.
    #: None on every row written before the ruling, where a time stop was discarded instead.
    stopped_on_wall: bool | None = None
    #: search arms only: cheap tier vs full panel over the states the search CONFIRMED (`arms.cheap_full_cells`),
    #: the four cells named as `reprobe.agreement` names them. Optional: a row without it is counted as missing data
    #: in `cheap_full_agreement`, never as agreement.
    cheap_full: dict | None = None
    #: WHY the output is what it is (`arms.verdict_triage` / `search_triage` / `stages_triage`), read by triage.py and
    #: by nothing that computes a rate. Optional: rows written before it existed carry none.
    triage: dict | None = None
    #: search_ai only: why the AI stopped (`proposer.StopReason`, or `model_budget`). `model_unavailable` = the model
    #: could not be reached after the router's retries, so the row is INCONCLUSIVE: the arm was never really run.
    ai_stop_reason: str | None = None
    #: the OUTPUT's fidelity numbers, REPORTED and never a gate (owner decision 2026-11-24). `gaming_flags` reads
    #: them to flag a clean output that moved the part. None = not measured (no extent, or a row written before
    #: they existed), counted as unmeasured and never as "within the limits".
    out_volume_ratio: float | None = None
    out_area_ratio: float | None = None
    out_bbox_ratio: float | None = None
    out_max_dev_mm: float | None = None
    out_bodies_before: int | None = None
    out_bodies_after: int | None = None

    @property
    def clean(self) -> bool:
        """The verdict's definition, derived: valid AND the whole panel passes.

        OWNER DECISION 2026-11-24: layer 3 does not gate. `out_faithful_ok` is still carried and reported, and a
        clean output that moved the part is FLAGGED (`gaming_flags`), never demoted."""
        return bool(self.out_valid
                    and self.out_panel_passes is not None and self.out_panel_passes == self.panel_size)

    @property
    def counts(self) -> bool:
        return not self.inconclusive

    @property
    def derived_inconclusive(self) -> bool:
        """What the search's own fields say about whether this file was measured at all (18b's rule:
        `infrastructure_failed` = some candidate inconclusive, none with a verdict, start not clean)."""
        from meshpipeline.cad.repair_v4.measurement import file_unmeasured  # the engine's own rule, not a copy

        return file_unmeasured(reason=self.reason, infrastructure_failed=self.infrastructure_failed,
                               inconclusive_candidates=self.inconclusive_candidates,
                               verdict_candidates=self.verdict_candidates, out_clean=self.out_clean,
                               out_tier=self.out_tier)


def _row_from_dict(d: Mapping[str, Any]) -> ArmRow:
    try:
        row = ArmRow(**dict(d))
    except TypeError as exc:
        raise ReportRefused(f"malformed arm row {dict(d)!r}: {exc}") from exc
    where = f"{row.name} [{row.arm}]"
    try:
        a = ev.arm(row.arm)
    except ValueError as exc:
        raise ReportRefused(f"{where}: {exc}") from exc
    if a.name != row.arm:
        raise ReportRefused(f"{where}: rows carry the canonical arm name {a.name!r}, not an alias")
    for label, value in (("model_usd", row.model_usd), ("wall_s", row.wall_s)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ReportRefused(f"{where}: {label}={value!r} is not a finite non-negative number")
    if row.model_usd > 0 and not a.uses_model:
        raise ReportRefused(f"{where}: arm {a.name!r} makes no metered model calls but reports "
                            f"${row.model_usd} of spend")
    if row.model_usd > a.budget.model_usd + SPEND_EPS_USD:
        raise ReportRefused(f"{where}: ${row.model_usd} of model spend on one file is over the arm's "
                            f"${a.budget.model_usd:g} per-file ceiling")
    for cname, count in (("verdict_candidates", row.verdict_candidates),
                         ("inconclusive_candidates", row.inconclusive_candidates)):
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
            raise ReportRefused(f"{where}: {cname}={count!r} is not a non-negative integer")
    if row.reason is not None and not isinstance(row.reason, str):
        raise ReportRefused(f"{where}: reason={row.reason!r} is not a string")
    if row.infrastructure_failed is not None and not isinstance(row.infrastructure_failed, bool):
        raise ReportRefused(f"{where}: infrastructure_failed={row.infrastructure_failed!r} is not a boolean")
    if row.triage is not None and not isinstance(row.triage, dict):
        raise ReportRefused(f"{where}: triage={row.triage!r} is not an object")
    if row.cheap_full is not None:
        if row.arm not in SEARCH_ARMS:
            raise ReportRefused(f"{where}: cheap_full is a search measurement; arm {row.arm!r} runs no search")
        if not isinstance(row.cheap_full, dict) or set(row.cheap_full) != set(CHEAP_FULL_CELLS) or any(
                not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in row.cheap_full.values()):
            raise ReportRefused(f"{where}: cheap_full={row.cheap_full!r} must map exactly {list(CHEAP_FULL_CELLS)} "
                                "to non-negative integers")
    if row.infrastructure_failed is False and row.reason == "no_verdict":
        raise ReportRefused(f"{where}: reason='no_verdict' but infrastructure_failed=False: contradictory")
    if row.derived_inconclusive and not row.inconclusive:
        raise ReportRefused(
            f"{where}: the search says nothing was measured (reason={row.reason!r}, verdicts="
            f"{row.verdict_candidates}, inconclusive candidates={row.inconclusive_candidates}, "
            f"infrastructure_failed={row.infrastructure_failed}) but the row says inconclusive=false: a search "
            "that never got a verdict is NOT a conclusive failure")
    if row.inconclusive:
        return row  # carries no verdict; the (never-used) fields are not read
    if row.arm in SEARCH_ARMS and (row.reason is None or row.verdict_candidates is None
                                   or row.inconclusive_candidates is None):
        raise ReportRefused(f"{where}: a conclusive search row must carry reason, verdict_candidates and "
                            "inconclusive_candidates (SearchResult's own fields): without them a search that "
                            "never measured anything cannot be told from one that failed")
    if row.arm == "input_only" and (row.in_valid is None or row.in_faithful_ok is None):
        raise ReportRefused(f"{where}: an input_only row must carry in_valid and in_faithful_ok so that "
                            "'the input passed through unchanged' can be checked on every layer")
    need = {"in_clean": row.in_clean, "in_panel_passes": row.in_panel_passes, "out_valid": row.out_valid,
            "out_faithful_ok": row.out_faithful_ok, "out_panel_passes": row.out_panel_passes,
            "panel_size": row.panel_size, "out_tier": row.out_tier, "out_clean": row.out_clean}
    absent = [k for k, v in need.items() if v is None]
    if absent:
        raise ReportRefused(f"{where}: a conclusive row is missing {absent}; a row the checker could not "
                            "judge must say inconclusive=true instead of omitting its verdict")
    assert row.panel_size is not None and row.out_panel_passes is not None and row.in_panel_passes is not None
    if row.out_tier != "full":
        raise ReportRefused(f"{where}: output judged at tier {row.out_tier!r}; only the full panel can "
                            "say a file is clean")
    if row.panel_size < 1 or not 0 <= row.out_panel_passes <= row.panel_size \
            or not 0 <= row.in_panel_passes <= row.panel_size:
        raise ReportRefused(f"{where}: panel passes {row.in_panel_passes}->{row.out_panel_passes} of "
                            f"{row.panel_size} is impossible")
    if row.in_clean and row.in_panel_passes != row.panel_size:
        raise ReportRefused(f"{where}: input clean but only {row.in_panel_passes}/{row.panel_size} panel passes")
    if bool(row.out_clean) != row.clean:
        raise ReportRefused(f"{where}: stored clean={row.out_clean} but valid={row.out_valid}, "
                            f"faithful_ok={row.out_faithful_ok}, panel {row.out_panel_passes}/{row.panel_size} "
                            f"make it {row.clean}")
    return row


def parse_arm_rows(raw: list[dict]) -> list[ArmRow]:
    return [_row_from_dict(d) for d in raw]


def row_to_dict(row: ArmRow) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(row)


# ---------------------------------------------------------------------------------------------- loading

_TOKEN = object()


@dataclass(frozen=True)
class WaveResult:
    """Rows that passed every gate. Built only by `load_wave`."""

    wave: Any  # evalspec.WaveDef
    wave_id: str
    commit: str
    image: str
    rows: tuple[ArmRow, ...]
    kinds: Mapping[str, str]  # sha256 -> defect kind (from the split)
    n_files: int
    shard_wall_seconds: tuple[float, ...]
    frozen: bool
    declined: frozenset[str] = frozenset()  # sha256 of the declined-stratum files (expected outcome: declined)
    #: the shards' EXCLUDED entries as the harness wrote them: a wall kill or a pool death, carried here so the
    #: report can NAME them (`excluded_stratum`) instead of only counting them as inconclusive
    excluded: tuple[Mapping[str, Any], ...] = ()
    #: the walls the shards ACTUALLY RAN WITH, from their provenance (collect.wall_values): the stratum must not
    #: quote whatever the constants say when the wave is reported
    walls: Mapping[str, Any] = field(default_factory=dict)
    slot: str | None = None  # the once-only slot the shards carry (held-out / checkpoint waves)
    slot_verified: bool = False  # ... and whether the marker store confirmed this wave_id holds it
    #: the merged `wave_backoff` provenance of every shard (see `backoff_summary`); a wave that was throttled must not
    #: pass for a free-running one, or its wall-clock would be read as the engine's
    backoff: Mapping[str, Any] | None = None
    #: LABEL DRIFT (lane C14b): sha256 -> the split's clean label and the input verdict THIS commit measured, for files
    #: where they disagree. Its own stratum, named, in no rate - not a wave refusal (v4's dev report refused on 4 such
    #: files, and a held-out run labelled on old code cannot be repeated to try again).
    drifted: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    _key: object = None

    def __post_init__(self) -> None:
        if self._key is not _TOKEN:
            raise TypeError("a WaveResult is built by load_wave, which applies every refusal collect.py makes")


def backoff_summary(shards: Iterable[Mapping[str, Any]], *, has_model_arm: bool) -> dict[str, Any]:
    """Merge each shard's `wave_backoff` provenance into one verdict on whether the wave was throttled.
    status: "not_applicable" (no model arm), "throttled" (a wave-level trip, or shards held calls for one),
    "free" (every shard ran the guard and none tripped or held), "unrecorded" (a shard carries no account, or ran
    without the guard, or its marker store failed: absence of a back-off is NOT claimed)."""
    shards = list(shards)
    if not has_model_arm:
        return {"status": "not_applicable"}
    accounts = [s["prov"].get("wave_backoff") for s in shards]
    unguarded = sum(1 for a in accounts if not isinstance(a, Mapping) or a.get("guard") != "wave_backoff")
    live = [a for a in accounts if isinstance(a, Mapping) and a.get("guard") == "wave_backoff"]
    trips: dict[tuple, dict] = {}
    for a in live:
        for t in a.get("trips_seen", []):
            trips[(t.get("bucket"), t.get("by_shard"))] = t
    held = sum(float(a.get("held_s") or 0.0) for a in live)
    store_errors = sum(int(a.get("store_errors") or 0) for a in live)
    out: dict[str, Any] = {
        "trips": sorted(trips.values(), key=lambda t: t.get("at", 0.0)), "shards": len(shards),
        "shards_without_guard": unguarded, "shards_held": sum(1 for a in live if a.get("holds")),
        "held_s_total": round(held, 1), "held_s_max_shard": round(max((float(a.get("held_s") or 0.0) for a in live),
                                                                     default=0.0), 1),
        "rate_limited_calls_reported": sum(int(a.get("reported_429") or 0) for a in live), "store_errors": store_errors}
    out["status"] = ("throttled" if trips or held > 0 else "unrecorded" if unguarded or store_errors else "free")
    return out


def defect_kind(split_row: Any) -> str:
    return split_row.family or str(split_row.first_failing_layer or "clean_control")


def _same(shards: list[dict], key: str, want: Any, what: str) -> None:
    got = {json.dumps(s["prov"].get(key), sort_keys=True) for s in shards}
    if got != {json.dumps(want, sort_keys=True)}:
        raise ReportRefused(f"shards disagree with the wave on {what}: shards say {sorted(got)}, the wave "
                            f"definition says {json.dumps(want)}")


def load_wave(root: Path, wave: Any, split_rows: Iterable[Any], *, n_shards: int, frozen_commit: str | None = None,
              shard_plan: dict | None = None,
              purpose: str = "report", markers: Any = None) -> WaveResult:
    """Every gate, then the rows. `split_rows` are the split file's ProbeRows (rp.read_split).

    ONCE-ONLY, enforced at READ time as well as at launch (the launcher calls `plan_wave`; this is the check that
    does not trust the launcher): the shards of a held-out or checkpoint wave must carry the
    `slot` that `plan_wave` claimed (WavePlan.provenance), agreed by every shard and one of the type's own keys.
    A run launched around `plan_wave` has none and is refused. When the durable `markers` store is passed, the
    slot must also be held by THIS wave_id there; without it the report says the slot was not verified."""
    if purpose not in ("report", "tuning"):
        raise ReportRefused(f"purpose must be 'report' or 'tuning', not {purpose!r}")
    if purpose == "tuning":
        ev.refuse_heldout_for_tuning(wave.split)
    if wave.split == ev.HELDOUT:
        if not frozen_commit or not _SHA40.match(frozen_commit):
            raise ReportRefused("the held-out report needs --frozen-commit <40-hex>: the commit the owner froze "
                                "before the one held-out run")
    split = list(split_rows)
    rp.check_sources(split)  # Fusion 360 raises DisallowedSource: reuse, not a second list
    declined_kinds = set(rp.load_inject().DECLINED_STRATUM_KINDS)
    for r in split:
        if r.kind in declined_kinds and r.kind_status != DECLINED_STATUS:
            raise ReportRefused(f"{r.name}: injector kind {r.kind!r} is the declined stratum but the split row's "
                                f"status is {r.kind_status!r}, not {DECLINED_STATUS!r}: it would be counted as a "
                                "failing file")
        if r.kind_status == DECLINED_STATUS and r.kind not in declined_kinds:
            raise ReportRefused(f"{r.name}: status {DECLINED_STATUS!r} on kind {r.kind!r}, which is not a declined kind")
    declined = frozenset(r.sha256 for r in split if r.kind_status == DECLINED_STATUS)
    if not split:
        raise ReportRefused("the split is empty")
    if len({r.sha256 for r in split}) != len(split):
        raise ReportRefused("the split lists a file twice")

    shards = _collect.load_shards(root, n_shards, parse_rows=parse_arm_rows)
    _collect.check_partition(shards, shard_plan)
    if frozen_commit and {str(s["prov"].get("commit")) for s in shards} != {frozen_commit}:
        raise ReportRefused(f"the shards ran commit {sorted({str(s['prov'].get('commit')) for s in shards})}, "
                            f"not the frozen {frozen_commit}")
    ids = {str(s["prov"].get("wave_id")) for s in shards}
    if len(ids) != 1 or "None" in ids:
        raise ReportRefused(f"shards do not share one wave_id: {sorted(ids)}")
    _same(shards, "wave", wave.name, "the wave name")
    _same(shards, "wave_type", wave.wave_type, "the wave type")
    _same(shards, "split", wave.split, "the split")
    _same(shards, "arms", list(wave.arms), "the arms")
    _same(shards, "wave_cap_usd", wave.cap_usd, "the spending cap")
    slot, slot_verified = None, False
    keys = ev.slot_keys(wave.wave_type)
    if keys:
        slots = {s["prov"].get("slot") for s in shards}
        slot = next(iter(slots))
        if len(slots) != 1 or slot not in keys:
            raise ReportRefused(f"a {wave.wave_type} wave must carry the slot plan_wave claimed ({keys}); the "
                                f"shards say {sorted(map(str, slots))}: this wave was launched around plan_wave "
                                "and may not be the once-only run it claims to be. Missing: the harness must copy "
                                "plan_wave(...).provenance() (it includes `slot`) into each shard's PROVENANCE.json")
        if markers is not None:
            if markers.get(slot) != next(iter(ids)):
                raise ReportRefused(f"slot {slot} is held by {markers.get(slot)!r} in the marker store, not by "
                                    f"this wave {next(iter(ids))!r}")
            slot_verified = True

    shard_cap = wave.cap_usd / n_shards
    if wave.has_model_arm:
        for s in shards:
            if s["prov"].get("ledger") != "shard":
                raise ReportRefused(f"{s['dir'].name}: a wave with a model arm ran without a shard ledger "
                                    f"(provenance {s['prov'].get('ledger')!r})")
        original = _original_shard_spend(shards)
        if original is not None:
            # A REGROUPED wave (snap-v4's per-arm groups merged into one wave): a merged shard pools rows from several
            # ORIGINAL shards, so its sum is not what any ledger bound. Each original shard ran its own ledger slice,
            # the wave type's cap over ITS group's shard count, and is checked against exactly that (lane C13: v4's
            # merged report refused a merged shard while every original AI shard stayed under its cap, max $1.16).
            for g, n_g, i, spent in original:
                cap_g = wave.cap_usd / n_g
                if spent > cap_g + SPEND_EPS_USD:
                    raise ReportRefused(f"group {g} shard {i}/{n_g}: spent ${spent:.4f}, over its slice ${cap_g:.4f} "
                                        f"of the ${wave.cap_usd:g} {wave.wave_type} cap: the ledger did not bind")
        else:
            for s in shards:
                spent = sum(r.model_usd for r in s["rows"])
                if spent > shard_cap + SPEND_EPS_USD:
                    raise ReportRefused(f"{s['dir'].name}: spent ${spent:.4f}, over its slice ${shard_cap:.4f} "
                                        f"of the ${wave.cap_usd:g} {wave.wave_type} cap: the ledger did not bind")

    by_sha = {r.sha256: r for r in split}
    for s in shards:
        rp.check_sources(SimpleNamespace(name=x.get("name"), source=x.get("source")) for x in s["excluded"])
    rows: dict[tuple[str, str], ArmRow] = {}
    for s in shards:
        for r in s["rows"]:
            key = (r.sha256, r.arm)
            if key in rows:
                raise ReportRefused(f"{r.name} has two rows for arm {r.arm!r}")
            rows[key] = r
        for x in s["excluded"]:  # a per-file kill: inconclusive for every arm that has no row of its own
            for a in wave.arms:
                rows.setdefault((x["sha256"], a), ArmRow(x["name"], x["sha256"], x["source"], a, True, 0.0, 0.0))
    rp.check_sources(rows.values())
    expected = {(sha, a) for sha in by_sha for a in wave.arms}
    missing = sorted(expected - set(rows))
    if missing:
        raise ReportRefused(f"{len(missing)} (file, arm) pair(s) have no row (first: {missing[0][0][:12]} "
                            f"{missing[0][1]}): the arm did not finish every file")
    extra = sorted(set(rows) - expected)
    if extra:
        raise ReportRefused(f"{len(extra)} row(s) are not in the {wave.split} split x {list(wave.arms)} "
                            f"(first: {extra[0][0][:12]} {extra[0][1]})")

    drifted: dict[str, dict[str, Any]] = {}
    for sha, base in by_sha.items():
        truth = base.verdict_summary.get("clean")
        if truth is None:
            raise ReportRefused(f"{base.name}: the split row carries no clean flag to check the arms against")
        for a in wave.arms:
            r = rows[(sha, a)]
            if r.inconclusive:
                continue
            if r.source != base.source:
                raise ReportRefused(f"{r.name} [{a}]: source {r.source!r} but the split says {base.source!r}")
            if bool(r.in_clean) != bool(truth):
                # the split was labelled on older code than this wave ran: the file is reported in its own stratum
                # with the input verdict measured here, and left out of every rate (the arms must still agree with
                # each other on that input: the "one file has one input" check below still refuses otherwise)
                drifted[sha] = {"name": base.name, "split_clean": bool(truth), "input_clean": bool(r.in_clean),
                                "input_valid": r.in_valid, "input_panel_passes": r.in_panel_passes,
                                "panel_size": r.panel_size}
            if a == "input_only" and (r.out_clean != r.in_clean or r.out_panel_passes != r.in_panel_passes
                                      or r.out_valid != r.in_valid or r.out_faithful_ok != r.in_faithful_ok):
                raise ReportRefused(f"{r.name} [input_only]: the output differs from the input "
                                    f"(clean {r.in_clean}->{r.out_clean}, panel {r.in_panel_passes}->"
                                    f"{r.out_panel_passes}, valid {r.in_valid}->{r.out_valid}, faithful_ok "
                                    f"{r.in_faithful_ok}->{r.out_faithful_ok}): input_only did not pass the "
                                    "input through")
        live = [rows[(sha, a)] for a in wave.arms if not rows[(sha, a)].inconclusive]
        seen = {(r.in_clean, r.in_panel_passes) for r in live}
        layers = {(r.in_valid, r.in_faithful_ok) for r in live if r.in_valid is not None or r.in_faithful_ok is not None}
        if len(layers) > 1:
            raise ReportRefused(f"{base.name}: arms measured different input layers {sorted(map(str, layers))}: "
                                "one file has one input")
        if len(seen) > 1:
            raise ReportRefused(f"{base.name}: arms measured different inputs {sorted(map(str, seen))}: one "
                                "file has one input")

    if len(drifted) > MAX_LABEL_DRIFT_FRACTION * len(split):
        first = next(iter(drifted.values()))
        raise ReportRefused(f"{len(drifted)} of {len(split)} files have an input verdict the {wave.split} split "
                            f"contradicts (first: {first['name']}: split says clean={first['split_clean']}, input "
                            f"clean={first['input_clean']}); over {MAX_LABEL_DRIFT_FRACTION:.0%} that is a broken "
                            "checker, not stale labels")
    total = sum(r.model_usd for r in rows.values())
    if total > wave.cap_usd + SPEND_EPS_USD:
        raise ReportRefused(f"wave spend ${total:.2f} exceeds the ${wave.cap_usd:g} {wave.wave_type} cap")

    first = shards[0]["prov"]
    return WaveResult(
        wave, next(iter(ids)), str(first["commit"]), str(first["image"]), tuple(rows.values()),
        {sha: defect_kind(r) for sha, r in by_sha.items()}, len(split),
        tuple(float(s["prov"].get("wall_seconds") or 0.0) for s in shards), bool(frozen_commit),
        declined=declined, slot=slot, slot_verified=slot_verified, walls=_collect.wall_values(shards),
        backoff=backoff_summary(shards, has_model_arm=bool(wave.has_model_arm)),
        excluded=tuple(dict(x) for s in shards for x in s["excluded"]), drifted=drifted, _key=_TOKEN)


# ---------------------------------------------------------------------------------------------- the report

def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def _arm_block(arm: str, rows: list[ArmRow], kinds: Mapping[str, str], n_files: int,
               stratum_rows: Sequence[ArmRow] = ()) -> dict[str, Any]:
    """One arm's numbers over ITS OWN conclusive files. When the arm is NOT MEASURED, withhold means withhold:
    every figure from which a clean rate is recoverable (counts, layers, regressions, per-kind rates) is None,
    not only the headline."""
    conclusive = [r for r in rows if r.counts]
    inc = n_files - len(conclusive)
    inc_rate = inc / n_files
    measured = inc_rate <= MAX_INCONCLUSIVE_FRACTION and bool(conclusive)

    def only(v: Any) -> Any:
        return v if measured else None

    clean_out = sum(r.clean for r in conclusive)
    clean_in = sum(bool(r.in_clean) for r in conclusive)
    c2u = sum(bool(r.in_clean) and not r.clean for r in conclusive)
    fewer = sum(r.out_panel_passes < r.in_panel_passes for r in conclusive)  # type: ignore[operator]
    either = sum((bool(r.in_clean) and not r.clean) or r.out_panel_passes < r.in_panel_passes  # type: ignore[operator]
                 for r in conclusive)
    by_kind: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[ArmRow]] = defaultdict(list)
    for r in rows:
        groups[kinds[r.sha256]].append(r)
    for kind, rs in sorted(groups.items()):
        con = [r for r in rs if r.counts]
        kind_inc = len(rs) - len(con)
        ok = measured and bool(con) and kind_inc / len(rs) <= MAX_INCONCLUSIVE_FRACTION
        by_kind[kind] = {"files": len(rs), "conclusive": len(con), "inconclusive": kind_inc,
                         "clean": sum(r.clean for r in con) if ok else None,
                         "clean_rate": _rate(sum(r.clean for r in con), len(con)) if ok else None,
                         "measured": ok}
    metered = ev.ARMS[arm].spend_metered
    # Money and wall-clock are REAL whatever the file's category: the declined stratum is out of every RATE, not out
    # of the bill, so the spend the cap is checked against still includes it.
    billed = [*rows, *stratum_rows]
    spend = sum(r.model_usd for r in billed)
    # B18: wall-stopped rows COUNT (their best state carries a full verdict), and they are counted SEPARATELY so a
    # reader can take them out of the denominator if they choose. `clean` among them is shown because a search that
    # ran out of wall can still have reached clean before it did.
    walled = [r for r in conclusive if r.stopped_on_wall]
    return {
        "files": n_files, "conclusive": len(conclusive), "inconclusive": inc, "inconclusive_rate": inc_rate,
        "measured": measured,
        "stopped_on_wall": {"conclusive_rows": len(walled), "clean": sum(r.clean for r in walled),
                            "share_of_conclusive": _rate(len(walled), len(conclusive)),
                            "clean_rate_without_them": only(_rate(sum(r.clean for r in conclusive
                                                                      if not r.stopped_on_wall),
                                                                  len(conclusive) - len(walled)))},
        "clean_in": only(clean_in), "clean_out": only(clean_out),
        "clean_rate": only(_rate(clean_out, len(conclusive))),
        "clean_gain": only(clean_out - clean_in),
        "clean_gain_rate": only(_rate(clean_out - clean_in, len(conclusive))),
        "regressions": ({"clean_to_unclean": c2u, "fewer_panel_passes": fewer, "either": either} if measured
                        else {"clean_to_unclean": None, "fewer_panel_passes": None, "either": None}),
        "layers": ({"valid": sum(bool(r.out_valid) for r in conclusive),
                    "faithful_ok": sum(bool(r.out_faithful_ok) for r in conclusive),
                    "panel_all_pass": sum(r.out_panel_passes == r.panel_size for r in conclusive),
                    "clean": clean_out} if measured
                   else {"valid": None, "faithful_ok": None, "panel_all_pass": None, "clean": None}),
        "by_defect_kind": by_kind,
        "spend_metered": metered,
        "model_usd": ({"total": spend, "per_file_mean": spend / len(billed)} if metered
                      else {"total": None, "per_file_mean": None}),
        "wall_s": {"total": sum(r.wall_s for r in billed), "per_file_mean": sum(r.wall_s for r in billed) / len(billed),
                   "max": max(r.wall_s for r in billed)},
    }


def _drift_block(drifted: Mapping[str, Mapping[str, Any]], rows: Mapping[str, Mapping[str, ArmRow]],
                 split: str) -> dict[str, Any]:
    """Files whose split label disagrees with the input verdict THIS commit measured (lane C14b). Named, with that
    verdict and every arm's outcome; in NO clean rate, gain, regression or marginal - the split's label is stale, so
    neither 'repaired' nor 'regressed' can be read off them."""
    files = []
    for sha, d in sorted(drifted.items(), key=lambda kv: str(kv[1]["name"])):
        arms = {a: {"out_clean": r.clean, "out_panel_passes": r.out_panel_passes, "inconclusive": r.inconclusive}
                for a, r in sorted(rows.get(sha, {}).items())}
        files.append({"sha256": sha, **d, "arms": arms})
    return {"counted_as": f"none: the {split} split's label disagrees with this commit's input verdict; in no rate",
            "files": len(files), "names": files}


def _declined_block(stratum: Mapping[str, list[ArmRow]], n: int) -> dict[str, Any]:
    """The declined stratum, per arm. NEVER a failure and in no clean rate or gain. `declined` = conclusive and no
    clean output (the expected outcome); `unexpectedly_clean` = conclusive and clean (a finding about the fill cap
    or the judge, shown, not credited as gain); `inconclusive` = not measured. The row cannot tell an op's
    OpDeclined from a search that found nothing, and does not pretend to."""
    arms: dict[str, dict[str, Any]] = {}
    for a, rs in stratum.items():
        con = [r for r in rs if r.counts]
        arms[a] = {"files": len(rs), "inconclusive": len(rs) - len(con),
                   "declined": sum(not r.clean for r in con), "unexpectedly_clean": sum(r.clean for r in con)}
    return {"expected_outcome": "correctly_declined", "files": n, "counted_as": "none: not a failure, not a gain",
            "arms": arms}


def _excluded_block(excluded: Sequence[Mapping[str, Any]], n_files: int,
                    walls: Mapping[str, Any]) -> dict[str, Any]:
    """THE EXCLUDED STRATUM, built by the same code collect.py uses for the re-probe so there is ONE mechanism:
    files the harness reached no verdict on (a per-file or one-call wall kill, a pool death, an unreadable
    input), counted and NAMED. They are inconclusive for every arm, so they are already outside every clean
    rate, gain and regression above; this block is what keeps them from being invisible there. `fraction_of_pool`
    is over the split, not the re-probe pool, so collect's 2% limit does not apply to it and is not quoted.

    An arm wave writes one entry per (file, arm), so the rows the stratum's fraction is taken over are counted
    from the DISTINCT files excluded, never from the entries (`collect.excluded_stratum` dedupes on sha256)."""
    files = len({str(x.get("sha256")) for x in excluded})
    block = _collect.excluded_stratum(
        {"rows": [None] * max(n_files - files, 0), "excluded": list(excluded)}, walls)
    for key in ("limit", "over_limit"):
        block.pop(key, None)
    block["counted_as"] = ("inconclusive for every arm: not a failure, not a gain, in no clean rate and no "
                           "regression")
    block["fraction_of_split"] = block.pop("fraction_of_pool")
    return block


#: Owner review thresholds for a CLEAN output that moved the part (owner decision 2026-11-24). They FLAG, they do
#: not demote: a flagged file is still clean and still in every rate. The owner reads the list.
GAMING_VOLUME_CHANGE = 0.25   # |volume_ratio - 1| above this
GAMING_BBOX_SHRINK = 0.10     # bbox_ratio below 1 - this


def _original_shard_spend(shards: Sequence[dict]) -> list[tuple[str, int, int, float]] | None:
    """(group, its shard count, shard index, spend) for every ORIGINAL shard of a regrouped wave, from the merged
    provenance's `groups[g].shard_spend_usd` (snap-v4 regroup.py). None when the wave was not regrouped or a merged
    shard does not carry the numbers, so the per-shard check applies as before. Every merged shard carries the SAME
    groups block; a disagreement is refused, not averaged."""
    blocks = [s["prov"].get("groups") for s in shards if s["prov"].get("regrouped")]
    if not blocks or len(blocks) != len(shards):
        return None
    if any(not isinstance(b, dict) or any("shard_spend_usd" not in v for v in b.values()) for b in blocks):
        return None
    first = json.dumps({g: v["shard_spend_usd"] for g, v in blocks[0].items()}, sort_keys=True)
    if any(json.dumps({g: v["shard_spend_usd"] for g, v in b.items()}, sort_keys=True) != first for b in blocks):
        raise ReportRefused("the merged shards disagree on the original shards' spend: the regroup is inconsistent")
    out = []
    for g, v in sorted(blocks[0].items()):
        spends = [float(x) for x in v["shard_spend_usd"]]
        n_g = int(v.get("n_shards") or len(spends))
        if len(spends) != n_g:
            raise ReportRefused(f"group {g}: {len(spends)} original shard spends for {n_g} shards")
        out += [(g, n_g, i + 1, x) for i, x in enumerate(spends)]
    return out


def gaming_why(row: ArmRow) -> list[str]:
    """Why this CLEAN row is worth the owner's eye, in its own numbers; empty when it is inside both limits or was
    never measured."""
    why = []
    if row.out_volume_ratio is not None and abs(row.out_volume_ratio - 1.0) > GAMING_VOLUME_CHANGE:
        why.append(f"volume x{row.out_volume_ratio:.3g}")
    if row.out_bbox_ratio is not None and row.out_bbox_ratio < 1.0 - GAMING_BBOX_SHRINK:
        why.append(f"bbox x{row.out_bbox_ratio:.3g}")
    return why


def _gaming_block(rows: Sequence[ArmRow], arms: Sequence[str]) -> dict[str, Any]:
    """Clean outputs that moved the part a long way, per arm, NAMED. Not a failure and not a demotion: these files
    are clean under the definition (valid + panel) and stay in every count. The flag exists because a repair that
    deletes or inflates geometry can satisfy the meshers, and the owner reviews that by hand."""
    out: dict[str, Any] = {}
    for a in arms:
        mine = [r for r in rows if r.arm == a and r.counts and r.clean]
        named = [{"name": r.name, "sha256": r.sha256, "source": r.source, "volume_ratio": r.out_volume_ratio,
                  "area_ratio": r.out_area_ratio, "bbox_ratio": r.out_bbox_ratio, "max_dev_mm": r.out_max_dev_mm,
                  "bodies": None if r.out_bodies_before is None else [r.out_bodies_before, r.out_bodies_after],
                  "why": ", ".join(w)}
                 for r, w in ((r, gaming_why(r)) for r in mine) if w]
        out[a] = {"files": len(named), "clean_reviewed": len(mine),
                  "unmeasured": sum(1 for r in mine if r.out_volume_ratio is None and r.out_bbox_ratio is None),
                  "names": sorted(named, key=lambda x: str(x["sha256"]))}
    return {"counted_as": "clean: flagged for owner review, in every rate and no failure",
            "limits": {"volume_change": GAMING_VOLUME_CHANGE, "bbox_shrink": GAMING_BBOX_SHRINK},
            "arms": out}


#: The worked example the valid-not-clean group is headed by (lane C12 on snap-v4-202611240851): search_ai DELIVERED
#: a VALID state with 0.07% of the part left. score() ranks validity first, so it outranked the intact invalid input.
VALID_UNCLEAN_EXAMPLE = ("0051_00510567 search_ai (v4): volume x0.00069, area x0.0036, bbox x0.281, max dev 40.0 mm, "
                         "bodies 8->4 - VALID, delivered as improved, and flagged nowhere before this group existed")


def _valid_unclean_gaming_block(rows: Sequence[ArmRow], arms: Sequence[str]) -> dict[str, Any]:
    """VALID but NOT clean outputs of the REPAIR arms that moved the part a long way, NAMED with their numbers (lane
    C12). Their own group, NOT clean-rate gaming: these rows are in no clean rate at all. The gaming rule used to read
    clean outputs only, so a delivered near-empty part (VALID_UNCLEAN_EXAMPLE) was flagged nowhere. Reporting only:
    nothing here changes a count, a verdict or what is delivered. input_only is skipped (its output is the input)."""
    out: dict[str, Any] = {}
    for a in arms:
        if a == "input_only":
            continue
        mine = [r for r in rows if r.arm == a and r.counts and not r.clean and r.out_valid]
        named = [{"name": r.name, "sha256": r.sha256, "source": r.source, "volume_ratio": r.out_volume_ratio,
                  "area_ratio": r.out_area_ratio, "bbox_ratio": r.out_bbox_ratio, "max_dev_mm": r.out_max_dev_mm,
                  "bodies": None if r.out_bodies_before is None else [r.out_bodies_before, r.out_bodies_after],
                  "why": ", ".join(w)}
                 for r, w in ((r, gaming_why(r)) for r in mine) if w]
        out[a] = {"files": len(named), "valid_unclean_reviewed": len(mine),
                  "unmeasured": sum(1 for r in mine if r.out_volume_ratio is None and r.out_bbox_ratio is None),
                  "names": sorted(named, key=lambda x: str(x["sha256"]))}
    return {"counted_as": "VALID but NOT clean: in NO clean rate; a delivered output that moved the part a long way",
            "limits": {"volume_change": GAMING_VOLUME_CHANGE, "bbox_shrink": GAMING_BBOX_SHRINK},
            "example": VALID_UNCLEAN_EXAMPLE, "arms": out}


def _numbers(x: Mapping[str, Any]) -> str:
    """A flagged row's own numbers, never a bare flag."""
    def f(v: Any, fmt: str) -> str:
        return "-" if v is None else format(v, fmt)
    b = x.get("bodies")
    return (f"volume x{f(x['volume_ratio'], '.3g')}, area x{f(x['area_ratio'], '.3g')}, bbox x{f(x['bbox_ratio'], '.3g')}, "
            f"max dev {f(x['max_dev_mm'], '.3g')} mm, bodies {'-' if not b else f'{b[0]}->{b[1]}'}")


def _paired_columns(by_sha: Mapping[str, dict[str, ArmRow]], blocks: dict[str, dict[str, Any]],
                    n_files: int) -> dict[str, Any]:
    """The PAIRED marginal: each measured arm's clean rate over the files EVERY measured arm judged. Per-arm
    rates use each arm's own dropouts (up to the limit, different per arm, and correlated with hard files), so
    they are flattered differently and cannot be compared with each other; this column can. An arm that is NOT
    MEASURED has no rate to pair and does not shrink the shared set."""
    arms = [a for a, b in blocks.items() if b["measured"]]
    shared = [s for s, by in by_sha.items() if arms and all(by[a].counts for a in arms)]
    for a, b in blocks.items():
        if a in arms and shared:
            clean = sum(by_sha[s][a].clean for s in shared)
            cin = sum(bool(by_sha[s][a].in_clean) for s in shared)
            b["paired"] = {"n": len(shared), "clean": clean, "clean_rate": clean / len(shared),
                           "clean_in": cin, "clean_gain": clean - cin}
        else:
            b["paired"] = {"n": len(shared), "clean": None, "clean_rate": None, "clean_in": None, "clean_gain": None}
    return {"arms": arms, "n_files": len(shared), "of": n_files,
            # plan amendment 2026-11-23 (3): the threshold is REPORTED beside the actual fraction, every time, so a
            # reader sees the number it is judging and not only a warning when it is crossed
            "fraction": len(shared) / n_files if n_files else None, "min_fraction": MIN_PAIRED_FRACTION,
            "excluded_not_measured": [a for a in blocks if a not in arms]}


def _agreement(cells: Mapping[str, int]) -> dict[str, Any]:
    """`reprobe.agreement`'s shape, over a cheap/full table."""
    pp, pf = cells["cheap_pass_full_pass"], cells["cheap_pass_full_fail"]
    fp, ff = cells["cheap_fail_full_pass"], cells["cheap_fail_full_fail"]
    both = pp + pf + fp + ff
    return {"compared": both, **dict(cells), "agree": (pp + ff) / both if both else None,
            "cheap_miss_rate_among_cheap_passes": pf / (pp + pf) if pp + pf else None,
            "cheap_false_alarm_rate_among_cheap_fails": fp / (fp + ff) if fp + ff else None}


def _cheap_full_block(rows: Iterable[ArmRow], arms: Sequence[str]) -> dict[str, Any]:
    """Cheap/full agreement of the search's ranking tier, per search arm and pooled, over the states each search
    confirmed. Every row counts, whatever the file's category: it measures the TIER, not the file. A search row with
    no table is counted in `rows_without_data`, so missing data cannot read as perfect agreement."""
    search = [a for a in arms if a in SEARCH_ARMS]
    if not search:
        return {"measured": False, "reason": "no search arm in this wave: the cheap tier only ranks search states"}
    per: dict[str, dict[str, Any]] = {}
    pooled = dict.fromkeys(CHEAP_FULL_CELLS, 0)
    for a in search:
        cells = dict.fromkeys(CHEAP_FULL_CELLS, 0)
        missing = 0
        for r in rows:
            if r.arm != a:
                continue
            if r.cheap_full is None:
                missing += not r.inconclusive  # an unmeasured search confirmed nothing; a conclusive one must say so
                continue
            for k in CHEAP_FULL_CELLS:
                cells[k] += r.cheap_full[k]
                pooled[k] += r.cheap_full[k]
        per[a] = {**_agreement(cells), "rows_without_data": missing}
    total = _agreement(pooled)
    return {"measured": total["compared"] > 0, "unit": "states confirmed by the full panel", "arms": per,
            "pooled": total, "rows_without_data": sum(p["rows_without_data"] for p in per.values()),
            "source": "search confirm events (the re-probe's own agreement is in collect.py report['agreement'])"}


def _paired(rows: Mapping[str, dict[str, ArmRow]], a: str, b: str, blocks: Mapping[str, Any]) -> dict[str, Any]:
    """clean(a) - clean(b) over the files BOTH arms judged. None, with the reason, if either arm is not
    measured: a marginal between an unmeasured arm and anything is not a number."""
    if a not in blocks or b not in blocks:
        return {"value": None, "reason": f"arm {a if a not in blocks else b} is not in this wave"}
    for x in (a, b):
        if not blocks[x]["measured"]:
            return {"value": None, "reason": f"{x} is NOT MEASURED ({blocks[x]['inconclusive_rate']:.0%} inconclusive)"}
    shas = [s for s, by in rows.items() if by[a].counts and by[b].counts]
    if not shas:
        return {"value": None, "reason": "no file was judged by both arms"}
    gain = sum(rows[s][a].clean for s in shas) - sum(rows[s][b].clean for s in shas)
    return {"value": gain / len(shas), "files_gained": gain, "n_paired": len(shas), "reason": None}


#: Member causes that are a REPEATED timeout judged against the file (lead ruling D5: it stays a file failure):
#: two timeouts, a second one when the retry budget was already spent, or a retry after a timeout that ended any
#: other non-pass way (panel.classify_after_timeout). Counted per arm because the rate moves with the MACHINE, not
#: only the file - the report must show it, and say when it jumps.
REPEATED_TIMEOUT_CAUSES = ("timeout_twice", "timeout_after_hang")
REPEATED_TIMEOUT_PREFIX = "after_timeout_"
#: A jump: an arm's files-with-a-repeated-timeout exceed the reference rate's expectation by at least this many
#: files AND by this factor. Both, so one file more on a small wave is not an alarm and a large wave is not muted.
TIMEOUT_JUMP_MIN_FILES = 3
TIMEOUT_JUMP_FACTOR = 2.0


def _is_repeated_timeout(cause: Any) -> bool:
    return isinstance(cause, str) and (cause in REPEATED_TIMEOUT_CAUSES or cause.startswith(REPEATED_TIMEOUT_PREFIX))


def _timeout_block(per_arm: Mapping[str, Sequence[ArmRow]]) -> dict[str, Any]:
    """Per arm: conclusive files, files with >= 1 repeated-timeout member, those members, and their causes."""
    arms: dict[str, Any] = {}
    for arm, rows in per_arm.items():
        conclusive = [r for r in rows if r.counts]
        causes: Counter[str] = Counter()
        files = 0
        for r in conclusive:
            hit = [str(m["cause"]) for m in ((r.triage or {}).get("panel_failing") or [])
                   if isinstance(m, Mapping) and _is_repeated_timeout(m.get("cause"))]
            causes.update(hit)
            files += bool(hit)
        arms[arm] = {"conclusive": len(conclusive), "files": files, "members": sum(causes.values()),
                     "rate": files / len(conclusive) if conclusive else None, "by_cause": dict(sorted(causes.items()))}
    return {"arms": arms, "wave": {"files": sum(b["files"] for b in arms.values()),
                                   "members": sum(b["members"] for b in arms.values())},
            "jump_rule": {"min_files": TIMEOUT_JUMP_MIN_FILES, "factor": TIMEOUT_JUMP_FACTOR}}


def _timeout_jumps(block: Mapping[str, Any], previous: Mapping[str, Any] | None) -> list[str]:
    """Warnings for arms whose repeated-timeout files jump against the reference: input_only of THIS wave when the
    wave has it (the same machines, the same files, no repair), else the same arm in `previous` (an earlier
    snapshot's report JSON). No reference, no claim."""
    out: list[str] = []
    arms = block["arms"]
    prev_arms = ((previous or {}).get("timeouts") or {}).get("arms") or {}
    for arm, b in arms.items():
        if arm == "input_only" or not b["conclusive"]:
            continue
        if "input_only" in arms and arms["input_only"]["conclusive"]:
            ref, label = arms["input_only"], "input_only in this wave"
        elif arm in prev_arms and prev_arms[arm].get("conclusive"):
            ref, label = prev_arms[arm], "the previous snapshot"
        else:
            continue
        expected = ref["files"] / ref["conclusive"] * b["conclusive"]
        if b["files"] - expected >= TIMEOUT_JUMP_MIN_FILES and b["files"] > TIMEOUT_JUMP_FACTOR * expected:
            out.append(f"{arm}: repeated member timeouts on {b['files']} of {b['conclusive']} files against "
                       f"{expected:.1f} expected from {label} ({ref['files']} of {ref['conclusive']}): a repeated "
                       "timeout is a file failure, and a jump this size usually means the machine got slower, not "
                       "that the files did - check the hosts before reading this arm's clean rate")
    return out


def build_report(result: WaveResult, *, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(result, WaveResult):
        raise TypeError("build_report takes a WaveResult from load_wave, never raw rows")
    wave = result.wave
    per_arm: dict[str, list[ArmRow]] = {a: [] for a in wave.arms}
    by_sha: dict[str, dict[str, ArmRow]] = defaultdict(dict)
    stratum: dict[str, list[ArmRow]] = {a: [] for a in wave.arms}
    drift_rows: dict[str, dict[str, ArmRow]] = defaultdict(dict)
    for r in result.rows:
        if r.sha256 in result.declined:  # the third category: kept out of EVERY figure below, reported on its own
            stratum[r.arm].append(r)
            continue
        if r.sha256 in result.drifted:  # label drift (C14b): kept out of every figure too, reported on its own
            drift_rows[r.sha256][r.arm] = r
            continue
        per_arm[r.arm].append(r)
        by_sha[r.sha256][r.arm] = r
    n_main = result.n_files - len(result.declined) - len(set(result.drifted) - set(result.declined))
    if n_main < 1:
        raise ReportRefused("every file in the split is declined-stratum: there is nothing to measure a clean rate on")
    blocks = {a: _arm_block(a, rs, result.kinds, n_main, stratum[a]) for a, rs in per_arm.items()}
    paired_set = _paired_columns(by_sha, blocks, n_main)
    warnings: list[str] = []
    for a, b in blocks.items():
        if not b["measured"]:
            warnings.append(f"{a}: NOT MEASURED - {b['inconclusive']} of {b['files']} files inconclusive "
                            f"({b['inconclusive_rate']:.0%}; limit {MAX_INCONCLUSIVE_FRACTION:.0%}); its clean rate is withheld")
        elif b["conclusive"] >= MIN_FILES_FOR_CONSTANT_WARNING and a != "input_only" \
                and b["clean_out"] in (0, b["conclusive"]):
            warnings.append(f"{a}: every conclusive file has the same outcome ({b['clean_out']}/{b['conclusive']} clean): "
                            "a constant verdict looks like a broken harness, not a result - but " + FALSE_POSITIVE_NOTE)
    # An arm whose numbers are withheld is not compared at all: "X and Y are indistinguishable on N files" would
    # leak that X's outcomes equal Y's on those files, which is exactly what withholding X's rate is meant to hide.
    names = [a for a in wave.arms if blocks[a]["measured"]]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = [s for s, by in by_sha.items() if by[a].counts and by[b].counts]
            if len(both) >= MIN_FILES_FOR_CONSTANT_WARNING and all(
                    (by_sha[s][a].clean, by_sha[s][a].out_panel_passes) == (by_sha[s][b].clean, by_sha[s][b].out_panel_passes)
                    for s in both):
                warnings.append(f"{a} and {b} are indistinguishable on all {len(both)} files they share: "
                                "check that the harness varies the arm; " + FALSE_POSITIVE_NOTE)
    timeouts = _timeout_block(per_arm)
    warnings += _timeout_jumps(timeouts, previous)
    if paired_set["arms"] and paired_set["n_files"] < MIN_PAIRED_FRACTION * n_main:
        warnings.append(
            f"the PAIRED columns cover only {paired_set['n_files']} of {n_main} files "
            f"({paired_set['n_files'] / n_main:.0%}; minimum {MIN_PAIRED_FRACTION:.0%}): each measured arm "
            "drops different files, so the shared set is no longer the split and the paired rates are about a "
            "subset the arms agreed to judge, not the corpus")
    shard_wall = list(result.shard_wall_seconds)
    return {
        "wave": wave.name, "wave_id": result.wave_id, "wave_type": wave.wave_type, "split": wave.split,
        "commit": result.commit, "image": result.image, "frozen_commit_checked": result.frozen,
        "slot": result.slot, "slot_verified_against_store": result.slot_verified,
        #: the held-out split is spent in ONE use, so this label is available only when the marker store confirmed
        #: this wave_id holds the once-only slot. A slot named in the shards' own provenance proves nothing.
        "primary_artifact": wave.split == ev.HELDOUT and result.slot_verified,
        "heldout_split": wave.split == ev.HELDOUT,
        "files": result.n_files, "files_measured_strata": n_main, "arms": blocks, "paired_set": paired_set,
        "declined_stratum": _declined_block(stratum, len(result.declined)),
        "label_drift_stratum": _drift_block(result.drifted, drift_rows, wave.split),
        "gaming_flags": _gaming_block(result.rows, wave.arms),
        "gaming_flags_valid_unclean": _valid_unclean_gaming_block(result.rows, wave.arms),
        "excluded_stratum": _excluded_block(result.excluded, result.n_files, result.walls),
        "marginals": {"ai_marginal (search_ai - search_det)": _paired(by_sha, "search_ai", "search_det", blocks),
                      "deterministic_marginal (staged - input_only)": _paired(by_sha, "staged", "input_only", blocks)},
        "budgets": {a: {"wall_s": ev.ARMS[a].budget.wall_s, "ops": ev.ARMS[a].budget.ops,
                        "model_usd": ev.ARMS[a].budget.model_usd if ev.ARMS[a].spend_metered else None}
                    for a in wave.arms},
        "spend": {"model_usd_total": sum(b["model_usd"]["total"] for b in blocks.values() if b["spend_metered"]),
                  "unmetered_arms": [a for a, b in blocks.items() if not b["spend_metered"]],
                  "cap_usd": wave.cap_usd},
        "wall": {"shards": len(shard_wall), "slowest_shard_s": max(shard_wall, default=0.0),
                 "shard_seconds_total": sum(shard_wall)},
        "throttle": dict(result.backoff or {"status": "unrecorded"}),
        "cheap_full_agreement": _cheap_full_block(result.rows, wave.arms),
        "timeouts": timeouts,
        "warnings": warnings,
        "status": "OK" if not warnings else "UNRELIABLE",
        #: a script reading the JSON must not have to parse the banner to learn the numbers are not to be used
        "numbers_valid": not warnings,
        "not_reported_here": ["the re-probe's own cheap/full agreement (collect.py report['agreement'])",
                              "per-op or per-layer pass counts below the four in `layers`"],
    }


def _pct(x: float | None) -> str:
    return "withheld" if x is None else f"{x:.1%}"


def render_text(rep: dict[str, Any]) -> str:
    out = [f"# {rep['wave']} ({rep['split']}, {rep['wave_type']}) - {rep['status']}",
           f"commit {rep['commit']}  image {rep['image']}  files {rep['files']}"
           + ("  [PRIMARY ARTIFACT, frozen commit verified, once-only slot verified]" if rep["primary_artifact"]
              else "  [held-out split, once-only slot NOT verified: NOT the primary artifact]" if rep["heldout_split"]
              else "")]
    if rep["slot"]:
        out.append(f"once-only slot {rep['slot']}: "
                   + ("held by this wave_id in the marker store" if rep["slot_verified_against_store"]
                      else "named in the shards' provenance, marker store NOT consulted"))
    if rep["warnings"]:
        out += ["", "!!! DO NOT READ THE NUMBERS BELOW WITHOUT THESE !!!"] + [f"  - {w}" for w in rep["warnings"]]
    ps = rep["paired_set"]
    out += ["", f"HOW TO READ THE RATES: 'per-arm' columns use each arm's OWN conclusive files, and the dropouts "
                f"differ by arm (and tend to be the hard files), so per-arm rates are NOT comparable with each "
                f"other. Only the PAIRED columns (the {ps['n_files']} of {ps['of']} files every measured arm "
                f"judged conclusively; arms: {', '.join(ps['arms']) or 'none'}) support arm-to-arm comparison.",
            "",
            "| arm | measured | inconclusive (n) | inconclusive rate | PER-ARM clean rate (own files; not for comparison) "
            "| PER-ARM gain | PAIRED clean rate (shared files) | PAIRED gain | regress (c->u / fewer panel) | $ total | wall/file s |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a, b in rep["arms"].items():
        gain = "withheld" if b["clean_gain"] is None else f"{b['clean_gain']:+d}"
        pg = "withheld" if b["paired"]["clean_gain"] is None else f"{b['paired']['clean_gain']:+d}"
        reg = b["regressions"]
        regs = "withheld" if reg["clean_to_unclean"] is None else f"{reg['clean_to_unclean']} / {reg['fewer_panel_passes']}"
        usd = f"{b['model_usd']['total']:.2f}" if b["spend_metered"] else "not metered (calls a model; spend is v3's own)"
        out.append(f"| {a} | {'yes' if b['measured'] else 'NOT MEASURED'} | {b['inconclusive']}/{b['files']} "
                   f"| {b['inconclusive_rate']:.1%} | {_pct(b['clean_rate'])} | {gain} | "
                   f"{_pct(b['paired']['clean_rate'])} (n={b['paired']['n']}) | {pg} | {regs} | "
                   f"{usd} | {b['wall_s']['per_file_mean']:.1f} |")
    walled = {a: b.get("stopped_on_wall") or {} for a, b in rep["arms"].items()}
    if any(w.get("conclusive_rows") for w in walled.values()):
        out += ["", "WALL-STOPPED rows (B18's anytime rule: the search stopped on the clock, judged its best state "
                    "with the full panel and delivered it, so the row COUNTS above). Keep them out of a clean rate "
                    "by using the last column: "
                + "; ".join(f"{a} {w['conclusive_rows']}/{rep['arms'][a]['conclusive']} rows "
                            f"({w['clean']} clean; clean rate without them "
                            f"{_pct(w.get('clean_rate_without_them'))})"
                            for a, w in walled.items() if w.get("conclusive_rows"))]
    to = rep.get("timeouts") or {"arms": {}}
    out += ["", "repeated member timeouts (file failures under the D5 ruling; the rate moves with the machine): "
            + "; ".join(f"{a} {b['files']}/{b['conclusive']} files ({b['members']} members)" for a, b in to["arms"].items())]
    frac = "n/a" if ps["fraction"] is None else f"{ps['fraction']:.1%}"
    out += ["", f"PAIRED columns cover the {ps['n_files']} of {ps['of']} files judged conclusively by ALL of these "
                f"{len(ps['arms'])} measured arm(s): {', '.join(ps['arms']) or 'none'}"
            + (f"; left out because NOT MEASURED: {', '.join(ps['excluded_not_measured'])}"
               if ps["excluded_not_measured"] else "") + ".",
            f"paired fraction {frac} (minimum {ps['min_fraction']:.0%})"]
    out += ["", "## Marginals (paired, files both arms judged)"]
    for name, m in rep["marginals"].items():
        out.append(f"- {name}: " + (f"{m['value']:+.1%} ({m['files_gained']:+d} files over {m['n_paired']})"
                                    if m["value"] is not None else f"withheld - {m['reason']}"))
    cf = rep["cheap_full_agreement"]
    out += ["", "## Cheap/full agreement (search ranking tier vs full panel, over confirmed states)"]
    if "arms" not in cf:
        out.append(f"- not measured: {cf['reason']}")
    else:
        for name, g in [*cf["arms"].items(), ("pooled", cf["pooled"])]:
            out.append(f"- {name}: " + ("no confirmed state" if not g["compared"] else
                       f"{g['agree']:.1%} agree over {g['compared']} states; cheap pass but full fail "
                       f"{g['cheap_pass_full_fail']} (miss rate {_pct(g['cheap_miss_rate_among_cheap_passes'])}), "
                       f"cheap fail but full pass {g['cheap_fail_full_pass']}")
                       + (f"; {g['rows_without_data']} conclusive row(s) carried NO table"
                          if g.get("rows_without_data") else ""))
    g = rep["gaming_flags"]
    total = sum(b["files"] for b in g["arms"].values())
    files = len({x["sha256"] for b in g["arms"].values() for x in b["names"]})
    out += ["", f"## Gaming flags ({total} flagged clean row(s) over {files} file(s) that moved the part; volume "
                f"change over {g['limits']['volume_change']:.0%} or bbox shrink over "
                f"{g['limits']['bbox_shrink']:.0%}). {g['counted_as']}."]
    for a, b in g["arms"].items():
        if b["files"]:
            out += [f"- {a}: {b['files']} of {b['clean_reviewed']} clean"
                    + (f" ({b['unmeasured']} unmeasured)" if b["unmeasured"] else "")]
            out += [f"  - {x['name']}: {x['why']} ({_numbers(x)})" for x in b["names"]]
    gv = rep.get("gaming_flags_valid_unclean")
    if gv is not None:
        vtotal = sum(b["files"] for b in gv["arms"].values())
        out += ["", f"## Gaming flags on VALID-NOT-CLEAN delivered outputs ({vtotal} row(s); same limits). "
                    f"{gv['counted_as']}. NOT clean-rate gaming: these rows are in no clean rate.",
                f"Worked example: {gv['example']}."]
        for a, b in gv["arms"].items():
            if b["files"]:
                out += [f"- {a}: {b['files']} of {b['valid_unclean_reviewed']} valid-not-clean"
                        + (f" ({b['unmeasured']} unmeasured)" if b["unmeasured"] else "")]
                out += [f"  - {x['name']}: {_numbers(x)}" for x in b["names"]]
        if not vtotal:
            out += ["- none in this wave"]
    dr = rep.get("label_drift_stratum") or {}
    if dr.get("files"):
        out += ["", f"## Label drift ({dr['files']} file(s); {dr['counted_as']}). The split was labelled on older code; "
                    "these files are NOT failures, gains or regressions here."]
        for x in dr["names"]:
            arms = ", ".join(f"{a} {'clean' if v['out_clean'] else 'not clean'} ({v['out_panel_passes']}/"
                             f"{x.get('panel_size')})" + (" inconclusive" if v["inconclusive"] else "")
                             for a, v in x["arms"].items())
            out += [f"- {x['name']}: split says clean={x['split_clean']}, this commit's input verdict clean="
                    f"{x['input_clean']} (valid {x['input_valid']}, panel {x['input_panel_passes']}/{x['panel_size']}); "
                    f"{arms}"]
    ds = rep["declined_stratum"]
    if ds["files"]:
        out += ["", f"## Declined stratum ({ds['files']} files; expected outcome: {ds['expected_outcome']}). NOT a "
                    "failure and in NO clean rate, gain or regression above.",
                "| arm | files | declined (expected) | unexpectedly clean | inconclusive |", "|---|---|---|---|---|"]
        out += [f"| {a} | {v['files']} | {v['declined']} | {v['unexpectedly_clean']} | {v['inconclusive']} |"
                for a, v in ds["arms"].items()]
    xs = rep["excluded_stratum"]
    walls = _collect._wall_text(xs["walls"])
    reasons = ", ".join(f"{k} {n}" for k, n in sorted(xs["by_reason"].items())) or "none"
    out += ["", f"## Excluded stratum ({xs['files']} files, {xs['fraction_of_split']:.1%} of the split; walls "
                f"{walls}; by reason: {reasons}). {xs['counted_as']}. Named under excluded_stratum.names in the "
                "JSON."]
    for a, b in rep["arms"].items():
        out += ["", f"## {a}: by defect kind (per-arm, own files)",
                "| kind | files | inconclusive | inconclusive rate | clean | clean rate |", "|---|---|---|---|---|---|"]
        for k, v in b["by_defect_kind"].items():
            clean = "withheld" if v["clean"] is None else f"{v['clean']}/{v['conclusive']}"
            out.append(f"| {k} | {v['files']} | {v['inconclusive']} | {v['inconclusive'] / v['files']:.1%} | {clean} | {_pct(v['clean_rate'])} |")
    th = rep["throttle"]
    if th["status"] == "throttled":
        out += ["", f"!! THROTTLED WAVE: {len(th['trips'])} wave-level 429 back-off trip(s); shards spent "
                    f"{th['held_s_total']:.0f} s held (worst shard {th['held_s_max_shard']:.0f} s) across "
                    f"{th['shards_held']} shard(s). The wall-clock above includes that waiting: it is NOT the "
                    "engine's wall."]
    elif th["status"] == "unrecorded":
        out += ["", f"?? back-off NOT RECORDED ({th.get('shards_without_guard', '?')} shard(s) without the guard, "
                    f"{th.get('store_errors', 0)} marker-store error(s)): this wave may have been throttled; do not "
                    "treat its wall-clock as free-running."]
    elif th["status"] == "free":
        out += ["", "wave back-off: none (every shard ran the guard; no trip, no held call)"]
    unmetered = rep["spend"]["unmetered_arms"]
    out += ["", f"model spend ${rep['spend']['model_usd_total']:.2f} of the ${rep['spend']['cap_usd']:g} wave cap"
            + (f" (EXCLUDES {', '.join(unmetered)}: calls a model, spend not metered by this ledger)" if unmetered else "")
            + f"; slowest shard {rep['wall']['slowest_shard_s']:.0f} s"]
    return "\n".join(out) + "\n"


def _markers(path: str) -> Any:
    if not path:
        return None
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    return FileMarkerStore(path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--wave", required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--split", required=True, help="the split file (corpora/v4/<split>.json)")
    p.add_argument("--frozen-commit", default="")
    p.add_argument("--shard-plan", default="", help="the cost-balanced shard plan the wave was cut by (default: hash)")
    p.add_argument("--purpose", default="report", choices=("report", "tuning"))
    p.add_argument("--json", default="")
    p.add_argument("--previous-report", default="",
                   help="an earlier snapshot's report JSON: the reference for the repeated-timeout jump warning when "
                        "this wave has no input_only arm")
    p.add_argument("--markers-dir", default="",
                   help="a directory-backed marker store (model_ledger.FileMarkerStore) to verify the once-only "
                        "slot against; without it the slot is only read from the shards' provenance")
    args = p.parse_args(argv)
    try:
        w = ev.wave(args.wave)
        named = json.loads(Path(args.split).read_text()).get("split")
        if named != w.split:
            raise ReportRefused(f"{args.split} is the {named!r} split but wave {w.name} runs {w.split!r}")
        ev.refuse_heldout_for_tuning(*([args.split] if args.purpose == "tuning" else []))
        result = load_wave(Path(args.src), w, rp.read_split(args.split), n_shards=args.shards,
                           frozen_commit=args.frozen_commit or None, purpose=args.purpose,
                           shard_plan=ev.load_shard_plan(args.shard_plan) if args.shard_plan else None,
                           markers=_markers(args.markers_dir))
    except (ReportRefused, _collect.CollectRefused, ev.HeldOutInTuning, rp.DisallowedSource, ValueError) as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    rep = build_report(result, previous=json.loads(Path(args.previous_report).read_text()) if args.previous_report else None)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n")
    print(render_text(rep))
    return 0 if rep["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
