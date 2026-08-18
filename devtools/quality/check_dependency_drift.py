#!/usr/bin/env python3
# Responsibility: Prove there is one dependency source of truth, and no installer pins beside it.
# Boundaries: a repository gate; it installs nothing and edits nothing.
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQ = ROOT / "requirements" / "runtime.txt"
REQ_DEV = ROOT / "requirements" / "dev.txt"

#: THE transitive constraint authority. Direct pins alone are not reproducible - one commit
#: resolved different cffi, langsmith, Mako and websockets versions across rebuilds - so every
#: command that asks the RESOLVER for project dependencies must be handed this file.
CONSTRAINTS = "requirements/constraints.txt"

# Files that install dependencies and must do it only via a requirements file. Discovery below
# walks every tracked file, so this list is only the inline-pin subject; a new installer in a new
# file is still found.
_INSTALLERS = [
    ".github/workflows/ci.yml",
    "Dockerfile",
    "Makefile",
    "devtools/env/setup.sh",
]

#: A pip invocation, however it is spelled: `pip install`, `python -m pip install`,
#: `"${VPY}" -m pip install`, `/path/to/venv/bin/pip install`.
_PIP_ANY = re.compile(r"""(?:[\w"'${}/.\-]*\bpip3?["']?\s+install|
                            \bpip3?\s+install)""", re.X)

#: Requirement-group files whose installation asks the resolver to solve project dependencies.
_PROJECT_REQS = ("requirements/runtime.txt", "requirements/dev.txt")


class Unavailable(RuntimeError):
    # The check could not be PERFORMED. Distinct from a drift failure, which is a real verdict about
    # real files; this says no verdict was reached at all.
    pass


