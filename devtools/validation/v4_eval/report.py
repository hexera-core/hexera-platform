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
the held-out split without the frozen commit, and the held-out split for a tuning purpose.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
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
#: Fewer conclusive files than this and the constant-outcome warning is not raised (too few to call).
MIN_FILES_FOR_CONSTANT_WARNING = 10
SPEND_EPS_USD = 1e-6
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

    @property
    def clean(self) -> bool:
        """The verdict's definition, derived: valid AND faithful.ok AND the whole panel passes."""
        return bool(self.out_valid and self.out_faithful_ok
                    and self.out_panel_passes is not None and self.out_panel_passes == self.panel_size)

    @property
    def counts(self) -> bool:
        return not self.inconclusive


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
    if row.inconclusive:
        return row  # carries no verdict; the (never-used) fields are not read
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
              purpose: str = "report") -> WaveResult:
    """Every gate, then the rows. `split_rows` are the split file's ProbeRows (rp.read_split)."""
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
            if a == "input_only" and (r.out_clean != r.in_clean or r.out_panel_passes != r.in_panel_passes):
                raise ReportRefused(f"{r.name} [input_only]: the output differs from the input "
                                    f"(clean {r.in_clean}->{r.out_clean}, panel {r.in_panel_passes}->"
                                    f"{r.out_panel_passes}): input_only did not pass the input through")
        seen = {(rows[(sha, a)].in_clean, rows[(sha, a)].in_panel_passes) for a in wave.arms
                if not rows[(sha, a)].inconclusive}
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
        tuple(float(s["prov"].get("wall_seconds") or 0.0) for s in shards), bool(frozen_commit), _TOKEN)


# ---------------------------------------------------------------------------------------------- the report

def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def _arm_block(rows: list[ArmRow], kinds: Mapping[str, str], n_files: int) -> dict[str, Any]:
    conclusive = [r for r in rows if r.counts]
    inc = n_files - len(conclusive)
    inc_rate = inc / n_files
    measured = inc_rate <= MAX_INCONCLUSIVE_FRACTION and bool(conclusive)
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
        ok = bool(con) and kind_inc / len(rs) <= MAX_INCONCLUSIVE_FRACTION
        by_kind[kind] = {"files": len(rs), "conclusive": len(con), "inconclusive": kind_inc,
                         "clean": sum(r.clean for r in con),
                         "clean_rate": _rate(sum(r.clean for r in con), len(con)) if ok else None,
                         "measured": ok}
    return {
        "files": n_files, "conclusive": len(conclusive), "inconclusive": inc, "inconclusive_rate": inc_rate,
        "measured": measured,
        "clean_in": clean_in, "clean_out": clean_out,
        "clean_rate": _rate(clean_out, len(conclusive)) if measured else None,
        "clean_gain": clean_out - clean_in if measured else None,
        "clean_gain_rate": _rate(clean_out - clean_in, len(conclusive)) if measured else None,
        "regressions": {"clean_to_unclean": c2u, "fewer_panel_passes": fewer, "either": either},
        "layers": {"valid": sum(bool(r.out_valid) for r in conclusive),
                   "faithful_ok": sum(bool(r.out_faithful_ok) for r in conclusive),
                   "panel_all_pass": sum(r.out_panel_passes == r.panel_size for r in conclusive),
                   "clean": clean_out},
        "by_defect_kind": by_kind,
        "model_usd": {"total": sum(r.model_usd for r in rows), "per_file_mean": sum(r.model_usd for r in rows) / n_files},
        "wall_s": {"total": sum(r.wall_s for r in rows), "per_file_mean": sum(r.wall_s for r in rows) / n_files,
                   "max": max(r.wall_s for r in rows)},
    }


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
    for r in result.rows:
        per_arm[r.arm].append(r)
        by_sha[r.sha256][r.arm] = r
    blocks = {a: _arm_block(rs, result.kinds, result.n_files) for a, rs in per_arm.items()}
    warnings: list[str] = []
    for a, b in blocks.items():
        if not b["measured"]:
            warnings.append(f"{a}: NOT MEASURED - {b['inconclusive']} of {b['files']} files inconclusive "
                            f"({b['inconclusive_rate']:.0%}; limit {MAX_INCONCLUSIVE_FRACTION:.0%}); its clean rate is withheld")
        elif b["conclusive"] >= MIN_FILES_FOR_CONSTANT_WARNING and a != "input_only" \
                and b["clean_out"] in (0, b["conclusive"]):
            warnings.append(f"{a}: every conclusive file has the same outcome ({b['clean_out']}/{b['conclusive']} clean): "
                            "a constant verdict looks like a broken harness, not a result")
    names = list(wave.arms)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = [s for s, by in by_sha.items() if by[a].counts and by[b].counts]
            if len(both) >= MIN_FILES_FOR_CONSTANT_WARNING and all(
                    (by_sha[s][a].clean, by_sha[s][a].out_panel_passes) == (by_sha[s][b].clean, by_sha[s][b].out_panel_passes)
                    for s in both):
                warnings.append(f"{a} and {b} are indistinguishable on all {len(both)} files they share: "
                                "check that the harness varies the arm")
    shard_wall = list(result.shard_wall_seconds)
    return {
        "wave": wave.name, "wave_id": result.wave_id, "wave_type": wave.wave_type, "split": wave.split,
        "commit": result.commit, "image": result.image, "frozen_commit_checked": result.frozen,
        "primary_artifact": wave.split == ev.HELDOUT,
        "files": result.n_files, "arms": blocks,
        "marginals": {"ai_marginal (search_ai - search_det)": _paired(by_sha, "search_ai", "search_det", blocks),
                      "deterministic_marginal (staged - input_only)": _paired(by_sha, "staged", "input_only", blocks)},
        "budgets": {a: {"wall_s": ev.ARMS[a].budget.wall_s, "ops": ev.ARMS[a].budget.ops,
                        "model_usd": ev.ARMS[a].budget.model_usd} for a in wave.arms},
        "spend": {"model_usd_total": sum(b["model_usd"]["total"] for b in blocks.values()),
                  "cap_usd": wave.cap_usd},
        "wall": {"shards": len(shard_wall), "slowest_shard_s": max(shard_wall, default=0.0),
                 "shard_seconds_total": sum(shard_wall)},
        "warnings": warnings,
        "status": "OK" if not warnings else "UNRELIABLE",
        "not_reported_here": ["cheap/full agreement (measured by the re-probe: collect.py report['agreement'])",
                              "per-op or per-layer pass counts below the four in `layers`"],
    }


