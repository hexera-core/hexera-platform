"""TIME-9: the upload's clean check stops at its time limit, and the user is told it did not finish -
never "clean", never "not clean", and it is never run again unbounded in the scout's own process."""
from __future__ import annotations

from meshpipeline.application import fea_check
from meshpipeline.application import geometry_check as gc


class _TimedOut:
    timeout_s = gc.CLEAN_CHECK_LIMIT_S

    def result(self):
        return {"error": f"took over {self.timeout_s:.0f} s", "timed_out": True}


def test_the_limit_is_two_minutes():
    assert gc.CLEAN_CHECK_LIMIT_S == 120.0


def test_the_check_beside_the_scout_is_started_with_the_limit(tmp_path, monkeypatch):
    import meshpipeline.cad.beside as beside

    seen = {}

    class _Fake:
        def __init__(self, module, task, work, *, name, timeout_s=None):
            seen.update(module=module, timeout_s=timeout_s)
            self.proc = object()

    monkeypatch.setattr(beside, "Beside", _Fake)
    monkeypatch.setattr("meshpipeline.cad.validity.canonical_task", lambda u, c: {})
    assert gc._start_clean_verdict(tmp_path / "x.igs", None, tmp_path) is not None
    assert seen == {"module": "meshpipeline.cad.validity", "timeout_s": 120.0}


def test_a_check_past_its_limit_is_said_as_not_finished_and_not_run_again(monkeypatch):
    def _never(*a, **k):
        raise AssertionError("the check must not run again in-process after its time limit")

    monkeypatch.setattr("meshpipeline.cad.validity.check_canonical", _never)
    v = gc.clean_verdict(None, None, started=_TimedOut())
    assert v["clean"] is None and v["complete"] is False and v["timed_out"] is True
    assert v["kind"] == "unchecked" and v["reasons"] == [] and v["rule"] == "TIME-9"
    assert v["summary"] == ("The check of this file did not finish in 2 minutes, so it is not known whether it "
                            "is clean. The mesh goes ahead.")


def test_the_stage_says_not_checked_never_clean_or_not_clean():
    facts = {"validity": gc.unfinished_verdict()}
    clean, words = fea_check.clean_words(facts)
    assert clean is None and "did not finish" in words
    assert fea_check.declared_clean(facts).startswith("Not checked: The check of this file did not finish")
    assert fea_check.has_solid(facts) is True          # an unjudged file is never stopped