def _install_commands() -> list[tuple[str, int, str]]:
    # Every pip installation in the tracked tree, found rather than listed - a new installer in a
    # file nobody thought to enumerate is exactly what a handwritten list misses.
    import subprocess
    # The tracked file list IS the subject here, so a git that cannot answer is a refusal and never
    # an empty result: a scan of nothing would report OK for every rule below, which is the one
    # outcome worse than failing.
    try:
        done = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    except OSError as exc:
        raise Unavailable(f"git could not be run: {exc.strerror or exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise Unavailable(f"git ls-files failed (exit {done.returncode}): "
                          f"{detail[0] if detail else 'no output'}")
    tracked = done.stdout.split()
    if not tracked:
        raise Unavailable("git reported no tracked files at all")
    out = []
    for rel in tracked:
        path = ROOT / rel
        if not path.is_file() or path.suffix in (".png", ".whl"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _PIP_ANY.search(line):
                out.append((rel, i, line.strip()))
    return out


def _resolves_project_dependencies(line: str) -> bool:
    # It asks the resolver for OUR dependency groups. `--no-deps` installs an artefact without
    # resolving; a bare tool bootstrap names no requirement file.
    if "--no-deps" in line:
        return False
    return any(req in line for req in _PROJECT_REQS)


def _is_bootstrap(line: str) -> bool:
    # Packaging tooling only - pip/setuptools/wheel - which must be installed BEFORE any project
    # requirement can be resolved, and whose versions are pinned as build args where it matters.
    return bool(re.search(r"install\s+(--\S+\s+)*--upgrade\s+[\"']?(pip|\$\{PIP_VERSION)", line)) \
        or bool(re.search(r'--upgrade\s+"?pip', line))
# `pip install foo==1.2` / `pip install foo>=1.2` - an inline PIN. Bare `pip install build`
# without a version is still drift (unpinned), so match a package token that is not a flag,
# a path, or a `-r` file reference.
_PIP = re.compile(r"pip\s+install\s+((?:--?[\w-]+(?:=\S+)?\s+)*)([^\n&|;]*)")


def _pinned_names(path: Path) -> set[str]:
    names = set()
    if not path.exists():
        return names
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.add(re.split(r"[=<>!~\[]", line, maxsplit=1)[0].strip().lower())
    return names


def main() -> int:
    problems: list[str] = []

    # Prove the scan CAN be performed before reporting anything about what it found. Outside a Git
    # checkout - an unpacked archive, a copied directory, a COPY'd image layer - there is no tracked
    # file list, and the honest answer is that this gate does not apply, said in one line rather
    # than as a traceback from a subprocess the operator never invoked.
    try:
        _install_commands()
    except Unavailable as why:
        print("CANNOT CHECK: no verdict was reached on dependency drift\n")
        print(f"  - {why}\n")
        print("  This gate reads the TRACKED file list, so it needs a Git checkout of the "
              "repository.\n"
              "  Run `make dependencies` from a checkout of the project. A directory copy or an\n"
              "  unpacked archive carries the files but not the tracking, so there is nothing to "
              "scan.")
        return 2

    if not REQ.exists():
        problems.append("requirements/runtime.txt is missing - there is no runtime source of truth")
    if not REQ_DEV.exists():
        problems.append("requirements/dev.txt is missing - the toolchain has no home")

    # 1. No installer may pin a version inline.
    for rel in _INSTALLERS:
        p = ROOT / rel
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if "pip install" not in line:
                continue
            m = _PIP.search(line)
            if not m:
                continue
            args = m.group(2)
            if "-r " in line or "requirements" in line:
                continue                       # installs from a requirements file: correct
            # allow the package itself and pip's own bootstrap
            if re.search(r"(-e\s+\.|/tmp/\*\.whl|\s\.\s*$|--upgrade pip)", line):
                continue
            if re.search(r"[\w\-]+\s*[=<>~]=", args):
                problems.append(
                    f"{rel}:{i}: pins a version inline - move it to requirements/dev.txt so the "
                    f"toolchain has one source of truth:\n      {line.strip()}")

    # 1b. Every command that resolves PROJECT dependencies must be handed the constraints file.
    #     Discovery walks the tracked tree, so a new installer cannot arrive unnoticed.
    seen_resolvers = 0
    for rel, i, line in _install_commands():
        if _is_bootstrap(line) or "--no-deps" in line:
            continue                            # proven exemptions: bootstrap, artefact install
        if not _resolves_project_dependencies(line):
            continue
        seen_resolvers += 1
        if CONSTRAINTS not in line:
            problems.append(
                f"{rel}:{i}: resolves project dependencies without {CONSTRAINTS}, so this "
                f"environment can drift from the one the images install:\n      {line}")
            continue
        # a second constraint file is a second authority
        others = [m for m in re.findall(r"-c\s+(\S+)", line) if CONSTRAINTS not in m]
        if others:
            problems.append(
                f"{rel}:{i}: constrains resolution with {others}, not the one authority "
                f"{CONSTRAINTS}")
    if seen_resolvers == 0:
        problems.append(
            "no project-dependency installer was discovered at all - the scan found nothing to "
            "check, so whatever it asserts is vacuous")

    # 2. The two files must not both pin the same package.
    overlap = _pinned_names(REQ) & _pinned_names(REQ_DEV)
    if overlap:
        problems.append(
            f"requirements/runtime.txt and requirements/dev.txt both pin: {sorted(overlap)} - one "
            "of them will drift")

    # 3. pyproject must not start declaring runtime pins beside requirements/runtime.txt.
    pyproject = (ROOT / "pyproject.toml").read_text()
    if re.search(r"^dependencies\s*=\s*\[[^\]]*[=<>~]=", pyproject, re.M | re.S):
        problems.append(
            "pyproject.toml declares pinned runtime dependencies, duplicating "
            "requirements/runtime.txt - pick one source of truth")

    if problems:
        print("FAIL: dependency drift\n")
        for p in problems:
            print(f"  - {p}")
        print("\nOne pinned environment: requirements/runtime.txt (runtime + test), "
              "requirements/dev.txt (tooling).")
        return 1

    resolvers = [c for c in _install_commands()
                 if _resolves_project_dependencies(c[2]) and not _is_bootstrap(c[2])
                 and "--no-deps" not in c[2]]
    print(f"OK: single dependency source of truth "
          f"({len(_pinned_names(REQ))} runtime/test pins, {len(_pinned_names(REQ_DEV))} tooling pins, "
          f"0 inline pins in {len(_INSTALLERS)} installers); "
          f"{len(resolvers)} project-dependency installer(s), all constrained by {CONSTRAINTS}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
