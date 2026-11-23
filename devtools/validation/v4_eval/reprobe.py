#!/usr/bin/env python3
# Responsibility: Re-measure the benchmark pool with the v4 checker and cut the dev / held-out / clean
#                 splits from the result. The old benchmark's metric (gmsh diag/40 yields tets) is not
#                 the definition any more: a file is failing when `verdict` says it is not clean.
# Owns: `ProbeRow`, `reprobe` (one tier over entries), `probe_pool` (cheap on all, full on cheap
#       failures plus a sample of passes), `agreement`, `build_splits` and the row/split JSON.
# Boundaries: every mesh goes through `verdict` / `cheap_verdict` and so through the verdict cache; this
#             module never meshes. An INCONCLUSIVE verdict (pool failure) or `CheckerUnavailable` is NOT a
#             finding about the file: it lands in `excluded`, never in a row, so it can be counted as
#             neither failing nor clean. Quarantined injector kinds are refused here (fail closed).
"""The re-probe and the splits.

    reprobe(entries, tier="cheap", pool=..., resolve=...)     # one tier
    probe_pool(entries, pool=..., resolve=...)                # the whole plan: cheap on all, then full
    build_splits(rows)                                        # stratified dev / held-out / clean

Order (design): the cheap tier (layer 1 + GmshDefault) runs on the whole pool; the full panel then runs
on the cheap-tier failures plus a deterministic 10% sample of the cheap passes, which measures how often
the cheap tier misses a failure (`agreement`). A cheap pass that was not sampled is "not known failing"
and is also not "known clean": only a full-tier pass enters the clean split.
"""
from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import random
import re
import signal
import sys
import types
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SPLITS_DIR = REPO / "devtools/validation/corpora/v4"
SEED = 20261122
PASS_SAMPLE_FRACTION = 0.10
Layer = Literal["valid", "panel", "faithful"]
Tier = Literal["cheap", "full"]
NO_SOLIDS = "no_solids"


# --------------------------------------------------------------------------- source allow-list

#: Sources the benchmark may measure. Fusion 360 ("f360") is HELD OUT: its licence (non-commercial
#: research, redistribution barred) has not been ruled on by the owner. It is a licence hold, not a
#: design decision: add "f360" here (and only here) when cleared. `reprobe` and `build_splits` both
#: refuse any entry or row outside this set, so the hold does not depend on a caller passing a flag.
ALLOWED_SOURCES: frozenset[str] = frozenset({"abc_fail", "abc_pass", "occt", "nist", "inject"})


class DisallowedSource(ValueError):
    """An entry or row from a source that is not in ALLOWED_SOURCES (licence hold)."""


def check_sources(items: Iterable[Any], allowed: Collection[str] | None = None) -> None:
    allowed = ALLOWED_SOURCES if allowed is None else allowed
    for item in items:
        if item.source not in allowed:
            raise DisallowedSource(
                f"{item.name}: source {item.source!r} is not in the allow-list {sorted(allowed)} "
                "(Fusion 360 is on licence hold until the owner rules)")


def split_allowed(entries: Iterable[Any], allowed: Collection[str] | None = None) -> tuple[list[Any], dict[str, int]]:
    """(kept, dropped-count-by-source): the EXPLICIT pool-level filter run_arm applies to a manifest
    that still lists held-out sources, so the drop is counted and recorded, never silent."""
    allowed = ALLOWED_SOURCES if allowed is None else allowed
    kept: list[Any] = []
    dropped: Counter[str] = Counter()
    for e in entries:
        if e.source in allowed:
            kept.append(e)
        else:
            dropped[e.source] += 1
    return kept, dict(dropped)


# --------------------------------------------------------------------------- injector quarantine

