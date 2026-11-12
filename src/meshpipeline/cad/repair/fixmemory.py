# Responsibility: Answer "how did operations like this end on geometry like this?" from a baked
# snapshot of lab action-sweep outcomes, and SHADOW that answer beside a live run.
# Boundaries: advisory only, and fail-open by design. A prediction never reaches a kernel call:
# with CAD_REPAIR_RAG=off nothing here runs at all, and in shadow a failure or an exhausted budget
# costs a log row, never the repair. Geometry stays the session's and the caps'.
"""The fix memory, shadow posture.

The memory is a SNAPSHOT baked at export time (labs/defect_cad_rag/export_memory.py) from the
independent action sweeps of experiments 1-2: per sampled defect, the hand-v2 descriptor and how
each applicable operation ended when tried alone under the real caps. Prediction is exact same-code
kNN over that snapshot with shrinkage toward the (code, op) prior - the policy that measured
0.946 held-out keep recall while skipping 49% of attempts offline
(docs/research/defect-cad-rag-experiment-2.md).

Why a baked file and not the database: the spec's store choice starts with exact in-process search
and moves to Postgres+pgvector only when durable shared access is needed. Two thousand 65-float
vectors are a megabyte; the repair loop runs where the kernel runs, and a worker that cannot reach
a store must not lose its shadow rows. The snapshot rides the image under `data/`.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.patchdesc import DESCRIPTOR_VERSION, _Topology, describe, isolate_patch

logger = logging.getLogger(__name__)

#: The snapshot shipped beside the engine, used when CAD_REPAIR_RAG_MEMORY_PATH is empty.
_PACKAGED = Path(__file__).resolve().parent / "data" / "fixmemory-hand-v2.json.gz"

#: Neighbours consulted per prediction - the k experiment 2's numbers were measured at.
K = 10

#: The slice of the run's repair allowance shadow work may spend, in total, per run. The spec
#: reserves at least 90% of CAD_REPAIR_TIMEOUT_S for the repair loop itself; prediction is ~1 ms
#: per defect, so this bound should never bind - it exists so a pathological part cannot make the
#: shadow the slow part of a run that is already short of time.
SHADOW_BUDGET_FRACTION = 0.1


@dataclass(frozen=True, slots=True)
class _Snapshot:
    descriptor_version: str
    mean: tuple[float, ...]
    sd: tuple[float, ...]
    #: Per sampled defect: code, standardised vector, {op: 1 kept / 0 rolled back or failed}.
    defects: tuple[tuple[str, tuple[float, ...], dict], ...]
    #: (code, op) -> Laplace-smoothed kept rate, baked at export time.
    priors: dict

    def standardise(self, vector: list[float]) -> tuple[float, ...]:
        return tuple((v - m) / s for v, m, s in zip(vector, self.mean, self.sd))


_CACHE: dict[str, _Snapshot | None] = {}


def _load(path: str) -> _Snapshot | None:
    """The snapshot at `path`, cached; None - recorded once - when it cannot serve.

    A missing or unreadable file, or one baked by a different descriptor, yields None rather than
    an exception: shadow is advisory, and the run must proceed exactly as if the flag were off.
    """
    if path in _CACHE:
        return _CACHE[path]
    snapshot: _Snapshot | None = None
    try:
        raw = Path(path).read_bytes()
        payload = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        if payload.get("descriptor_version") != DESCRIPTOR_VERSION:
            # A memory keyed by another descriptor is not comparable; using it would be a silent
            # retrieval of the wrong neighbours, which is worse than no retrieval.
            logger.warning("fix memory %s is %s, engine descriptor is %s; shadow disabled",
                           path, payload.get("descriptor_version"), DESCRIPTOR_VERSION)
        else:
            mean, sd = payload["mean"], payload["sd"]
            snapshot = _Snapshot(
                descriptor_version=payload["descriptor_version"],
                mean=tuple(mean), sd=tuple(sd),
                defects=tuple(
                    (d["c"], tuple((v - m) / s for v, m, s in zip(d["v"], mean, sd)),
                     dict(d["ops"]))
                    for d in payload["defects"]),
                priors={tuple(k.split("|", 1)): v for k, v in payload["priors"].items()})
    except Exception as exc:  # noqa: BLE001 - fail open: shadow must never cost the run
        logger.warning("fix memory unavailable at %s: %s", path, exc)
    _CACHE[path] = snapshot
    return snapshot


def predict_kept(snapshot: _Snapshot, code: str, vector: list[float], op: str) -> float:
    """P(kept) for `op` on this patch: distance-weighted same-code kNN, shrunk to the prior.

    The exact scoring experiment 2 validated: neighbours whose attempt of `op` was judged vote
    kept/not with weight 1/(d+1e-3); their weighted rate is worth as many observations as there
    are voting neighbours, plus two of the (code, op) prior - one lucky neighbour is never a
    certainty, and a defect with no informed neighbours answers with the prior alone.
    """
    q = snapshot.standardise(vector)
    scored = sorted(((math.dist(q, v), ops) for c, v, ops in snapshot.defects
                     if c == code and op in ops), key=lambda t: t[0])[:K]
    p0 = snapshot.priors.get((code, op), 0.5)
    if not scored:
        return p0
    w_all = sum(1.0 / (d + 1e-3) for d, _ in scored)
    w_kept = sum((1.0 / (d + 1e-3)) * ops[op] for d, ops in scored)
    return (len(scored) * (w_kept / w_all) + 2 * p0) / (len(scored) + 2)


@dataclass
class ShadowScorer:
    """One run's shadow: predicts per proposed attempt, logs rows, never touches the outcome.

    Constructed by the loop only when `CAD_REPAIR_RAG` is `shadow`. Every public call is
    fail-open: an exception becomes an `error` field on the row and the run continues.
    """

    rows: list[dict] = field(default_factory=list)
    _spent_s: float = 0.0
    _topology_for: int | None = None
    _topology: _Topology | None = None

    def __post_init__(self) -> None:
        self._snapshot = _load(rcfg.CAD_REPAIR_RAG_MEMORY_PATH or str(_PACKAGED))
        self._budget_s = SHADOW_BUDGET_FRACTION * rcfg.CAD_REPAIR_TIMEOUT_S

    @property
    def available(self) -> bool:
        return self._snapshot is not None

    def observe(self, shape, defect, op_name: str) -> None:
        """Predict P(kept) for the attempt the loop is about to make, and record it.

        Called BEFORE the attempt, so the prediction cannot see the verdict; `close()` on the
        loop's log joins them afterwards. The topology index is rebuilt only when the shape handle
        changed (a kept operation), which is what keeps per-defect cost at the measured ~1 ms.
        """
        row = {"op": op_name, "defect_code": defect.code.value,
               "fingerprint": defect.fingerprint, "p_kept": None}
        if self._snapshot is None:
            return
        if self._spent_s >= self._budget_s:
            row["skipped"] = "shadow_budget"
            self.rows.append(row)
            return
        started = time.monotonic()
        try:
            if self._topology_for != id(shape) or self._topology is None:
                self._topology = _Topology(shape)
                self._topology_for = id(shape)
            topology = self._topology
            patch = isolate_patch(shape, defect, topology=topology)
            vector = describe(patch, topology=topology)
            row["p_kept"] = round(
                predict_kept(self._snapshot, defect.code.value, vector, op_name), 4)
        except Exception as exc:  # noqa: BLE001 - a shadow failure is a row, never a run failure
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["predict_s"] = round(time.monotonic() - started, 5)
        self._spent_s += time.monotonic() - started
        self.rows.append(row)

    def resolve(self, status: str) -> None:
        """Join the verdict of the attempt just made onto the row `observe` wrote for it."""
        if self.rows and "status" not in self.rows[-1]:
            self.rows[-1]["status"] = status

    def close(self) -> tuple[dict, ...]:
        """Every row, in attempt order. A row no verdict resolved stays status-less, visibly."""
        return tuple(self.rows)
