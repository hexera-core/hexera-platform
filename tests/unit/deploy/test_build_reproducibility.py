# Responsibility: Verify every dependency resolver is constrained by the one constraints authority.
# Boundaries: it inspects tracked build inputs; it builds no image.
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parents[3]
sys.path.insert(0, str(REPO / "devtools" / "quality"))

import check_dependency_drift as drift  # noqa: E402

CONSTRAINTS = "requirements/constraints.txt"


# constraints reach every resolver

def test_every_project_dependency_installer_is_constrained():
    offenders = []
    for rel, line_no, line in drift._install_commands():
        if drift._is_bootstrap(line) or "--no-deps" in line:
            continue
        if drift._resolves_project_dependencies(line) and CONSTRAINTS not in line:
            offenders.append(f"{rel}:{line_no}: {line}")
    assert offenders == [], "unconstrained project-dependency installation:\n  " + "\n  ".join(offenders)


def test_the_installer_scan_is_not_vacuous():
    resolvers = [c for c in drift._install_commands()
                 if drift._resolves_project_dependencies(c[2])
                 and not drift._is_bootstrap(c[2]) and "--no-deps" not in c[2]]
    assert len(resolvers) >= 4, (
        f"only {len(resolvers)} project-dependency installers found - the scan has stopped seeing them")
    files = {r[0] for r in resolvers}
    for expected in (".github/workflows/ci.yml", "devtools/env/setup.sh", "Dockerfile"):
        assert expected in files, f"{expected} is no longer discovered as an installer"


def test_no_second_constraint_authority():
    for rel, line_no, line in drift._install_commands():
        for ref in re.findall(r"-c\s+(\S+)", line):
            assert CONSTRAINTS in ref, f"{rel}:{line_no} constrains with {ref}, not the one authority"


def test_the_drift_gate_passes():
    r = subprocess.run([sys.executable, str(REPO / "devtools/quality/check_dependency_drift.py")],
                       cwd=str(REPO), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all constrained by" in r.stdout


def test_exemptions_are_only_no_deps_and_bootstrap():
    # Every command the scan skips must be skippable for a proven reason, not because it looked
    # inconvenient. Anything else resolving project groups would have failed the guard above.
    for rel, line_no, line in drift._install_commands():
        if drift._resolves_project_dependencies(line):
            continue
        assert ("--no-deps" in line or drift._is_bootstrap(line)
                or not re.search(r"requirements/(runtime|dev)\.txt", line)), (
            f"{rel}:{line_no} resolves project groups but was treated as exempt")
