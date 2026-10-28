#!/usr/bin/env python3
# Responsibility: Decide which CI lanes a changed path set needs.
# Owns: conservative path classification only; git diffing stays in the workflow.
# Boundaries: it does not inspect file contents and never runs checks itself.
from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    docs_only: bool
    full_ci: bool
    run_python: bool
    run_images: bool
    run_web: bool


WEB_PATHS = {
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.base.json",
}

PYTHON_PATHS = {
    "Dockerfile",
    "Makefile",
    "alembic.ini",
    "docker-compose.yml",
    "pyproject.toml",
}

IMAGE_PATHS = {
    "Dockerfile",
    "Makefile",
    "alembic.ini",
    "deploy/verify_install.py",
    "devtools/quality/shard_tests.py",
    "docker-compose.yml",
    "pyproject.toml",
}


def _is_doc(path: str) -> bool:
    return path.endswith(".md") or path.startswith("docs/")


def _is_web(path: str) -> bool:
    return path in WEB_PATHS or path.startswith(("apps/", "packages/"))


def _is_python(path: str) -> bool:
    return (
        path in PYTHON_PATHS
        or path.startswith((
            "alembic/",
            "deploy/",
            "devtools/",
            "requirements/",
            "src/",
            "tests/integration/",
            "tests/native/",
            "tests/unit/",
            "tests/ui/",
            "ui/",
        ))
    )


def _needs_images(path: str) -> bool:
    return (
        path in IMAGE_PATHS
        or path.startswith((
            "alembic/",
            "deploy/docker/",
            "requirements/",
            "src/",
            "tests/integration/",
            "tests/native/",
            "tests/ui/",
            "ui/",
        ))
    )


def classify(paths: list[str], *, event_name: str = "") -> Scope:
    """Return the CI lane set needed for ``paths``.

    Empty input means the workflow could not establish a diff, so every selectable lane runs.
    Unknown paths do the same. A missed lane is worse than a slow lane.
    """
    full_ci = event_name != "pull_request"
    if not paths:
        return Scope(docs_only=False, full_ci=full_ci, run_python=True, run_images=True,
                     run_web=True)

    if all(_is_doc(path) for path in paths):
        return Scope(docs_only=True, full_ci=full_ci, run_python=False, run_images=False,
                     run_web=False)

    run_python = False
    run_images = False
    run_web = False

    for path in paths:
        if _is_doc(path):
            continue
        known = False
        if _is_web(path):
            run_web = True
            known = True
        if _is_python(path):
            run_python = True
            known = True
        if _needs_images(path):
            run_images = True
            known = True
        if path.startswith(("tests/integration/", "tests/native/")):
            known = True
        if not known:
            return Scope(docs_only=False, full_ci=full_ci, run_python=True, run_images=True,
                         run_web=True)

    return Scope(
        docs_only=False,
        full_ci=full_ci,
        run_python=run_python,
        run_images=run_images,
        run_web=run_web,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    event_name = ""
    if args[:1] == ["--event"]:
        try:
            event_name = args[1]
        except IndexError:
            print("--event requires a value", file=sys.stderr)
            return 2
        args = args[2:]
    scope = classify(args, event_name=event_name)
    for key in ("docs_only", "full_ci", "run_python", "run_images", "run_web"):
        value = getattr(scope, key)
        print(f"{key}={'true' if value else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
