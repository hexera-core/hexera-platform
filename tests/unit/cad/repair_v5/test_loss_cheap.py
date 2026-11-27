# Responsibility: CRS v5 r2h - the catastrophic-loss guard made cheap with its rule unchanged: the exact screen
#                 (`loss_kernel.same_body`), the input/output split of the tessellated measurement, and the parent's
#                 parallel merge of deferred bodies (`loss._measure_deferred`). No kernel.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.cad.repair_v4 import loss
from meshpipeline.cad.repair_v4.loss_kernel import SAME_REL, same_body
from meshpipeline.cad.repair_v5 import tess_kernel as K


def _sig(vol=1000.0, box=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0), counts=(6, 12, 8)):
    return (*counts, vol, box)


def test_same_body_is_exact_to_one_part_in_a_billion():
    a = _sig()
    assert same_body(a, _sig())
    assert same_body(a, _sig(vol=1000.0 * (1 + 0.5 * SAME_REL)))
    assert not same_body(a, _sig(vol=1000.0 * (1 + 10 * SAME_REL)))         # the volume moved
    assert not same_body(a, _sig(box=(0.0, 0.0, 0.0, 10.0, 10.0, 10.001)))  # the body moved / grew
    assert not same_body(a, _sig(counts=(7, 15, 10)))                       # a face was split
    assert not same_body(a, _sig(box=()))


def _cube(x0, y0, z0, x1, y1, z1):
    p = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return [t for a, b, c, d in quads for t in ((p[a], p[b], p[c]), (p[a], p[c], p[d]))]


def test_the_split_sampling_is_the_same_arithmetic():
    body, out = _cube(0, 0, 0, 10, 10, 10), _cube(0, 0, 0, 10, 5, 10)
    whole = K.kept_by_sampling(body, out)
    sample = K.inside_sample(body)
    # the sample survives the JSON round trip the sidecar makes (lists for tuples)
    sample["inner"] = [list(p) for p in sample["inner"]]
    assert K.kept_from_sample(sample, out) == whole
    assert 0.45 < whole["kept"] < 0.55


class _Pool:
    """Answers each deferred chunk with the bodies it was asked for (S2 lost, the rest kept); `fail` chunks fail."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def call(self, fn, payload, *, timeout_s):
        self.calls.append(payload)
        chunk = payload["bodies"]
        if self.fail in chunk:
            return SimpleNamespace(ok=False, value=None, status="timeout", error="x")
        bodies = {b: (0.0 if b == "S2" else 1.0) for b in chunk}
        return SimpleNamespace(ok=True, value={"bodies": bodies, "kept_sum": sum(bodies.values()) * 10.0,
                                               "vol_sum": 10.0 * len(chunk), "seconds": 1.0}, status="ok")


FIRST = {"solidless": False, "bodies": {"S1": 1.0}, "screened": ["S1"], "deferred": ["S2", "S3", "S4"],
         "kept_sum": 50.0, "vol_sum": 50.0, "bodies_total": 4, "volume_ratio": 0.9, "inverted": {}}


def test_deferred_bodies_are_measured_in_parallel_and_merged():
    pool = _Pool()
    merged = loss._measure_deferred(pool, {"original": "a", "repaired": "b"}, FIRST, timeout_s=10)
    assert sorted(b for c in pool.calls for b in c["bodies"]) == ["S2", "S3", "S4"]
    assert all(c["defer_over"] is None for c in pool.calls)
    report = loss.from_kernel(merged)
    assert report.lost_bodies == ("S2",) and report.catastrophic
    assert merged["part_kept"] == (50.0 + 20.0) / 80.0


def test_a_failed_chunk_leaves_only_its_bodies_unmeasured():
    merged = loss._measure_deferred(_Pool(fail="S2"), {"original": "a", "repaired": "b"}, FIRST, timeout_s=10)
    report = loss.from_kernel(merged)
    assert merged["bodies"]["S2"] is None and not report.catastrophic
    assert "S2" in report.detail["unmeasured_bodies"]
