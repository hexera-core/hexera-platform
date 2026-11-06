"""Leave-one-family-out evaluation: does local geometry predict which operation will be kept?

THE QUESTION IS NARROW ON PURPOSE. Not "is the descriptor a good embedding" but: given the
operations the deterministic rules already consider applicable to one defect, does looking up
similar defects in OTHER part families choose a kept operation more often than

  rules  the narrowest applicable operation (what `RulesPlanner` picks for this defect), or
  prior  the operation with the best kept rate for this defect code in the training families
         (the symbolic facets alone, no geometry)?

`knn` beating `rules` but not `prior` means the gain is the defect code, not the geometry, and an
encoder is not yet justified. Rows are grouped by family for the split and for the confidence
interval, never treated as independent.

Usage:
    .venv/bin/python -m labs.defect_cad_rag.knn /tmp/dcr-sweep/records.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

#: Attempt statuses that are a verdict on the operation. `guarded`, `budget` and `no_targets` are
#: not: the operation was never judged, so it is unknown, not negative.
JUDGED = {"kept": 1, "rolled_back": 0, "failed": 0}


@dataclass(frozen=True, slots=True)
class Row:
    family: str
    file: str
    code: str
    vector: tuple[float, ...]
    labels: dict  # op -> 1 kept / 0 not
    rules_op: str


def load(path: Path, *, exclude: frozenset[str] = frozenset()) -> list[Row]:
    """Labelled defect rows. `exclude` names files whose verdicts say nothing about operations
    (precap.py: they arrive over an absolute cap)."""
    rows: list[Row] = []
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r.get("kind") != "defect" or not r.get("vector") or r["file"] in exclude:
            continue
        labels = {a["op"]: JUDGED[a["status"]] for a in r["attempts"] if a["status"] in JUDGED}
        if labels:
            rows.append(Row(r["family"], r["file"], r["code"], tuple(r["vector"]), labels,
                            r.get("rules_op", "")))
    return rows


def _standardiser(rows: list[Row]):
    n = len(rows[0].vector)
    mean = [sum(r.vector[i] for r in rows) / len(rows) for i in range(n)]
    sd = [math.sqrt(sum((r.vector[i] - mean[i]) ** 2 for r in rows) / len(rows)) or 1.0
          for i in range(n)]
    return lambda v: [(v[i] - mean[i]) / sd[i] for i in range(n)]


def _prior(train: list[Row]) -> dict:
    """(code, op) -> Laplace-smoothed kept rate."""
    counts: dict = defaultdict(lambda: [0, 0])
    for r in train:
        for op, y in r.labels.items():
            counts[(r.code, op)][0] += y
            counts[(r.code, op)][1] += 1
    return {k: (k_ + 1) / (n + 2) for k, (k_, n) in counts.items()}


def _knn_scores(query: Row, train: list[Row], z, *, k: int, prior: dict) -> dict:
    """op -> estimated kept probability from the k nearest same-code training defects.

    Failures count as evidence AGAINST an operation (contraindications), and the estimate shrinks
    to the code prior with two pseudo-observations, so one lucky neighbour is not a certainty.
    """
    q = z(query.vector)
    pool = [r for r in train if r.code == query.code]
    scored = sorted(((math.dist(q, z(r.vector)), r) for r in pool), key=lambda t: t[0])[:k]
    out = {}
    for op in query.labels:
        w_kept = w_all = 0.0
        for d, r in scored:
            if op in r.labels:
                w = 1.0 / (d + 1e-3)
                w_all += w
                w_kept += w * r.labels[op]
        p0 = prior.get((query.code, op), 0.5)
        if w_all == 0:
            out[op] = p0
        else:
            # Shrink: the neighbours' weighted rate, worth as many observations as they are, plus
            # two of the prior. Normalised weights so the distance scale does not set the trust.
            n_eff = sum(1 for _, r in scored if op in r.labels)
            out[op] = (n_eff * (w_kept / w_all) + 2 * p0) / (n_eff + 2)
    return out


def _pick(scores: dict, rules_op: str) -> str:
    # Ties go to the rules choice, so a policy with no information reduces to the baseline.
    return max(scores, key=lambda op: (round(scores[op], 9), op == rules_op))


def _auc(pairs: list[tuple[float, int]]) -> float | None:
    pos = [s for s, y in pairs if y == 1]
    neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def evaluate(rows: list[Row], *, k: int = 10, seed: int = 0) -> dict:
    families = sorted({r.family for r in rows})
    per_family: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    auc_pairs: dict = defaultdict(list)
    for fam in families:
        train = [r for r in rows if r.family != fam]
        test = [r for r in rows if r.family == fam]
        if not train:
            continue
        z = _standardiser(train)
        prior = _prior(train)
        for r in test:
            knn = _knn_scores(r, train, z, k=k, prior=prior)
            pri = {op: prior.get((r.code, op), 0.5) for op in r.labels}
            choices = {
                "rules": r.rules_op if r.rules_op in r.labels else _pick(pri, r.rules_op),
                "prior": _pick(pri, r.rules_op),
                "knn": _pick(knn, r.rules_op),
                "oracle": max(r.labels, key=lambda op: r.labels[op]),
            }
            choice_matters = len(r.labels) > 1
            for name, op in choices.items():
                for bucket in ("all", f"code:{r.code}") + (("multi_op",) if choice_matters else ()):
                    per_family[(bucket, name)][fam][0] += r.labels[op]
                    per_family[(bucket, name)][fam][1] += 1
            # "WILL THE RULES OPERATION BE KEPT?" - the abstention question. A good answer lets a
            # run with thousands of defects skip attempts that will be rolled back.
            if r.rules_op in r.labels:
                y = r.labels[r.rules_op]
                auc_pairs[("all", "knn")].append((knn[r.rules_op], y))
                auc_pairs[("all", "prior")].append((pri[r.rules_op], y))
                auc_pairs[(f"code:{r.code}", "knn")].append((knn[r.rules_op], y))
                auc_pairs[(f"code:{r.code}", "prior")].append((pri[r.rules_op], y))

    rng = random.Random(seed)
    report: dict = {"rows": len(rows), "families": len(families), "k": k, "top1_kept_rate": {},
                    "rules_op_kept_auc": {}}
    for (bucket, name), fams in sorted(per_family.items()):
        hits = sum(v[0] for v in fams.values())
        n = sum(v[1] for v in fams.values())
        keys = list(fams)
        boots = []
        for _ in range(1000):
            draw = [rng.choice(keys) for _ in keys]
            bn = sum(fams[f][1] for f in draw)
            boots.append(sum(fams[f][0] for f in draw) / bn if bn else 0.0)
        boots.sort()
        report["top1_kept_rate"].setdefault(bucket, {})[name] = {
            "rate": round(hits / n, 4) if n else None, "n": n, "families": len(fams),
            "ci95": [round(boots[25], 4), round(boots[974], 4)]}
    for (bucket, name), pairs in sorted(auc_pairs.items()):
        auc = _auc(pairs)
        report["rules_op_kept_auc"].setdefault(bucket, {})[name] = {
            "auc": round(auc, 4) if auc is not None else None, "n": len(pairs),
            "positives": sum(y for _, y in pairs)}
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", type=Path)
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--precapped", type=Path, help="precap.py output; those files are dropped")
    args = ap.parse_args(argv)
    exclude = frozenset(json.loads(args.precapped.read_text())) if args.precapped else frozenset()
    report = evaluate(load(args.records, exclude=exclude), k=args.k)
    report["excluded_files"] = sorted(exclude)
    text = json.dumps(report, indent=2)
    if args.json:
        args.json.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
