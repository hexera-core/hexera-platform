# Responsibility: Task 44 - `stages.step_for` is called from the search's evaluation threads; two concurrent exports
#                 of two DIFFERENT BREPs must never be handed the same STEP path.
# Boundaries: the kernel is a fake pool whose `write_step_xcaf` blocks on a barrier, so the interleaving that a loaded
#             machine produces by luck is produced here on purpose. Everything in `step_for` is the real code.
"""The race this pins (found by capturing a failing trace of `test_a_fixable_injected_case_ends_clean`):
`step_for` named a new export `state_{len(_STEP_FOR)}.step` BEFORE the write and registered it AFTER, so two threads
that entered together got the same name; the second write replaced the first, and a candidate was judged on another
candidate's geometry. The result depended on which thread was late, i.e. on load, and the wrong path was memoised, so
the full-panel confirmation repeated the wrong verdict."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair_v4 import stages


class RacingPool:
    """`write_step_xcaf` writes the BREP's own name into the STEP, but only after BOTH callers have been named."""

    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties, timeout=10)

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> SimpleNamespace:
        assert fn == "write_step_xcaf"
        try:
            self.barrier.wait()  # both threads are inside the export: every name has been chosen
        except threading.BrokenBarrierError:
            pass  # a serialising implementation never gets both here; it simply proceeds
        Path(payload["path"]).write_bytes(Path(payload["brep"]).read_bytes())
        return SimpleNamespace(ok=True, status="ok", error=None, value={})


@pytest.fixture(autouse=True)
def _clean_step_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(stages, "_STEPS_DIR", [str(tmp_path / "steps")])
    (tmp_path / "steps").mkdir()
    monkeypatch.setattr(stages, "_STEP_FOR", {})
    monkeypatch.setattr(stages, "_deterministic", lambda path: None)


def test_two_threads_exporting_different_breps_get_different_step_files(tmp_path: Path) -> None:
    breps = []
    for name in ("a", "b"):
        p = tmp_path / f"{name}.brep"
        p.write_text(f"geometry-{name}")
        breps.append(p)
    pool = RacingPool(parties=2)
    got: dict[str, str] = {}
    errors: list[BaseException] = []

    def export(p: Path) -> None:
        try:
            got[p.stem] = stages.step_for(pool, str(p))  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=export, args=(p,)) for p in breps]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert got["a"] != got["b"], f"two different BREPs were handed one STEP path: {got}"
    for stem in ("a", "b"):
        assert Path(got[stem]).read_text() == f"geometry-{stem}", (
            f"the STEP for {stem!r} holds another candidate's geometry: {Path(got[stem]).read_text()!r}")
