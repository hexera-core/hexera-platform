"""`focus_faces`: a repair SCOPED to faces something else already named (plan task 26).

The mesher handback hands repair the faces the FEA ladder actually failed on, so this is the one
case where repair is not guessing what a mesher will dislike. A focused run must therefore:

* walk ONLY the regions holding one of those faces;
* REPORT every other region as `not_in_focus` rather than silently skipping it - a customer owed
  an account of a partial repair is the reason this loop reports refusals at all;
* be byte-for-byte today's run when the focus is EMPTY, which is what every other caller passes.

NO KERNEL HERE. The shape is a string, the scanner and measurer are doubles, and the clock moves
only when the work being timed says it did - the same harness `test_repair_region_loop.py` uses.
"""
from __future__ import annotations

import inspect

from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import LoopBudget
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.region_loop import NOT_IN_FOCUS, focus_filter, run_regions
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import RepairSession

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)

#: Two FACE defects 100 mm apart on a 200 mm part. `decompose` merges boxes within 1% of the
#: diagonal (2 mm), so 100 mm apart is two regions - and this file is only evidence of anything
#: while that stays true. Face 7 is the one the "mesher" names; face 12 is the other region.
_FACES = {7: (0.0, 1.0), 12: (100.0, 101.0)}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _defect(index: int) -> EntityDefect:
    low, high = _FACES[index]
    return EntityDefect(code=DefectCode.small_face, severity=DefectSeverity.error,
                        entity_type="face", entity_index=index,
                        message=f"a sliver face at {low}", fingerprint=f"face:{index}",
                        location={"bbox_min": [low, low, low], "bbox_max": [high, high, high]})


class _Op:
    answers = frozenset({DefectCode.small_face})
    scope = 1

    def __init__(self, clock: _Clock, index: int) -> None:
        self.name = f"fix_{index}"
        self._clock = clock
        self._index = index

    def apply(self, shape, targets, *, tolerance_mm):
        self._clock.now += 0.5
        return OpOutcome(shape=f"{shape}+{self._index}", evidence={})


class _Part:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.scans = 0
        self.ops = {i: _Op(self.clock, i) for i in _FACES}
        #: Every region index the author was ASKED about: the only honest way to prove a region
        #: was left UNRUN rather than run and found sound.
        self.asked: list[int] = []

    def scanner(self, shape):
        self.clock.now += 1.0
        self.scans += 1
        return [_defect(i) for i in _FACES if f"+{i}" not in shape], True

    def measurer(self, shape):
        return Measure(**{**_SOUND.to_dict(), "vertices": 40 + str(shape).count("+")})

    def session(self) -> RepairSession:
        return RepairSession("original", measurer=self.measurer, scanner=self.scanner,
                             clock=self.clock)

    def author(self):
        def author(region, defects, attempts):
            self.asked.append(int(region.index))
            defect = defects[0]
            index = int(defect.fingerprint.split(":")[1])
            predicate = Predicate(cleared=frozenset({defect.fingerprint}),
                                  reclassified=frozenset(), unchanged=frozenset(),
                                  max_area_delta_mm2=0.0, max_volume_delta_mm3=0.0)
            plan = ({"op": self.ops[index], "defect_code": DefectCode.small_face,
                     "targets": (EntityRef(entity_type="face", fingerprint=defect.fingerprint,
                                           entity_index=index),)},)
            return TargetPackage(region=region, intent=f"clear {defect.fingerprint}", plan=plan,
                                 predicate=predicate,
                                 budget=PackageBudget(max_ops=4, timeout_s=1000.0, model_calls=0,
                                                      deviation_mm=0.0))
        return author


def _run(part: _Part, **kw):
    return run_regions("original", author=part.author(), tolerance_mm=0.1,
                       budget=LoopBudget(max_ops=9, stall_limit=9, timeout_s=1000.0),
                       session=part.session(), clock=part.clock, **kw)


# --------------------------------------------------------------------------- the pure filter


def _region(index: int, faces: tuple[int, ...]) -> Region:
    return Region(index=index, fingerprints=frozenset({f"face:{f}" for f in faces}),
                  bbox_min=(0.0, 0.0, 0.0), bbox_max=(1.0, 1.0, 1.0),
                  face_indices=frozenset(faces))


