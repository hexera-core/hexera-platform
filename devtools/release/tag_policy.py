# Responsibility: Decide whether a commit may be promoted, from its tree, its tags and its gate evidence.
# Boundaries: it reads git and the release record; it creates no tag, publishes nothing and never contacts a remote.
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: git subcommands this module may run. All are queries. Anything that writes a ref, or that could
#: reach the network, is absent by construction rather than by care.
READ_ONLY_GIT = ("status", "rev-parse", "cat-file", "for-each-ref", "tag")

#: `git tag` is a query ONLY in its listing forms. Creating, deleting or moving a tag is not this
#: module's business, and neither is any form that takes a remote.
FORBIDDEN_GIT_ARGS = ("push", "fetch", "pull", "remote", "clone", "ls-remote",
                      "-a", "-s", "-m", "-d", "-f", "--delete", "--force", "--annotate", "--sign")


class Refused(Exception):
    pass


def _git(*args: str, repo: Path | None = None) -> str:
    if args[0] not in READ_ONLY_GIT:
        raise AssertionError(
            f"tag policy tried to run `git {args[0]}`, which is not one of {READ_ONLY_GIT}. "
            "Readiness validation reads; it never writes a ref and never reaches a remote.")
    bad = [a for a in args[1:] if a in FORBIDDEN_GIT_ARGS]
    if bad:
        raise AssertionError(
            f"tag policy tried to run `git {' '.join(args)}`, which would write or publish "
            f"({', '.join(bad)}). Creating and pushing the tag is a separate human action.")
    done = subprocess.run(["git", *args], cwd=str(repo or REPO_ROOT), capture_output=True, text=True)
    if done.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def check_clean_tree(repo: Path | None = None) -> None:
    dirty = _git("status", "--porcelain", repo=repo)
    if dirty:
        raise Refused("the promotion commit must have a clean tree, but this one has "
                      f"uncommitted changes:\n{dirty}")


def check_tag_is_annotated(tag: str, repo: Path | None = None) -> None:
    # An annotated tag is its own object with an author, a date and a message; a lightweight tag is
    # a bare pointer that anyone can move without leaving a trace of who or when.
    kind = _git("cat-file", "-t", tag, repo=repo)
    if kind != "tag":
        raise Refused(
            f"{tag!r} is a {kind} - a lightweight tag. A release tag must be annotated, so that it "
            "records who made it and when, and so that moving it is visible.")


def check_tag_is_new(tag: str, repo: Path | None = None) -> None:
    # Published release tags are immutable. Reusing one silently changes what a version means for
    # everybody who already fetched it.
    existing = _git("for-each-ref", "--format=%(refname:short)", "refs/tags", repo=repo).split()
    if tag in existing:
        raise Refused(
            f"tag {tag!r} already exists. Release tags are immutable: create the next version "
            "rather than moving this one.")


def check_gates_proven(record: dict | None) -> None:
    # Reuse the release record's own authority rather than re-deciding what "validated" means.
    from devtools.release.record import promotability

    if record is None:
        raise Refused(
            "no Gate C release record was supplied, so nothing states that this commit was "
            "validated. Promotion may not run while the required gates are unproven.")
    promotable, problems = promotability(record)
    if not promotable:
        raise Refused("the release record does not permit promotion:\n  " + "\n  ".join(problems))


def readiness(tag: str, *, record: dict | None = None,
              repo: Path | None = None) -> list[str]:
    # Report EVERY reason rather than the first, so an operator learns the whole distance to a
    # promotable state in one run. This function creates nothing: no tag, no ref, no publication,
    # no network call. It is safe to run on any tree, including one that is nowhere near ready.
    # It judges the tree and the evidence, never the shape or value of a version string.
    refusals: list[str] = []
    for probe in (
        lambda: check_clean_tree(repo),
        lambda: check_tag_is_new(tag, repo),
        lambda: check_gates_proven(record),
    ):
        try:
            probe()
        except Refused as exc:
            refusals.append(str(exc))
    return refusals