class _OcpStub(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Lets inject.py's module-level constants load where OCP is not installed (the hermetic unit tier).
    Only used when the real import fails; the fleet image has OCP and imports the real module."""

    def find_spec(self, name, path=None, target=None):  # noqa: ANN001, ANN201
        if name == "OCP" or name.startswith("OCP."):
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):  # noqa: ANN001, ANN201
        from unittest.mock import MagicMock

        mock = MagicMock(name=spec.name)
        mock.__path__ = []
        mock.__spec__ = spec
        return mock

    def exec_module(self, module) -> None:  # noqa: ANN001
        return None


_INJECT: types.ModuleType | None = None


def load_inject() -> types.ModuleType:
    global _INJECT
    if _INJECT is not None:
        return _INJECT
    path = HERE / "inject.py"

    def _load() -> types.ModuleType:
        spec = importlib.util.spec_from_file_location("v4_eval_inject", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["v4_eval_inject"] = module  # dataclasses resolve annotations through sys.modules
        spec.loader.exec_module(module)
        return module

    try:
        _INJECT = _load()
    except ImportError:
        stub = _OcpStub()
        sys.meta_path.insert(0, stub)
        try:
            _INJECT = _load()
        finally:
            sys.meta_path.remove(stub)
            for name in [n for n in sys.modules if n == "OCP" or n.startswith("OCP.")]:
                del sys.modules[name]
    return _INJECT


class QuarantinedKind(ValueError):
    """An injected entry whose damage kind is not in `inject.DEFAULT_KINDS` (quarantined, infeasible or
    unreadable). Including it would pad the failing set with files that are clean by our definition."""


def injected_kind(name: str) -> str | None:
    """The injector kind in an `inject()` output name `<stem>.<kind>.<seed>.step`, else None."""
    kinds = sorted(load_inject().KINDS, key=len, reverse=True)
    match = re.search(r"\.(" + "|".join(map(re.escape, kinds)) + r")\.\d+\.(?:step|stp)$", name)
    return match.group(1) if match else None


def not_default_kinds() -> list[str]:
    """Every injector kind that is NOT in DEFAULT_KINDS (quarantined or infeasible)."""
    inject = load_inject()
    return [k for k in inject.KINDS if k not in set(inject.DEFAULT_KINDS)]


def banned_kind_in(text: str) -> str | None:
    """A non-default injector kind named anywhere in `text`, delimited by non-alphanumerics."""
    for kind in not_default_kinds():
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(kind) + r"(?![A-Za-z0-9])", text):
            return kind
    return None


def declared_kinds(item: Any) -> list[str]:
    """Every injector kind an entry declares, by ANY route: a `kind` attribute, a `label` mapping's kind, and the
    kind in an `inject()` output name. None of them need the kind string to be in the name."""
    kinds: list[str] = []
    label = getattr(item, "label", None)
    for declared in (getattr(item, "kind", None), label.get("kind") if isinstance(label, dict) else None,
                     injected_kind(item.name)):
        if isinstance(declared, str) and declared not in kinds:
            kinds.append(declared)
    return kinds


def check_not_quarantined(items: Iterable[Any]) -> None:
    """Refuse the pool/rows if a quarantined kind can get through, whatever the source label.

    1. STATUS, for EVERY entry: each kind the entry declares (see `declared_kinds`) must be in `KIND_STATUS` with
       the allowed status (`structural`, i.e. `DEFAULT_KINDS`), and an entry carrying a `status` of its own
       (an attribute, or a label's `status`) must carry that one too. A quarantined entry whose name happens
       not to contain the kind string is caught here: those kinds read back as valid solids with a raised
       tolerance, so leaking them pads the failing set with files that are not broken.
    2. NAME/GROUP: no entry of ANY source may carry a non-default kind string in its name or group (a file
       renamed by a pool adapter).
    3. An `inject` entry must DECLARE its kind and status (`PoolEntry.kind` / `.kind_status`, from the injector's
       label) and the status must be the injector's own for that kind; a missing declaration is refused, never
       inferred from the name.
    `inject()` does not enforce the quarantine; this is where it is enforced."""
    inject = load_inject()
    allowed = set(inject.DEFAULT_KINDS)
    status_of: dict[str, str] = inject.KIND_STATUS
    for item in items:
        for kind in declared_kinds(item):
            status = status_of.get(kind)
            if status is None or kind not in allowed:
                raise QuarantinedKind(
                    f"{item.name} ({item.source}): injector kind {kind!r} has status {status!r}, not "
                    f"'structural' (allowed: {sorted(allowed)}); quarantined kinds stay out of the benchmark "
                    "until layer-2 evidence exists")
        label = getattr(item, "label", None)
        own = [getattr(item, "kind_status", None), label.get("status") if isinstance(label, dict) else None]
        for declared_status in own:
            if declared_status is not None and declared_status != "structural":
                raise QuarantinedKind(
                    f"{item.name} ({item.source}): the entry's own kind status is {declared_status!r}, not "
                    "'structural'; quarantined kinds stay out of the benchmark until layer-2 evidence exists")
        for text in (item.name, getattr(item, "group", None) or ""):
            banned = banned_kind_in(text)
            if banned is not None:
                raise QuarantinedKind(
                    f"{item.name} ({item.source}): carries non-default injector kind {banned!r}; "
                    "quarantined kinds stay out of the benchmark until layer-2 evidence exists")
        if item.source == "inject":
            # The injector's own declaration is REQUIRED: a missing kind or status is refused, never inferred
            # from the name (the name check above stays as a second layer, not the only one).
            declared_kind, declared = getattr(item, "kind", None), getattr(item, "kind_status", None)
            if declared_kind is None or declared is None:
                raise QuarantinedKind(
                    f"{item.name}: an inject entry must declare its injector kind and status (kind={declared_kind!r}, "
                    f"status={declared!r}); unknown damage is not proven damage, and quarantined kinds stay out "
                    "of the benchmark until layer-2 evidence exists")
            if status_of.get(declared_kind) != declared:
                raise QuarantinedKind(
                    f"{item.name}: declared status {declared!r} is not the injector's status for kind {declared_kind!r} "
                    f"({status_of.get(declared_kind)!r})")


def failure_family(message: str) -> str:
    return str(load_inject().failure_family(message))


# --------------------------------------------------------------------------- rows

@dataclass(frozen=True)
class ProbeRow:
    name: str
    sha256: str
    source: str
    verdict_summary: dict
    first_failing_layer: Layer | None
    family: str
    group: str | None = None  # variants of one part (NIST) share a group; splits keep them together
    kind: str | None = None  # `inject` rows: the injector's declared kind / status, carried from the pool entry
    kind_status: str | None = None


@dataclass(frozen=True)
class Excluded:
    """An entry the probe could not judge. NOT a failing file and NOT a clean one."""

    name: str
    sha256: str
    source: str
    reason: str


def is_no_solids(row: ProbeRow) -> bool:
    return row.family == NO_SOLIDS or NO_SOLIDS in row.verdict_summary.get("hard", [])


def is_counted(row: ProbeRow) -> bool:
    """Only rows a conclusive verdict produced exist, but the summary says so explicitly: a row written
    from an inconclusive verdict must never count."""
    return bool(row.verdict_summary.get("counts_for_scoreboard", False))  # fail closed: unknown is not counted


def summarise(v: Any) -> dict:
    return {
        "tier": v.tier,
        "clean": bool(v.clean),
        "counts_for_scoreboard": bool(v.counts_for_scoreboard),
        "valid": bool(v.valid.ok),
        "hard": [f.code for f in v.valid.hard],
        "soft": [f.code for f in v.valid.soft],
        "panel": [
            {"name": r.name, "status": r.status, "error_kind": getattr(r, "error_kind", None),
             "min_sicn": getattr(r, "min_sicn", None)}
            for r in v.panel
        ],
        "faithful": None if v.faithful is None else bool(v.faithful.ok),
    }


def first_failing_layer(v: Any) -> Layer | None:
    if not v.valid.ok:
        return "valid"
    if any(r.status != "pass" for r in v.panel):
        return "panel"
    if v.faithful is not None and not v.faithful.ok:
        return "faithful"
    return None


def family_of(v: Any, layer: Layer | None) -> str:
    if layer is None:
        return ""
    if layer == "valid":
        codes = [f.code for f in v.valid.hard]
        return NO_SOLIDS if NO_SOLIDS in codes else (codes[0] if codes else "valid_other")
    if layer == "faithful":
        return str(v.faithful.reason or "unfaithful") if v.faithful is not None else "unfaithful"
    failing = next(r for r in v.panel if r.status != "pass")
    family = failure_family(str(getattr(failing, "message", "") or ""))
    if family == "other" and failing.status in ("timeout", "crashed"):
        return "timeout_crash"
    return family


class FileTimeout(Exception):
    pass


class _WallClock:
    """SIGALRM deadline for one file's checker calls (main thread only; 0 disables). The pool kills a
    worker on its own per-call timeouts; this bounds the SUM of a file's calls."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._armed = False

    def _fire(self, signum, frame) -> None:  # noqa: ANN001
        raise FileTimeout(f"file wall clock {self.seconds:g}s exceeded")

    def __enter__(self) -> _WallClock:
        if self.seconds > 0:
            self._old = signal.signal(signal.SIGALRM, self._fire)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
            self._armed = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self._armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._old)


def reprobe(
    entries: Iterable[Any],
    *,
    tier: Tier,
    pool: Any,
    resolve: Callable[[Any], str],
    cache: Any = None,
    members: Sequence[Any] | None = None,
    run_panel: Callable[..., Sequence[Any]] | None = None,
    file_timeout_s: float = 0.0,
    excluded: list[Excluded] | None = None,
    on_row: Callable[[ProbeRow], None] | None = None,
) -> list[ProbeRow]:
    """Run `tier` of the checker on each entry's own file (the input is its own "original") and return
    one ProbeRow per CONCLUSIVE verdict.

    Not returned, but appended to `excluded` when given: a `CheckerUnavailable` (layer 1/3 could not be
    measured), an inconclusive panel (pool failure), an unreadable path and a file wall-clock timeout.
    None of these says anything about the CAD, so none may count as failing, unclean or clean.
    """
    from meshpipeline.cad.repair_v4.verdict import cheap_verdict, verdict
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    entries = list(entries)
    check_sources(entries)
    check_not_quarantined(entries)  # before any mesh: a quarantined kind must cost nothing
    check = cheap_verdict if tier == "cheap" else verdict
    rows: list[ProbeRow] = []

    def skip(entry: Any, reason: str) -> None:
        if excluded is not None:
            excluded.append(Excluded(entry.name, entry.sha256, entry.source, reason))

    for entry in entries:
        try:
            step = resolve(entry)
            with _WallClock(file_timeout_s):
                v = check(pool, step, step, cache=cache, members=members, run_panel=run_panel)
        except FileNotFoundError as exc:
            skip(entry, f"input_missing: {exc}")
            continue
        except FileTimeout as exc:
            skip(entry, f"file_timeout: {exc}")
            continue
        except CheckerUnavailable as exc:
            skip(entry, f"checker_unavailable: {exc}")
            continue
        if not v.counts_for_scoreboard:
            skip(entry, "inconclusive: a panel member failed for pool reasons")
            continue
        if not v.panel:
            skip(entry, "empty_panel: no member produced a result")
            continue
        layer = first_failing_layer(v)
        row = ProbeRow(entry.name, entry.sha256, entry.source, summarise(v), layer,
                       family_of(v, layer), getattr(entry, "group", None),
                       getattr(entry, "kind", None), getattr(entry, "kind_status", None))
        rows.append(row)
        if on_row is not None:
            on_row(row)
    return rows


# --------------------------------------------------------------------------- the whole plan

def in_pass_sample(sha256: str, *, seed: int = SEED, fraction: float = PASS_SAMPLE_FRACTION) -> bool:
    """A stable 10% of the cheap passes, chosen by hash so a rerun or a different shard picks the same."""
    digest = hashlib.sha256(f"{seed}:{sha256}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < fraction


@dataclass
class ProbeResult:
    cheap: list[ProbeRow]
    full: list[ProbeRow]
    excluded: list[Excluded] = field(default_factory=list)

    def final_rows(self) -> list[ProbeRow]:
        """One row per sha: the full-tier row where one exists (it is the stronger measurement)."""
        by_sha = {r.sha256: r for r in self.cheap}
        by_sha.update({r.sha256: r for r in self.full})
        return list(by_sha.values())


def full_tier_entries(entries: Sequence[Any], cheap_rows: Sequence[ProbeRow], *, seed: int = SEED) -> list[Any]:
    """Entries the full panel runs on: cheap-tier failures (no_solids is not repairable, so it is not
    paid for twice) plus the hash-sampled 10% of cheap passes."""
    verdict_of = {r.sha256: r for r in cheap_rows}
    chosen = []
    for e in entries:
        row = verdict_of.get(e.sha256)
        if row is None or (row.first_failing_layer is not None and is_no_solids(row)):
            continue
        if row.first_failing_layer is not None or in_pass_sample(e.sha256, seed=seed):
            chosen.append(e)
    return chosen


def probe_pool(entries: Sequence[Any], *, seed: int = SEED, **kw: Any) -> ProbeResult:
    excluded: list[Excluded] = []
    cheap = reprobe(entries, tier="cheap", excluded=excluded, **kw)
    full = reprobe(full_tier_entries(entries, cheap, seed=seed), tier="full", excluded=excluded, **kw)
    return ProbeResult(cheap, full, excluded)


def agreement(cheap: Sequence[ProbeRow], full: Sequence[ProbeRow]) -> dict:
    """Cheap vs full over the files that have both. `cheap_miss` is the cheap tier's blind spot: it
    passed a file the full panel fails (the number that says how far a cheap pass can be trusted)."""
    cheap_by = {r.sha256: r for r in cheap}
    cells: Counter[str] = Counter()
    for f in full:
        c = cheap_by.get(f.sha256)
        if c is None:
            continue
        cheap_fail, full_fail = c.first_failing_layer is not None, f.first_failing_layer is not None
        cells[("fail" if cheap_fail else "pass") + "/" + ("fail" if full_fail else "pass")] += 1
    both = sum(cells.values())
    passes = cells["pass/pass"] + cells["pass/fail"]
    fails = cells["fail/fail"] + cells["fail/pass"]
    return {
        "compared": both,
        "cheap_pass_full_pass": cells["pass/pass"], "cheap_pass_full_fail": cells["pass/fail"],
        "cheap_fail_full_fail": cells["fail/fail"], "cheap_fail_full_pass": cells["fail/pass"],
        "agree": (cells["pass/pass"] + cells["fail/fail"]) / both if both else None,
        "cheap_miss_rate_among_cheap_passes": cells["pass/fail"] / passes if passes else None,
        "cheap_false_alarm_rate_among_cheap_fails": cells["fail/pass"] / fails if fails else None,
    }


# --------------------------------------------------------------------------- splits

@dataclass(frozen=True)
class Splits:
    dev: list[ProbeRow]
    heldout: list[ProbeRow]
    clean: list[ProbeRow]
    seed: int
    excluded_no_solids: int = 0
    excluded_uncounted: int = 0
    shortfall: dict[str, int] = field(default_factory=dict)
    strata: dict[str, dict[str, int]] = field(default_factory=dict)


def _stratum(row: ProbeRow) -> tuple[str, str, str]:
    return (str(row.first_failing_layer), row.family, row.source)


def _apportion(sizes: dict[Any, int], total: int) -> dict[Any, int]:
    """Largest-remainder shares of `total` over strata in proportion to `sizes`, never above a stratum's
    size. Deterministic: ties break on the stratum key."""
    pop = sum(sizes.values())
    if total > pop:
        raise ValueError(f"asked for {total} from a population of {pop}")
    quota = dict.fromkeys(sizes, 0)
    if total == 0:
        return quota
    exact = {k: total * n / pop for k, n in sizes.items()}
    quota = {k: min(sizes[k], int(exact[k])) for k in sizes}
    order = sorted(sizes, key=lambda k: (-(exact[k] - int(exact[k])), str(k)))
    while sum(quota.values()) < total:
        progressed = False
        for k in order:
            if sum(quota.values()) >= total:
                break
            if quota[k] < sizes[k]:
                quota[k] += 1
                progressed = True
        if not progressed:
            break
    return quota


def _shuffled(rows: list[ProbeRow], key: str, seed: int) -> list[ProbeRow]:
    ordered = sorted(rows, key=lambda r: r.sha256)  # input order must not matter
    random.Random(f"{seed}:{key}").shuffle(ordered)
    return ordered


def build_splits(
    rows: Iterable[ProbeRow], *, dev: int = 300, heldout: int = 200, clean: int = 150, seed: int = SEED,
    out_dir: Path | str | None = None, cheap_failed: Collection[str] = (),
) -> Splits:
    """Stratified dev / held-out / clean splits.

    Failing set: rows with a first failing layer, minus `no_solids` (not a repairable defect), minus
    anything a conclusive verdict did not produce. Dev and held-out are drawn from it stratified by
    (first_failing_layer, family, source): each stratum contributes in proportion to its size
    (largest remainder), and within a stratum a seeded shuffle of sha256-sorted rows gives dev first and
    held-out from what is left, so the two are disjoint by construction. Variants of one part (a shared
    `group`) never straddle dev and held-out: held-out skips any group dev already holds.
    Clean: full-tier passes only (a cheap pass is not certified clean), stratified by source. Files the
    cheap tier FAILED and the full tier passed (`cheap_failed`: the marginal, possibly flaky ones) are
    used only to top up when the genuine cheap-pass population is too small; the count drawn from them
    is reported as `clean_from_cheap_fail` so the bias is visible.
    Raises ValueError when the failing set has fewer than dev + heldout files rather than shrinking a
    split silently; a group conflict that leaves a split short is reported in `shortfall`.
    Writes dev.json / heldout.json / clean.json under `out_dir` when given.
    """
    rows = list({r.sha256: r for r in sorted(rows, key=lambda r: r.sha256)}.values())
    check_sources(rows)
    check_not_quarantined(rows)
    uncounted = [r for r in rows if not is_counted(r)]
    rows = [r for r in rows if is_counted(r)]
    no_solids = [r for r in rows if r.first_failing_layer is not None and is_no_solids(r)]
    failing = [r for r in rows if r.first_failing_layer is not None and not is_no_solids(r)]
    certified = [r for r in rows if r.first_failing_layer is None and r.verdict_summary.get("clean")
                 and r.verdict_summary.get("tier") == "full"]

    by_stratum: dict[tuple[str, str, str], list[ProbeRow]] = defaultdict(list)
    for r in failing:
        by_stratum[_stratum(r)].append(r)
    sizes = {k: len(v) for k, v in by_stratum.items()}
    if dev + heldout > len(failing):
        raise ValueError(f"failing set has {len(failing)} files, need dev {dev} + heldout {heldout}")
    totals = _apportion(sizes, dev + heldout)
    dev_quota = _apportion(totals, dev)
    ordered = {k: _shuffled(v, "/".join(k), seed) for k, v in by_stratum.items()}

    dev_rows: list[ProbeRow] = []
    taken: set[str] = set()
    for k in sorted(ordered):
        picked = ordered[k][: dev_quota[k]]
        dev_rows += picked
        taken.update(r.sha256 for r in picked)
    dev_groups = {r.group for r in dev_rows if r.group}
    held_rows: list[ProbeRow] = []
    for k in sorted(ordered):
        want = totals[k] - dev_quota[k]
        rest = [r for r in ordered[k] if r.sha256 not in taken and r.group not in dev_groups]
        held_rows += rest[:want]
    shortfall = {}
    if len(held_rows) < heldout:
        shortfall["heldout"] = heldout - len(held_rows)

    primary = [r for r in certified if r.sha256 not in cheap_failed]
    marginal = [r for r in certified if r.sha256 in cheap_failed]

    def draw(pop: list[ProbeRow], n: int) -> list[ProbeRow]:
        by_source: dict[str, list[ProbeRow]] = defaultdict(list)
        for r in pop:
            by_source[r.source].append(r)
        quota = _apportion({k: len(v) for k, v in by_source.items()}, n)
        return [r for k in sorted(by_source) for r in _shuffled(by_source[k], k, seed)[: quota[k]]]

    clean_rows = draw(primary, min(clean, len(primary)))
    topped = draw(marginal, min(clean - len(clean_rows), len(marginal)))
    clean_rows += topped
    if len(clean_rows) < clean:
        shortfall["clean"] = clean - len(clean_rows)

    result = Splits(
        dev_rows, held_rows, clean_rows, seed, len(no_solids), len(uncounted), shortfall,
        {"failing_population": {"/".join(k): n for k, n in sorted(sizes.items())},
         "dev": dict(Counter("/".join(_stratum(r)) for r in dev_rows)),
         "heldout": dict(Counter("/".join(_stratum(r)) for r in held_rows)),
         "clean": dict(Counter(r.source for r in clean_rows)),
         "clean_from_cheap_fail": {"count": len(topped)}},
    )
    assert_disjoint(result)
    if out_dir is not None:
        write_splits(result, out_dir)
    return result


def assert_disjoint(splits: Splits) -> None:
    """Held-out is never in dev (by sha256 or by part group), and clean is in neither."""
    sets = {n: {r.sha256 for r in getattr(splits, n)} for n in ("dev", "heldout", "clean")}
    for a, b in (("dev", "heldout"), ("dev", "clean"), ("heldout", "clean")):
        both = sets[a] & sets[b]
        if both:
            raise AssertionError(f"{a} and {b} share {len(both)} file(s), e.g. {sorted(both)[0]}")
    groups = {n: {r.group for r in getattr(splits, n) if r.group} for n in ("dev", "heldout")}
    if groups["dev"] & groups["heldout"]:
        raise AssertionError(f"dev and heldout share part group(s) {sorted(groups['dev'] & groups['heldout'])[:3]}")


def write_splits(splits: Splits, out_dir: Path | str = SPLITS_DIR) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"seed": splits.seed, "strata": splits.strata, "shortfall": splits.shortfall,
            "excluded_no_solids": splits.excluded_no_solids, "excluded_uncounted": splits.excluded_uncounted}
    for name in ("dev", "heldout", "clean"):
        body = {"split": name, **meta, "files": [asdict(r) for r in getattr(splits, name)]}
        (out / f"{name}.json").write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")


def read_split(path: Path | str) -> list[ProbeRow]:
    return [ProbeRow(**item) for item in json.loads(Path(path).read_text())["files"]]


def write_rows(rows: Iterable[ProbeRow], path: Path | str) -> None:
    with Path(path).open("w", encoding="utf-8") as handle:
        for r in rows:
            handle.write(json.dumps(asdict(r), sort_keys=True) + "\n")


def read_rows(path: Path | str) -> list[ProbeRow]:
    return [ProbeRow(**json.loads(line)) for line in Path(path).read_text().splitlines() if line.strip()]