def test_an_empty_focus_is_the_walk_it_was_given():
    walk = (_region(0, (7,)), _region(1, (12,)))
    kept, refused = focus_filter(walk, ())
    assert kept == walk
    assert refused == ()


def test_the_filter_keeps_only_regions_holding_a_focus_face():
    walk = (_region(0, (7, 8)), _region(1, (12,)), _region(2, ()))
    kept, refused = focus_filter(walk, (8,))
    assert [r.index for r in kept] == [0], "region 0 holds face 8; nothing else does"
    assert [r["refusal"] for r in refused] == [NOT_IN_FOCUS, NOT_IN_FOCUS]
    assert {r["region"] for r in refused} == {1, 2}
    assert refused[0]["focus_faces"] == [8]
    assert NOT_IN_FOCUS == "not_in_focus"


def test_the_filter_keeps_the_walk_order():
    walk = (_region(5, (12,)), _region(0, (7,)))
    kept, _ = focus_filter(walk, (7, 12))
    assert [r.index for r in kept] == [5, 0], "the focus filters; it never re-orders"


def test_a_focus_naming_nothing_on_the_part_runs_nothing():
    """A face index no region holds is not a licence to repair everything."""
    walk = (_region(0, (7,)), _region(1, (12,)))
    kept, refused = focus_filter(walk, (99,))
    assert kept == ()
    assert len(refused) == 2


# --------------------------------------------------------------------------- the loop


def test_focus_limits_the_walk():
    """The out-of-focus region is REPORTED and UNRUN: never authored for, never repaired."""
    part = _Part()
    run = _run(part, focus_faces=(7,))

    in_focus = [r.index for r in run.regions if 7 in r.face_indices]
    out_of_focus = [r.index for r in run.regions if 7 not in r.face_indices]
    assert len(in_focus) == 1 and len(out_of_focus) == 1, "the fixture must give two regions"

    assert part.asked == in_focus, "only the focused region may be offered a package"
    assert [p.region_index for p in run.packages] == in_focus
    refusals = [r for r in run.refusals if r.get("refusal") == NOT_IN_FOCUS]
    assert [r["region"] for r in refusals] == out_of_focus
    assert refusals[0]["focus_faces"] == [7]
    # AND THE OTHER FACE IS STILL THERE: the out-of-focus defect was not repaired by accident.
    assert "+12" not in str(run.shape)
    assert "+7" in str(run.shape)


def test_no_focus_walks_both_regions():
    """The control. Without a focus the same fixture repairs both, so the test above is measuring
    the focus and not a broken author."""
    part = _Part()
    run = _run(part)
    assert sorted(part.asked) == [0, 1]
    assert run.refusals == ()
    assert "+7" in str(run.shape) and "+12" in str(run.shape)


def test_an_empty_focus_adds_no_refusal_key():
    """Flag-off is byte-identical: `()` must not add a `not_in_focus` row to any report."""
    part = _Part()
    run = _run(part, focus_faces=())
    assert run.refusals == ()
    assert "not_in_focus" not in str(run.to_dict())


# --------------------------------------------------------------------------- the entry point


def test_repair_step_file_takes_focus_faces_keyword_only_defaulting_empty():
    from meshpipeline.cad.repair.conservative import repair_step_file
    sig = inspect.signature(repair_step_file)
    p = sig.parameters["focus_faces"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default == (), "every caller but the handback passes nothing"
    assert inspect.signature(run_regions).parameters["focus_faces"].default == ()


def test_a_focus_reaches_the_region_driver_and_nothing_else(monkeypatch):
    """`focus_faces` is the region driver's parameter. If it ever reached the operation-at-a-time
    driver silently, a focused run would repair the WHOLE part - the exact failure the handback
    exists to avoid - so the unsupported case is refused, loudly."""
    import meshpipeline.cad.repair.conservative as C
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True, raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "off", raising=False)
    # A PATH THAT DOES NOT EXIST, deliberately: the refusal must come BEFORE the file is read,
    # because a caller whose focus cannot be honoured should not pay for a kernel read first.
    try:
        C.repair_step_file("does-not-exist.step", "out.step", focus_faces=(7,))
    except C.RepairUnavailable as exc:
        assert "region" in str(exc).lower()
    else:  # pragma: no cover - the refusal is the point
        raise AssertionError("a focused run on the operation-at-a-time driver must refuse")
