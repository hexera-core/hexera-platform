# Responsibility: The two settings that selected stages inside the measurement package are gone from this
#                 platform, refused at startup, and read or written by nothing.
# Boundaries: reads this repository's files and the settings catalogue. It imports no agent module, so it runs and
#             reports in an environment where the package is absent, which is where a dead switch hides best.
#
# WHY THIS FILE EXISTS, and it replaces one. `tests/unit/application/test_geometry_package_switches.py` proved
# that `policy.arm_the_package` was CALLED at the two points the package was reached, after a round where it was
# documented as called and called from nowhere. That test was right and its subject is now deleted: the package
# removed both readers, so the two platform settings selected nothing, and an operator could set either one with
# no effect whatever. Deleting the old test without putting this in its place would have left the two names with
# no test naming them at all, which is how the first version of that defect lasted a round.
#
# WHY IT SURVIVED THE ROUND IT WAS BORN IN: `devtools/quality/_flags_gone_emit.py`, whose whole job is to prove no
# geometry gate is left, carried these two exact names on its exemption list. The last assertion below is that the
# exemption is gone, because a check told not to look at something is worth nothing about that thing.
from __future__ import annotations

import re
from pathlib import Path

import pytest

from meshpipeline.settings import inventory as inv

REPO = Path(__file__).resolve().parents[3]

#: The platform's two settings, and the package's own two variables they used to be armed into.
PLATFORM_NAMES = ("GEOMETRY_MEASURED_STOPS_ENABLED", "GEOMETRY_FLUID_SIDE_ENABLED")
PACKAGE_NAMES = ("GEOMETRY_AGENT_MEASURED_STOPS", "GEOMETRY_AGENT_FLUID_SIDE")


@pytest.mark.parametrize("name", PLATFORM_NAMES)
def test_the_platform_setting_is_refused_at_startup_with_a_reason(name):
    """Driven through the authority `runtime/startup.validate` calls, not read off the map. A deployment that
    still carries a stale value is told, rather than started with a setting that means nothing."""
    got = inv.retired_present({name: "false"})
    assert got, f"{name} is not refused at startup"
    assert len(got) == 1 and got[0][0] == name
    assert "no off state" in got[0][1], got[0][1]


@pytest.mark.parametrize("name", PLATFORM_NAMES + PACKAGE_NAMES)
def test_no_name_is_declared_as_a_supported_setting_any_more(name):
    assert name not in {v.name for v in inv.all_vars()}


@pytest.mark.parametrize("name", PACKAGE_NAMES)
def test_the_packages_own_variable_is_undeclared_rather_than_refused(name):
    """DELIBERATELY NOT REFUSED. These two are the measurement package's own names, and its tests and evals still
    export them in a developer's shell. Refusing them at this platform's startup would break that shell over two
    variables this platform no longer writes. Undeclared is the honest state: nothing here reads them."""
    assert not inv.retired_present({name: "on"})


def test_nothing_in_this_repository_reads_or_writes_any_of_the_four():
    """The claim, held on the tree. A comment may name them; a line of code may not touch them."""
    touching = re.compile(r"(?:environ|optional_env|getenv)[^\n]*(?:" + "|".join(PLATFORM_NAMES + PACKAGE_NAMES)
                          + r")|(?:" + "|".join(PLATFORM_NAMES + PACKAGE_NAMES) + r")\s*[:=]\s*(?!=)")
    hits = []
    for root in ("src", "devtools", "alembic", "deploy"):
        for path in sorted((REPO / root).rglob("*.py")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("#", "*", '"', "'")):
                    continue
                if touching.search(line):
                    hits.append(f"{path.relative_to(REPO)}:{n}: {stripped[:100]}")
    assert hits == [], f"a retired geometry stage setting has a reader or a writer again: {hits}"


def test_the_module_that_armed_them_and_the_function_that_decided_them_are_gone():
    assert not (REPO / "src" / "meshpipeline" / "settings" / "package_switches.py").exists()
    policy = (REPO / "src" / "meshpipeline" / "settings" / "policy.py").read_text(encoding="utf-8")
    assert "def arm_the_package" not in policy
    for app in ("geometry_measurement.py", "geometry_survey.py"):
        text = (REPO / "src" / "meshpipeline" / "application" / app).read_text(encoding="utf-8")
        assert "polcfg.arm_the_package()" not in text, app


def test_the_check_that_should_have_caught_this_no_longer_exempts_them():
    """`_flags_gone_emit._flags` lists every `GEOMETRY_*_ENABLED` left on the policy module and asserts there is
    none. It excluded these two by name for a round, which is the only reason they lasted one."""
    text = (REPO / "devtools" / "quality" / "_flags_gone_emit.py").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    for name in PLATFORM_NAMES:
        assert name not in body, f"{name} is still exempted from the no-geometry-gate check"


def test_the_configuration_reference_offers_neither_and_the_template_carries_neither():
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    for name in PLATFORM_NAMES + PACKAGE_NAMES:
        assert name not in env, f".env.example still offers {name}"
    doc = (REPO / "docs" / "reference" / "configuration.md").read_text(encoding="utf-8")
    begin, end = inv.REMOVED_BEGIN, inv.REMOVED_END
    outside = doc[:doc.index(begin)] + doc[doc.index(end):]
    for name in PLATFORM_NAMES:
        assert f"`{name}`" not in outside, f"{name} is named outside the retired table"
