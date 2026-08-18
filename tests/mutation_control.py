# Responsibility: Prove a named behavioural control depends on the implementation it names.
# Boundaries: it runs one controlled mutation and restores it; it asserts nothing about the domain.
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator


class MutationSurvived(AssertionError):
    # The mutation changed the implementation and NOTHING failed. That is the finding this
    # harness exists to surface: the control does not actually depend on what it claims to.
    pass


@contextlib.contextmanager
def substitute(target: object, attribute: str, replacement: object) -> Iterator[None]:
    # EXACTLY ONE controlled mutation, restored on every exit including an exception. Runtime
    # substitution rather than a source rewrite, so a failed run can never leave mutated
    # production source on disk.
    missing = object()
    original = getattr(target, attribute, missing)
    setattr(target, attribute, replacement)
    try:
        yield
    finally:
        if original is missing:                          # pragma: no cover - defensive
            delattr(target, attribute)
        else:
            setattr(target, attribute, original)


def prove(name: str, control: Callable[[], None],
          mutation: Callable[[], contextlib.AbstractContextManager],
          *, expecting: str) -> str:
    # The five steps, each running the control EXACTLY ONCE. No loop and no retry: an unbounded
    # mutation harness can hang a suite, and a retry would hide a control that fails only
    # sometimes.
    control()                                             # 1. normal implementation passes
    with mutation():                                      # 2. one mutation
        try:
            control()
        except (KeyboardInterrupt, SystemExit):           # pragma: no cover - never swallowed
            raise
        except BaseException as exc:                      # noqa: BLE001 - the observed failure
            observed = f"{type(exc).__name__}: {exc}"
        else:
            raise MutationSurvived(
                f"{name}: the mutation was applied and the control still passed, so the control "
                f"does not depend on it (expected a failure mentioning {expecting!r})")
    # 3. restored by the context manager on exit
    control()                                             # 4. passes again, so nothing leaked
    assert expecting in observed, (                       # 5. and it failed for the named reason
        f"{name}: the control failed for a different reason than the mutation predicts.\n"
        f"  expected to mention: {expecting!r}\n  observed: {observed}")
    print("MUTATION " + json.dumps({
        "mutation": name, "expected": expecting, "observed": observed[:400],
        "restored": True}), flush=True)
    return observed


def refuses(exception_type, call: Callable[[], object], detail: str) -> BaseException:
    # `pytest.raises` reports a non-raise as `Failed`, which is not an AssertionError; controls
    # here must fail as assertions so a mutation's effect is unambiguous.
    try:
        result = call()
    except exception_type as exc:
        return exc
    except Exception as exc:                              # noqa: BLE001 - the wrong refusal
        raise AssertionError(f"{detail}: refused with {type(exc).__name__}, not "
                             f"{exception_type.__name__}: {exc}") from None
    raise AssertionError(f"{detail}: nothing was refused, it returned {result!r}")
