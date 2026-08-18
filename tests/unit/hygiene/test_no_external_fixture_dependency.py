# Responsibility: Verify production code never references a licensed test fixture.
from __future__ import annotations

import re
from pathlib import Path

from tests._scan import scanned

REPO = Path(__file__).parents[3]
PRODUCT_ROOTS = [REPO / "src" / "meshpipeline", REPO / "src"]


def test_production_code_never_references_external_fixtures():
    offenders = []
    for root in PRODUCT_ROOTS:
        if not root.exists():
            continue
        for p in scanned(root.rglob("*.py"), "the scanned source root"):
            src = re.sub(r"#.*", "", p.read_text())
            if "tests/fixtures/external" in src:
                offenders.append(str(p.relative_to(REPO)))
    assert not offenders, (
        "production runtime code references the external-fixture dir (must not - its "
        "assets are per-machine and gitignored): " + ", ".join(offenders))
