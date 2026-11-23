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
seconds. Clean is DERIVED here (`valid AND faithful_ok AND every panel member passes`, the verdict's
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
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
    #: search arms only: cheap tier vs full panel over the states the search CONFIRMED (`arms.cheap_full_cells`),
    #: the four cells named as `reprobe.agreement` names them. Optional: a row without it is counted as missing data
    #: in `cheap_full_agreement`, never as agreement.
    cheap_full: dict | None = None

    @property
    def clean(self) -> bool:
        """The verdict's definition, derived: valid AND faithful.ok AND the whole panel passes."""
        return bool(self.out_valid and self.out_faithful_ok
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
                               verdict_candidates=self.verdict_candidates, out_clean=self.out_clean)


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
    slot: str | None = None  # the once-only slot the shards carry (held-out / checkpoint waves)
    slot_verified: bool = False  # ... and whether the marker store confirmed this wave_id holds it
    _key: object = None

    def __post_init__(self) -> None:
        if self._key is not _TOKEN:
            raise TypeError("a WaveResult is built by load_wave, which applies every refusal collect.py makes")


def defect_kind(split_row: Any) -> str:
    return split_row.family or str(split_row.first_failing_layer or "clean_control")


def _same(shards: list[dict], key: str, want: Any, what: str) -> None:
    got = {json.dumps(s["prov"].get(key), sort_keys=True) for s in shards}
    if got != {json.dumps(want, sort_keys=True)}:
        raise ReportRefused(f"shards disagree with the wave on {what}: shards say {sorted(got)}, the wave "
                            f"definition says {json.dumps(want)}")


def load_wave(root: Path, wave: Any, split_rows: Iterable[Any], *, n_shards: int, frozen_commit: str | None = None,
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
    _collect.check_partition(shards)
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
                raise ReportRefused(f"{r.name} [{a}]: the arm saw an input with clean={r.in_clean} but the "
                                    f"{wave.split} split says clean={truth}")
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

    total = sum(r.model_usd for r in rows.values())
    if total > wave.cap_usd + SPEND_EPS_USD:
        raise ReportRefused(f"wave spend ${total:.2f} exceeds the ${wave.cap_usd:g} {wave.wave_type} cap")

    first = shards[0]["prov"]
    return WaveResult(
        wave, next(iter(ids)), str(first["commit"]), str(first["image"]), tuple(rows.values()),
        {sha: defect_kind(r) for sha, r in by_sha.items()}, len(split),
        tuple(float(s["prov"].get("wall_seconds") or 0.0) for s in shards), bool(frozen_commit),
        declined=declined, slot=slot, slot_verified=slot_verified, _key=_TOKEN)


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
    return {
        "files": n_files, "conclusive": len(conclusive), "inconclusive": inc, "inconclusive_rate": inc_rate,
        "measured": measured,
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


def build_report(result: WaveResult) -> dict[str, Any]:
    if not isinstance(result, WaveResult):
        raise TypeError("build_report takes a WaveResult from load_wave, never raw rows")
    wave = result.wave
    per_arm: dict[str, list[ArmRow]] = {a: [] for a in wave.arms}
    by_sha: dict[str, dict[str, ArmRow]] = defaultdict(dict)
    stratum: dict[str, list[ArmRow]] = {a: [] for a in wave.arms}
    for r in result.rows:
        if r.sha256 in result.declined:  # the third category: kept out of EVERY figure below, reported on its own
            stratum[r.arm].append(r)
            continue
        per_arm[r.arm].append(r)
        by_sha[r.sha256][r.arm] = r
    n_main = result.n_files - len(result.declined)
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
        "cheap_full_agreement": _cheap_full_block(result.rows, wave.arms),
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
    ds = rep["declined_stratum"]
    if ds["files"]:
        out += ["", f"## Declined stratum ({ds['files']} files; expected outcome: {ds['expected_outcome']}). NOT a "
                    "failure and in NO clean rate, gain or regression above.",
                "| arm | files | declined (expected) | unexpectedly clean | inconclusive |", "|---|---|---|---|---|"]
        out += [f"| {a} | {v['files']} | {v['declined']} | {v['unexpectedly_clean']} | {v['inconclusive']} |"
                for a, v in ds["arms"].items()]
    for a, b in rep["arms"].items():
        out += ["", f"## {a}: by defect kind (per-arm, own files)",
                "| kind | files | inconclusive | inconclusive rate | clean | clean rate |", "|---|---|---|---|---|---|"]
        for k, v in b["by_defect_kind"].items():
            clean = "withheld" if v["clean"] is None else f"{v['clean']}/{v['conclusive']}"
            out.append(f"| {k} | {v['files']} | {v['inconclusive']} | {v['inconclusive'] / v['files']:.1%} | {clean} | {_pct(v['clean_rate'])} |")
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
    p.add_argument("--purpose", default="report", choices=("report", "tuning"))
    p.add_argument("--json", default="")
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
                           markers=_markers(args.markers_dir))
    except (ReportRefused, _collect.CollectRefused, ev.HeldOutInTuning, rp.DisallowedSource, ValueError) as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    rep = build_report(result)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n")
    print(render_text(rep))
    return 0 if rep["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
