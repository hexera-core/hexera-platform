"""Experiment 2: can a calibrated kept-probability skip doomed attempts and order the rest?

The sweep already measured, for every sampled defect, what the rules-chosen operation did and how
long the attempt took. That makes two policies evaluable offline, with no new kernel runs:

  skip    predict P(kept) for the rules operation; skip the attempt when the calibrated
          probability is under a threshold chosen ON TRAINING FAMILIES ONLY, at a target keep
          recall. Report attempts skipped, keeps lost and attempt-seconds saved on the held-out
          family.
  order   per file, attempt defects in descending predicted P(kept) instead of scan order, under
          the real budgets (6 operations, 120 s of measured attempt time). Report keeps achieved
          within budget.

HONESTY LIMIT: the replay treats attempts as independent — in a real run a kept operation changes
the shape, which can create or destroy later candidates. The sweep's snapshot design measures
exactly the independent quantity, so the replay is a faithful simulation of the FIRST round of a
run, not of a whole session.

Usage:
    .venv/bin/python -m labs.defect_cad_rag.policy /tmp/dcr-sweep/records.jsonl \
        /tmp/dcr-sweep-dg/records.jsonl --precapped /tmp/dcr-sweep/precapped.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from labs.defect_cad_rag.knn import JUDGED, _knn_scores, _prior, _standardiser

#: The run budgets the replay enforces, from settings/cad_repair.py defaults.
MAX_OPS = 6
TIMEOUT_S = 120.0

#: The threshold is the largest that keeps this fraction of training keeps.
TARGET_KEEP_RECALL = 0.95

CALIBRATION_BINS = 10


@dataclass(frozen=True, slots=True)
class R:
    """One judged rules-op attempt. Duck-types knn.Row for `_knn_scores`."""

    family: str
    file: str
    code: str
    vector: tuple[float, ...]
    labels: dict
    rules_op: str
    kept: int
    seconds: float
    order: int  # the defect's position in the file's scan order


def load(paths: list[Path], *, exclude: frozenset[str] = frozenset()) -> list[R]:
    rows: list[R] = []
    for path in paths:
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if r.get("kind") != "defect" or not r.get("vector") or r["file"] in exclude:
                continue
            judged = {a["op"]: a for a in r["attempts"] if a["status"] in JUDGED}
            a = judged.get(r.get("rules_op", ""))
            if a is None:
                continue
            rows.append(R(r["family"], r["file"], r["code"], tuple(r["vector"]),
                          {op: JUDGED[x["status"]] for op, x in judged.items()},
                          r["rules_op"], JUDGED[a["status"]], float(a.get("seconds", 0.0)),
                          int(r["defect_index"])))
    return rows


def _score(query: R, train: list[R], z, prior: dict) -> float:
    return _knn_scores(query, train, z, k=10, prior=prior)[query.rules_op]


def _crossfit(train: list[R], folds: int = 5) -> list[tuple[float, int]]:
    """(score, kept) for every training row, scored by a model that never saw its family."""
    families = sorted({r.family for r in train})
    fold_of = {f: i % folds for i, f in enumerate(families)}
    out: list[tuple[float, int]] = []
    for fold in range(folds):
        fit = [r for r in train if fold_of[r.family] != fold]
        held = [r for r in train if fold_of[r.family] == fold]
        if not fit or not held:
            continue
        z, prior = _standardiser(fit), _prior(fit)
        out += [(_score(r, fit, z, prior), r.kept) for r in held]
    return out


def _calibrator(scored: list[tuple[float, int]]):
    """Fixed-width bins over the raw score, Laplace-smoothed kept rate per bin."""
    bins = [[0, 0] for _ in range(CALIBRATION_BINS)]
    for s, y in scored:
        b = min(CALIBRATION_BINS - 1, int(s * CALIBRATION_BINS))
        bins[b][0] += y
        bins[b][1] += 1

    def cal(s: float) -> float:
        b = min(CALIBRATION_BINS - 1, int(s * CALIBRATION_BINS))
        k, n = bins[b]
        return (k + 1) / (n + 2)
    return cal


def _threshold(scored: list[tuple[float, int]]) -> float:
    """Largest raw-score cutoff keeping TARGET_KEEP_RECALL of the training keeps above it.

    ON RAW SCORES, NOT CALIBRATED ONES. The calibrator bins, and a bin holding both keeps and
    hopeless attempts gives them one identical probability - a strict cutoff then protects the
    whole bin and nothing is ever skipped (measured: 0 skips at recall 0.95). The raw kNN score
    is continuous, so the recall constraint can actually bind."""
    kept = sorted(s for s, y in scored if y == 1)
    if not kept:
        return 0.0  # no keeps to protect; nothing is known safe to skip either
    allowed_losses = int((1 - TARGET_KEEP_RECALL) * len(kept))
    return kept[allowed_losses]


def _replay(rows: list[R], probs: dict[int, float]) -> dict:
    """Keeps achieved within the real budgets, scan order vs predicted order vs skip-ordered."""
    by_file: dict = defaultdict(list)
    for i, r in enumerate(rows):
        by_file[r.file].append((i, r))

    def run(ordered: list[tuple[int, R]]) -> int:
        keeps, ops, spent = 0, 0, 0.0
        for _, r in ordered:
            if ops >= MAX_OPS or spent + r.seconds > TIMEOUT_S:
                break
            ops += 1
            spent += r.seconds
            keeps += r.kept
        return keeps

    totals = {"scan": 0, "ranked": 0, "possible": 0, "files": 0, "files_improved": 0,
              "files_worsened": 0}
    for group in by_file.values():
        scan = run(sorted(group, key=lambda t: t[1].order))
        ranked = run(sorted(group, key=lambda t: -probs[t[0]]))
        totals["scan"] += scan
        totals["ranked"] += ranked
        totals["possible"] += sum(r.kept for _, r in group)
        totals["files"] += 1
        totals["files_improved"] += ranked > scan
        totals["files_worsened"] += ranked < scan
    return totals


def evaluate(rows: list[R]) -> dict:
    families = sorted({r.family for r in rows})
    skip = defaultdict(lambda: {"attempts": 0, "skipped": 0, "keeps": 0, "keeps_lost": 0,
                                "seconds": 0.0, "seconds_saved": 0.0})
    probs: dict[int, float] = {}
    brier, n_brier = 0.0, 0
    thresholds: list[float] = []
    index = {id(r): i for i, r in enumerate(rows)}

    for fam in families:
        train = [r for r in rows if r.family != fam]
        test = [r for r in rows if r.family == fam]
        if not train:
            continue
        z, prior = _standardiser(train), _prior(train)
        crossfit = _crossfit(train)
        cal = _calibrator(crossfit)
        t = _threshold(crossfit)
        thresholds.append(t)
        for r in test:
            raw = _score(r, train, z, prior)
            p = cal(raw)
            probs[index[id(r)]] = p
            brier += (p - r.kept) ** 2
            n_brier += 1
            for bucket in ("all", f"code:{r.code}"):
                s = skip[bucket]
                s["attempts"] += 1
                s["keeps"] += r.kept
                s["seconds"] += r.seconds
                if raw < t:
                    s["skipped"] += 1
                    s["keeps_lost"] += r.kept
                    s["seconds_saved"] += r.seconds

    report = {"rows": len(rows), "families": len(families),
              "target_keep_recall": TARGET_KEEP_RECALL,
              "threshold_range": [round(min(thresholds), 4), round(max(thresholds), 4)],
              "brier": round(brier / n_brier, 4), "skip": {}, "replay": _replay(rows, probs)}
    for bucket, s in sorted(skip.items()):
        report["skip"][bucket] = {
            "attempts": s["attempts"], "skipped": s["skipped"],
            "skipped_pct": round(100 * s["skipped"] / s["attempts"], 1),
            "keeps": s["keeps"], "keeps_lost": s["keeps_lost"],
            "keep_recall": round(1 - s["keeps_lost"] / s["keeps"], 4) if s["keeps"] else None,
            "attempt_seconds": round(s["seconds"], 1),
            "seconds_saved": round(s["seconds_saved"], 1),
            "seconds_saved_pct": round(100 * s["seconds_saved"] / s["seconds"], 1)
            if s["seconds"] else None}
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", nargs="+", type=Path)
    ap.add_argument("--precapped", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    exclude = frozenset(json.loads(args.precapped.read_text())) if args.precapped else frozenset()
    text = json.dumps(evaluate(load(args.records, exclude=exclude)), indent=2)
    if args.json:
        args.json.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