def _pct(x: float | None) -> str:
    return "withheld" if x is None else f"{x:.1%}"


def render_text(rep: dict[str, Any]) -> str:
    out = [f"# {rep['wave']} ({rep['split']}, {rep['wave_type']}) - {rep['status']}",
           f"commit {rep['commit']}  image {rep['image']}  files {rep['files']}"
           + ("  [PRIMARY ARTIFACT, frozen commit verified]" if rep["primary_artifact"] else "")]
    if rep["warnings"]:
        out += ["", "!!! DO NOT READ THE NUMBERS BELOW WITHOUT THESE !!!"] + [f"  - {w}" for w in rep["warnings"]]
    out += ["", "| arm | measured | inconclusive (n) | inconclusive rate | clean rate | gain | regress (c->u / fewer panel) | $ total | wall/file s |",
            "|---|---|---|---|---|---|---|---|---|"]
    for a, b in rep["arms"].items():
        gain = "withheld" if b["clean_gain"] is None else f"{b['clean_gain']:+d}"
        out.append(f"| {a} | {'yes' if b['measured'] else 'NOT MEASURED'} | {b['inconclusive']}/{b['files']} "
                   f"| {b['inconclusive_rate']:.1%} | {_pct(b['clean_rate'])} | {gain} | "
                   f"{b['regressions']['clean_to_unclean']} / {b['regressions']['fewer_panel_passes']} | "
                   f"{b['model_usd']['total']:.2f} | {b['wall_s']['per_file_mean']:.1f} |")
    out += ["", "## Marginals (paired, files both arms judged)"]
    for name, m in rep["marginals"].items():
        out.append(f"- {name}: " + (f"{m['value']:+.1%} ({m['files_gained']:+d} files over {m['n_paired']})"
                                    if m["value"] is not None else f"withheld - {m['reason']}"))
    for a, b in rep["arms"].items():
        out += ["", f"## {a}: by defect kind", "| kind | files | inconclusive | inconclusive rate | clean | clean rate |",
                "|---|---|---|---|---|---|"]
        for k, v in b["by_defect_kind"].items():
            out.append(f"| {k} | {v['files']} | {v['inconclusive']} | {v['inconclusive'] / v['files']:.1%} | {v['clean']}/{v['conclusive']} | {_pct(v['clean_rate'])} |")
    out += ["", f"model spend ${rep['spend']['model_usd_total']:.2f} of the ${rep['spend']['cap_usd']:g} wave cap; "
            f"slowest shard {rep['wall']['slowest_shard_s']:.0f} s"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--wave", required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--split", required=True, help="the split file (corpora/v4/<split>.json)")
    p.add_argument("--frozen-commit", default="")
    p.add_argument("--purpose", default="report", choices=("report", "tuning"))
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    try:
        w = ev.wave(args.wave)
        named = json.loads(Path(args.split).read_text()).get("split")
        if named != w.split:
            raise ReportRefused(f"{args.split} is the {named!r} split but wave {w.name} runs {w.split!r}")
        ev.refuse_heldout_for_tuning(*([args.split] if args.purpose == "tuning" else []))
        result = load_wave(Path(args.src), w, rp.read_split(args.split), n_shards=args.shards,
                           frozen_commit=args.frozen_commit or None, purpose=args.purpose)
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
