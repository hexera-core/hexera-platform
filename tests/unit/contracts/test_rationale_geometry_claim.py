# Responsibility: Verify the intake completion sentence claims a geometry check only when one happened.
# Boundaries: the sentence and its default; whether a check happens is agents/intake/'s to decide.
from __future__ import annotations

import ast
import pathlib

from meshpipeline.contracts import rationale as R

SOURCE = pathlib.Path(R.__file__).read_text(encoding="utf-8")


class _Recorder:
    def __init__(self):
        self.calls: list = []

    def rationale(self, conclusion, because=""):
        self.calls.append((conclusion, because))


def _said(**kwargs) -> tuple[str, str]:
    pub = _Recorder()
    R.intake_requirements_finalized(pub, patches=3, dimensionality="3D", **kwargs)
    assert len(pub.calls) == 1
    return pub.calls[0]


def test_the_default_does_not_claim_the_geometry_was_checked():
    """It claimed exactly that until 2026-09-15, on every job ever run, while intake's one geometry
    reader listed a directory the upload endpoint deletes. The default is what every caller gets,
    so the default is the one that has to be true."""
    _conclusion, because = _said()
    assert "checked against the selected engine" in because
    assert "the geometry itself was not measured" in because
    assert "checked against the geometry" not in because
    assert "measured geometry" not in because


def test_the_conclusion_itself_is_unchanged():
    conclusion, _because = _said()
    assert conclusion == "Requirements are complete and validated."


def test_it_still_reports_what_it_really_validated():
    _conclusion, because = _said()
    assert "3 boundary assignment(s)" in because
    assert "3D domain" in because


def test_the_true_sentence_is_one_argument_away():
    """The fix is not to delete the claim forever. When intake reads the stored measurement and
    binds each declared patch to a measured opening, one keyword makes the sentence true again."""
    _conclusion, because = _said(geometry_checked=True)
    assert "checked against the measured geometry and the selected engine" in because


def test_the_honest_sentence_is_the_default_in_the_signature_itself():
    tree = ast.parse(SOURCE)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "intake_requirements_finalized")
    names = [a.arg for a in fn.args.kwonlyargs]
    defaults = dict(zip(names, fn.args.kw_defaults))
    assert "geometry_checked" in names
    node = defaults["geometry_checked"]
    assert isinstance(node, ast.Constant) and node.value is False, (
        "a claim that has to be switched off to be honest goes back to lying the first time "
        "somebody adds a caller")


def test_no_other_sentence_in_this_module_claims_a_geometry_check_nothing_performs():
    """`geometry_admission` is allowed to talk about the geometry: it is published from the engine
    admission node, which has actually been handed a surface."""
    tree = ast.parse(SOURCE)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("intake_"):
            continue
        text = ast.get_source_segment(SOURCE, node) or ""
        if "against the geometry" in text:
            offenders.append(node.name)
    assert offenders == []
