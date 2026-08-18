# Responsibility: Verify no test-framework code is reachable from the shipped application.
from __future__ import annotations

import re
from pathlib import Path

from tests._scan import scanned

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

# class definitions whose name marks a test double
_DOUBLE_CLASS = re.compile(r"^\s*class\s+(Fake|Mock|Stub|Dummy)\w*", re.MULTILINE)
# test-framework imports/usages that only make sense under pytest/unittest
_TEST_ONLY = (
    re.compile(r"\bfrom\s+unittest\.mock\b"),
    re.compile(r"\bimport\s+unittest\.mock\b"),
    re.compile(r"\bMagicMock\b"),
    re.compile(r"\bmonkeypatch\b"),
    re.compile(r"\bimport\s+pytest\b"),
)


def _strip_comments(src: str) -> str:
    # a word in a comment (e.g. "not a mock") is fine; only real code counts
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    return re.sub(r"#.*", "", src)


def test_no_test_framework_code_in_app():
    offenders = []
    for p in scanned(APP.rglob("*.py"), "the shipped application package"):
        code = _strip_comments(p.read_text())
        for rx in _TEST_ONLY:
            if rx.search(code):
                offenders.append(f"{p.relative_to(APP)}: {rx.pattern}")
    assert not offenders, (
        "pytest/unittest-only code found in src/meshpipeline/ - it belongs in tests/:\n  "
        + "\n  ".join(offenders))
