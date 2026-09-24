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
#
# AT WHAT GRANULARITY THE NOISE ARRIVES, which is the thing this got wrong the first time. Three
# modules call require() at MODULE SCOPE, as a gate over the whole file. Raising there is a COLLECTION
# ERROR, and pytest answers three collection errors with `Interrupted: 3 errors during collection` and
# stops: 7252 tests collected, ZERO executed, on every commit. The intent was a loud failure about the
# Surveyor and the effect was total silence about everything else - the same shape of defect as the
# skip it replaced, one level up. A test that says nothing because it skipped and a suite that says
# nothing because it never ran are the same lie told differently.
#
# So a module-scope call now FAILS EVERY TEST IN ITS OWN MODULE and touches nothing else. That is the
# right granularity: the tests that need the package say so, each in its own report, and the 7249 that
# do not need it still answer. The mechanism is a `pytestmark` and a fixture installed into the calling
# module, which is pytest's own way to spell "every test here", and the module body then imports
# cleanly because require() returns a stand-in that fails loudly if anything actually uses it.
from __future__ import annotations

import importlib
import os
import site
import sys
from pathlib import Path

import pytest

#: The developer's opt-out. Honoured in a checkout, ignored against an installed distribution.
ABSENT_ENV = "GEOMETRY_AGENT_ABSENT"

#: The fixture require() installs in a module that asked for a package this interpreter does not have.
#: Named rather than anonymous so the failure report says where the gate came from.
GATE_FIXTURE = "_the_geometry_agent_was_required_by_this_module"


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


class Absent:
    """What a module-scope `require()` returns when the package is not here.

    The module body has to carry on, because the alternative is the collection error that stopped the
    whole suite. So the name is bound to this, and it fails loudly the moment anything reads an
    attribute off it or calls it. Nothing silently becomes None.
    """

    def __init__(self, module: str, verdict: Verdict) -> None:
        self._module = module
        self._verdict = verdict

    def __getattr__(self, name: str) -> object:
        # INTROSPECTION IS NOT USE, and this distinction is load-bearing. pytest walks every module-level
        # object while collecting a file and reads `__bases__`, `__test__`, `__name__` and
        # `_pytestfixturefunction` off each one. Failing on those put the outcome back INSIDE collection -
        # the module ended as one collected skip instead of two failing tests, which is a quieter version
        # of the defect this stand-in exists to fix. Anything private or dunder therefore answers the way
        # an ordinary object does: it is simply not there.
        if name.startswith("_"):
            raise AttributeError(name)
        self._verdict.end(f"{self._module}.{name} was used and ")

    def __call__(self, *args: object, **kwargs: object) -> object:
        self._verdict.end(f"{self._module} was called and ")

    def __repr__(self) -> str:
        # Never raises: pytest prints the repr of module attributes while building a failure report,
        # and a repr that failed would replace the real reason with a second, confusing one.
        return f"<geometry agent absent: {self._module}>"


class Verdict:
    """What the absence of one agent module means here: skip it, or fail it, and with what words."""

    def __init__(self, *, outcome, message: str) -> None:
        self.outcome = outcome
        self.message = message

    def end(self, prefix: str = "") -> None:
        self.outcome(prefix + self.message)


def _verdict(module: str, needs: str, exc: BaseException) -> Verdict:
    """The decision, made once, whether it is delivered now or at the next test in this module."""
    wanted = f" ({needs})" if needs else ""
    detail = (f"the geometry agent is not importable on this interpreter: {module}{wanted} "
              f"raised {type(exc).__name__}: {exc}")
    if _meshpipeline_is_installed():
        return Verdict(outcome=pytest.fail, message=(
            f"{detail}\n"
            f"This environment runs an INSTALLED meshpipeline, so it was BUILT - and a built "
            f"environment without the geometry agent has no Surveyor at all: every measurement, "
            f"look, survey and plan records only what it could not do, and the builder receives "
            f"the pre-Surveyor message. {ABSENT_ENV} is deliberately NOT honoured here.\n"
            f"The image installs the package from vendor/wheels/ in the Dockerfile's `wheel` "
            f"stage; if it is missing, that wheel was not committed or not copied."))
    if os.environ.get(ABSENT_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return Verdict(outcome=pytest.skip, message=f"{ABSENT_ENV} is set: {detail}")
    return Verdict(outcome=pytest.fail, message=(
        f"{detail}\n"
        f"The Surveyor's measuring and looking half lives in the geometry-agent repository. To run "
        f"these tests, put its src/ on PYTHONPATH or `pip install` it.\n"
        f"If you genuinely do not have it, set {ABSENT_ENV}=1 and these tests will skip while the "
        f"rest of the suite runs. They are NOT skipped by default: a suite that quietly says "
        f"nothing about the Surveyor is how an image that shipped without it passed every check."))


def _called_at_module_scope() -> dict | None:
    """The calling module's globals when `require()` was called from a module body, else None.

    A module body's frame is the one pytest is inside while IMPORTING the file, so raising there ends
    collection for the whole session rather than the file. This is how that case is told apart from a
    call inside a test or a fixture, where raising is exactly right.
    """
    frame = sys._getframe(2)
    return frame.f_globals if frame.f_code.co_name == "<module>" else None


def _gate_the_module(module_globals: dict, verdict: Verdict) -> None:
    """Make every test in this module end with `verdict`, and leave the rest of the suite alone.

    A fixture defined in a test module is visible to that module's tests and to nothing else, so this
    is scoped exactly to the file that asked. `pytestmark` is pytest's own way to say "every test
    here", and it is extended rather than replaced: these modules carry their own marks.
    """
    def gate() -> None:
        # No parameters, deliberately: pytest reads a fixture's signature as a list of OTHER fixtures to
        # resolve, so a bound method with an optional argument would send it looking for a fixture named
        # after that argument and the report would be about the missing fixture instead of the package.
        verdict.end()

    module_globals[GATE_FIXTURE] = pytest.fixture(name=GATE_FIXTURE)(gate)
    mark = pytest.mark.usefixtures(GATE_FIXTURE)
    existing = module_globals.get("pytestmark")
    if existing is None:
        module_globals["pytestmark"] = [mark]
    elif isinstance(existing, list):
        module_globals["pytestmark"] = [*existing, mark]
    else:
        module_globals["pytestmark"] = [existing, mark]


def require(module: str, *, needs: str = "") -> object:
    """Import `module` from the geometry agent, or end the tests that need it loudly. Never skips silently.

    `needs` says what the caller wanted it for, so the failure reads as a missing capability rather
    than a missing import line.

    FROM A TEST OR A FIXTURE the outcome arrives immediately: this test fails, or skips if the
    developer's opt-out applies. FROM A MODULE BODY it arrives at every test in that module instead,
    because raising during collection stops the entire session and takes 7000 unrelated tests with it.
    Either way the words are the same and nothing goes quiet.
    """
    if not module.startswith("geometry_agent"):
        raise ValueError(f"{module!r} is not a geometry_agent module; this helper is only for the agent")
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        verdict = _verdict(module, needs, exc)
        module_globals = _called_at_module_scope()
        if module_globals is None:
            verdict.end()
            raise AssertionError("unreachable: the verdict always ends the test")  # pragma: no cover
        _gate_the_module(module_globals, verdict)
        return Absent(module, verdict)


__all__ = ["ABSENT_ENV", "GATE_FIXTURE", "Absent", "Verdict", "require"]
