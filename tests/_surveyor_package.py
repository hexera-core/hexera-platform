# Responsibility: Decide what the absence of the geometry-agent distribution means for a test that needs it.
# Owns: the one line between "this developer has no agent checkout" and "this environment shipped without it".
# Boundaries: it imports and it raises; it installs nothing and configures nothing.
#
# WHY THIS EXISTS. Every surveyor test used to open with
# `pytest.importorskip("geometry_agent...")`, and nothing installed the geometry agent into the built
# images - not requirements/runtime.txt, not pyproject.toml, not a Dockerfile line, not a deploy script.
# So the suite went green in exactly the environment where the package was missing. The check had the
# same blind spot as the thing it checked. A skip is a test SAYING NOTHING, and a suite that says
# nothing about the product's central feature reads identically to one that says it works.
#
# WHERE THE LINE IS DRAWN, and it is drawn in two places on purpose:
#
#   1. Running against an INSTALLED meshpipeline - a built image, or any environment that installed the
#      wheel rather than putting src/ on the path. Absence is a FAILURE and NOTHING can silence it. Not
#      an environment variable, not a flag. An image either carries the package or it is broken, and the
#      opt-out below must not be able to travel into the one place the answer matters. This is the rule
#      the defect needed: `make test-container-smoke` runs the suite inside the image, and on the
#      unfixed Dockerfile it now fails there instead of passing.
#
#   2. Running from a source CHECKOUT - a developer's clone. Absence is still a failure by DEFAULT,
#      because the default has to be the loud one: a CI job, a container or a script that forgot to say
#      anything gets the noise rather than the silence. A developer who genuinely has no agent checkout
#      sets GEOMETRY_AGENT_ABSENT=1 once and the surveyor tests skip while the rest of the suite runs.
#      The failure message names that variable, so nobody has to find this file to learn it.
#
# The variable is deliberately NOT read through meshpipeline.settings: it is a property of the test
# ENVIRONMENT, not a supported product setting, and the settings catalogue is for what the application
# reads.
from __future__ import annotations

import importlib
import os
import site
from pathlib import Path

import pytest

#: The developer's opt-out. Honoured in a checkout, ignored against an installed distribution.
ABSENT_ENV = "GEOMETRY_AGENT_ABSENT"


def _meshpipeline_is_installed() -> bool:
    """True when this interpreter imports meshpipeline from an install root rather than from src/.

    The same question deploy/verify_install.py asks inside the image build, asked the same way: an
    installed distribution is an environment that was BUILT, and a built environment that lacks the
    geometry agent is a broken artefact rather than an incomplete workstation.
    """
    import meshpipeline

    pkg = Path(meshpipeline.__file__).resolve().parent
    roots = [Path(p).resolve() for p in (site.getsitepackages() + [site.getusersitepackages()])]
    return any(root in pkg.parents for root in roots)


def require(module: str, *, needs: str = "") -> object:
    """Import `module` from the geometry agent, or end the test loudly. Never skips silently.

    `needs` says what the caller wanted it for, so the failure reads as a missing capability rather
    than a missing import line.
    """
    if not module.startswith("geometry_agent"):
        raise ValueError(f"{module!r} is not a geometry_agent module; this helper is only for the agent")
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        installed = _meshpipeline_is_installed()
        wanted = f" ({needs})" if needs else ""
        detail = (f"the geometry agent is not importable on this interpreter: {module}{wanted} "
                  f"raised {type(exc).__name__}: {exc}")
        if installed:
            pytest.fail(
                f"{detail}\n"
                f"This environment runs an INSTALLED meshpipeline, so it was BUILT - and a built "
                f"environment without the geometry agent has no Surveyor at all: every measurement, "
                f"look, survey and plan records only what it could not do, and the builder receives "
                f"the pre-Surveyor message. {ABSENT_ENV} is deliberately NOT honoured here.\n"
                f"The image installs the package from vendor/wheels/ in the Dockerfile's `wheel` "
                f"stage; if it is missing, that wheel was not committed or not copied.")
        if os.environ.get(ABSENT_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
            pytest.skip(f"{ABSENT_ENV} is set: {detail}")
        pytest.fail(
            f"{detail}\n"
            f"The Surveyor's measuring and looking half lives in the geometry-agent repository. To run "
            f"these tests, put its src/ on PYTHONPATH or `pip install` it.\n"
            f"If you genuinely do not have it, set {ABSENT_ENV}=1 and these tests will skip while the "
            f"rest of the suite runs. They are NOT skipped by default: a suite that quietly says "
            f"nothing about the Surveyor is how an image that shipped without it passed every check.")


__all__ = ["ABSENT_ENV", "require"]
