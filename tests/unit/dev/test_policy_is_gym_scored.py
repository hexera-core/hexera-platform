# Responsibility: the gym gate. A v3 policy or prompt change is unshippable until it has been
# scored on a stratified sample of the corpus, and this file is what makes that true.
#
# Built in the spirit of tests/unit/cad/test_repair_oracle_invariants.py's V3_MODULES invariant: a
# LIVE listing is diffed against a FROZEN record, so a forgotten entry is a loud failure and never
# a silent hole. Here the live listing is `policy_fingerprint()` over the owner-tunable constants
# and the investigator prompt; the frozen record is `devtools/validation/gym/scored-policies.json`.
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GYM_PY = REPO / "devtools" / "validation" / "gym.py"


def _load(path: Path):
    """devtools/validation is not a package; this is how its own tests load its modules."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gym = _load(GYM_PY)


# --- a synthetic policy tree, so the fingerprint tests never depend on today's real numbers ----
def _write_policy_tree(root: Path, overrides: dict[tuple[str, str], str] | None = None) -> None:
    """Write a fake `src/` holding every declared policy source with placeholder values."""
    overrides = overrides or {}
    by_file: dict[str, list[tuple[str, str]]] = {}
    for source in gym.POLICY_SOURCES:
        value = overrides.get((source.relpath, source.attr), source.attr.lower() + "_placeholder")
        by_file.setdefault(source.relpath, []).append((source.attr, value))
    for relpath, pairs in by_file.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"{attr} = {value!r}" for attr, value in pairs)
        path.write_text(f"from __future__ import annotations\n\n{body}\n", encoding="utf-8")


@pytest.mark.parametrize("source", gym.POLICY_SOURCES, ids=lambda s: str(s.label))
def test_fingerprint_changes_with_any_constant(tmp_path, source):
    """EVERY declared constant is load-bearing: perturb one and the fingerprint must move.

    A fingerprint that ignored one constant would let that one knob be retuned with no gym run,
    which is the exact hole this task exists to close.
    """
    base, moved = tmp_path / "base", tmp_path / "moved"
    _write_policy_tree(base)
    _write_policy_tree(moved, overrides={(source.relpath, source.attr): "RETUNED"})
    before = gym.policy_fingerprint(root=base)
    after = gym.policy_fingerprint(root=moved)
    assert before != after, (
        f"{source.label} ({source.relpath}:{source.attr}) does not reach the fingerprint - it "
        f"could be retuned with no gym score")
    assert len(before) == 64 and len(after) == 64, "a fingerprint is a sha256 hex digest"


def test_fingerprint_ignores_everything_that_is_not_a_declared_policy(tmp_path):
    """Comments, unrelated constants and reformatting must NOT invalidate a recorded score, or
    the gate cries wolf on every refactor and gets disabled."""
    root = tmp_path / "src"
    _write_policy_tree(root)
    before = gym.policy_fingerprint(root=root)
    noisy = root / gym.POLICY_SOURCES[0].relpath
    noisy.write_text("# a new comment\nUNRELATED = 17\n" + noisy.read_text(encoding="utf-8"),
                     encoding="utf-8")
    assert gym.policy_fingerprint(root=root) == before


def test_a_policy_a_later_lane_still_owes_is_absent_not_a_crash(tmp_path):
    """Several declared policies live in modules concurrent lanes have not landed yet. The
    fingerprint must report those as absent - and must MOVE when the lane lands them."""
    empty, filled = tmp_path / "empty", tmp_path / "filled"
    empty.mkdir()
    constants = gym.policy_constants(root=empty)
    assert set(constants) == {s.label for s in gym.POLICY_SOURCES}
    assert set(constants.values()) == {gym.ABSENT}, constants
    _write_policy_tree(filled)
    assert gym.policy_fingerprint(root=empty) != gym.policy_fingerprint(root=filled), (
        "a lane landing a declared policy module must move the fingerprint")


def test_the_prompt_is_fingerprinted_by_hash_not_pasted_into_the_record(tmp_path):
    """A system prompt is thousands of characters; the record carries its sha256."""
    root = tmp_path / "src"
    _write_policy_tree(root, overrides={
        (gym.PROMPT_SOURCE.relpath, gym.PROMPT_SOURCE.attr): "you are an investigator" * 40})
    value = gym.policy_constants(root=root)[gym.PROMPT_SOURCE.label]
    assert len(value) == 64 and "investigator" not in value, value


# --- scoring two arms ---------------------------------------------------------------------------
def _bundle(directory: Path, rows: list[dict], *, sample: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sample.json").write_text(json.dumps({"files": sample}, sort_keys=True),
                                           encoding="utf-8")
    (directory / "rows.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    return directory


# A REAL gmsh error string, so the stratum this test asserts on is the one repair_corpus's own
# `stratum_of` derives - a made-up error text would silently land every row in `other`.
OVERLAP_ERROR = "PLC Error:  A facet of boundary (overlapping facets) on surface 12"


def _row(sha: str, *, before: bool, after: bool | None, seconds: float = 10.0,
         score: float | None = 0.5, family: str = OVERLAP_ERROR, solids: int = 1) -> dict:
    return {"sha256": sha, "name": f"{sha[:4]}.step", "meshes_before": before,
            "meshes_after": after, "seconds": seconds, "tier2_score": score,
            "gmsh_error": family, "solids": solids}


SAMPLE = [{"sha256": f"{i:064x}", "gmsh_error": OVERLAP_ERROR, "solids": 1}
          for i in range(1, 5)]


def test_score_refuses_mismatched_samples(tmp_path):
    """Two arms run on different samples are not comparable, and a comparison printed anyway is
    worse than no comparison: it would attribute a sampling difference to the policy."""
    arm = _bundle(tmp_path / "arm", [_row(SAMPLE[0]["sha256"], before=False, after=True)],
                  sample=SAMPLE)
    baseline = _bundle(tmp_path / "baseline",
                       [_row(SAMPLE[1]["sha256"], before=False, after=True)],
                       sample=SAMPLE[:2])
    assert gym.bundle_sample_sha256(arm) != gym.bundle_sample_sha256(baseline)
    with pytest.raises(ValueError) as exc:
        gym.score(arm, baseline)
    assert "sample" in str(exc.value).lower()
    assert gym.bundle_sample_sha256(arm)[:12] in str(exc.value), (
        "the refusal must name the hashes, or nobody can tell which arm is the odd one")


def test_score_counts_the_fixes_the_regressions_and_the_clean_control(tmp_path):
    shas = [entry["sha256"] for entry in SAMPLE]
    arm_rows = [
        _row(shas[0], before=False, after=True, seconds=20.0),    # fixed by the arm only
        _row(shas[1], before=False, after=True, seconds=30.0),    # fixed by both
        _row(shas[2], before=True, after=False, seconds=5.0),     # arm BROKE a clean part
        _row(shas[3], before=True, after=None, seconds=1.0),      # clean, untouched
    ]
    baseline_rows = [
        _row(shas[0], before=False, after=False, seconds=15.0),
        _row(shas[1], before=False, after=True, seconds=15.0),
        _row(shas[2], before=True, after=True, seconds=5.0),
        _row(shas[3], before=True, after=None, seconds=1.0),
    ]
    arm = _bundle(tmp_path / "arm", arm_rows, sample=SAMPLE)
    baseline = _bundle(tmp_path / "baseline", baseline_rows, sample=SAMPLE)
    out = gym.score(arm, baseline)

    assert out["files"] == 4
    assert out["newly_meshed"] == {"arm": 2, "baseline": 1, "delta": 1}
    assert out["newly_broken"] == {"arm": 1, "baseline": 0, "delta": 1}
    # THE CLEAN CONTROL: parts that already meshed. The arm regressed one of two.
    assert out["clean_control"] == {"files": 2, "arm_still_meshes": 1, "baseline_still_meshes": 2,
                                   "delta": -1}
    assert out["score_mean"]["arm"] == pytest.approx(0.5)
    # COST PER FIXED PART, which is what makes a 1-file gain over 56 s of extra compute visible.
    assert out["cost_per_fixed_part_s"]["arm"] == pytest.approx(56.0 / 2)
    assert out["cost_per_fixed_part_s"]["baseline"] == pytest.approx(36.0 / 1)
    stratum = out["per_stratum"]["overlapping_facets/single"]
    assert stratum["files"] == 4 and stratum["newly_meshed"]["arm"] == 2


def test_score_refuses_a_bundle_with_no_sample(tmp_path):
    """A corpus run that did not record its sample cannot be scored: provenance or nothing."""
    directory = tmp_path / "arm"
    directory.mkdir()
    (directory / "rows.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        gym.bundle_sample_sha256(directory)
    assert "sample.json" in str(exc.value)


# --- the record ---------------------------------------------------------------------------------
def test_record_carries_no_cad(tmp_path):
    """HARD INVARIANT: no CAD file is ever committed. The record is hashes and numbers only - no
    file name, no path into a corpus, nothing with a geometry extension."""
    arm = _bundle(tmp_path / "arm", [_row(SAMPLE[0]["sha256"], before=False, after=True)],
                  sample=SAMPLE)
    baseline = _bundle(tmp_path / "baseline",
                       [_row(SAMPLE[0]["sha256"], before=False, after=False)], sample=SAMPLE)
    db = tmp_path / "scored-policies.json"
    db.write_text('{"records": []}', encoding="utf-8")
    entry = gym.record(arm, baseline=baseline, db=db, fingerprint="f" * 64)

    assert entry["policy"] == "f" * 64
    assert entry["sample_sha256"] == gym.bundle_sample_sha256(arm)
    blob = json.dumps(json.loads(db.read_text(encoding="utf-8")))
    for extension in (".step", ".stp", ".stl", ".brep", ".iges", ".dxf"):
        assert extension not in blob.lower(), f"the record leaked a CAD path: {extension}"
    assert gym.scored_fingerprints(db) == ["f" * 64]


def test_record_is_append_only(tmp_path):
    arm = _bundle(tmp_path / "arm", [_row(SAMPLE[0]["sha256"], before=False, after=True)],
                  sample=SAMPLE)
    db = tmp_path / "scored-policies.json"
    db.write_text('{"records": []}', encoding="utf-8")
    gym.record(arm, baseline=None, db=db, fingerprint="a" * 64)
    gym.record(arm, baseline=None, db=db, fingerprint="b" * 64)
    assert gym.scored_fingerprints(db) == ["a" * 64, "b" * 64]


# --- the gate itself ----------------------------------------------------------------------------
def test_an_unscored_policy_fails_the_guard_and_says_what_to_run(tmp_path):
    """THE POINT OF THE TASK. A policy nobody scored is named, loudly, with the command."""
    db = tmp_path / "scored-policies.json"
    db.write_text(json.dumps({"records": [{"policy": "a" * 64, "evidence": "x"}]}),
                  encoding="utf-8")
    reason = gym.unscored_reason("b" * 64, db=db)
    assert reason, "an unrecorded fingerprint must be reported, not waved through"
    assert "b" * 64 in reason, "the message must name the fingerprint that is missing"
    assert "make gym" in reason, "the message must name the command that fixes it"


def test_a_scored_policy_passes_the_guard(tmp_path):
    db = tmp_path / "scored-policies.json"
    db.write_text(json.dumps({"records": [{"policy": "c" * 64, "evidence": "x"}]}),
                  encoding="utf-8")
    assert gym.unscored_reason("c" * 64, db=db) is None


def test_the_guard_reports_which_constants_moved(tmp_path):
    """Naming the fingerprint is not enough to act on; the message lists the policy labels so a
    reviewer can see WHAT needs a gym run."""
    db = tmp_path / "scored-policies.json"
    db.write_text(json.dumps({"records": []}), encoding="utf-8")
    reason = gym.unscored_reason("d" * 64, db=db, labels=["grind.ATTEMPTS"])
    assert "grind.ATTEMPTS" in str(reason)


def test_policy_is_gym_scored():
    """THE LIVE GATE: today's policy must appear in the committed record.

    Bootstrap: while the record holds nothing, this SKIPS with the reason, because the first score
    needs a corpus pass the controller serialises and no lane may start on its own. The skip is
    conditional on emptiness alone, so the first recorded score turns the gate hard - see below.
    """
    db = gym.POLICY_DB
    fingerprint = gym.policy_fingerprint()
    if not gym.scored_fingerprints(db):
        pytest.skip(
            f"gym record is empty: no v3 policy has been scored yet. Today's fingerprint is "
            f"{fingerprint}. Seed it with `make gym` on a stratified sample, then "
            f"`gym.py record`. This skip disappears the moment the first score is recorded.")
    reason = gym.unscored_reason(fingerprint, db=db, labels=sorted(gym.policy_constants()))
    assert reason is None, reason


def test_the_bootstrap_skip_cannot_outlive_the_first_record():
    """Guards the guard: once ANY score exists, an unscored fingerprint is a hard failure. Proven
    on a synthetic record so this holds whatever the committed file says today."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "scored-policies.json"
        db.write_text(json.dumps({"records": [{"policy": "e" * 64, "evidence": "x"}]}),
                      encoding="utf-8")
        assert gym.scored_fingerprints(db), "a seeded record is not empty"
        assert gym.unscored_reason(gym.policy_fingerprint(), db=db) is not None, (
            "with a seeded record, today's unscored policy MUST fail")
