# Responsibility: Decide, read-only, whether the running worker may be trusted to run the integration tier.
# Boundaries: it inspects Docker and git and changes neither; it never builds, creates, starts or replaces anything.
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The ONLY docker subcommands this module may issue. Both are pure queries. The list is enforced
# here rather than left as a convention, because the incident this module exists to prevent was a
# refusal PATH that mutated: the message warning about a stale image executed `make rebuild` while
# composing itself, and rebuilt and replaced four running services in the act of complaining.
# Nothing may run before the stamp is accepted except reads.
READ_ONLY_DOCKER = ("ps", "inspect")

STAMP = "MESH_SOURCE_TREE"
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


class Refused(Exception):
    pass


def _docker(*args: str) -> str:
    if args[0] not in READ_ONLY_DOCKER:
        raise AssertionError(
            f"preflight tried to run `docker {args[0]}`, which is not one of {READ_ONLY_DOCKER}. "
            "The inspection phase may only read.")
    done = subprocess.run(["docker", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise Refused(f"docker {' '.join(args)} failed:\n{done.stderr.strip()}")
    return done.stdout.strip()


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if done.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed:\n{done.stderr.strip()}")
    return done.stdout.strip()


def require_clean_tree() -> None:
    # The digest below is computed from the files on disk. Requiring a clean tree first is what
    # makes that the COMMITTED tree rather than a coincidence: with nothing modified, staged or
    # untracked-and-copied-into-the-image, the bytes hashed are the bytes of the commit under test.
    dirty = _git("status", "--porcelain")
    if dirty:
        raise Refused(
            "the tracked tree is dirty, so an image can only ever match it by accident.\n\n"
            f"{dirty}\n\n"
            "Commit or stash these changes, rebuild, and run the tier against the committed tree.")


def committed_tree_digest() -> str:
    # THE existing authority, not a second implementation. It refuses on its own terms (untracked
    # files inside a directory the Dockerfile copies), and those refusals are ours too.
    done = subprocess.run(["bash", str(ROOT / "tests/integration/source_digest.sh")],
                          capture_output=True, text=True, cwd=ROOT)
    if done.returncode != 0:
        raise Refused(f"the tracked-tree digest could not be computed:\n{done.stderr.strip()}")
    return done.stdout.strip()


def project_name() -> str:
    # The same rule Compose applies, evaluated here so that discovery never has to invoke Compose.
    # Asking Compose to name a container means asking it to load the project, and loading the
    # project is what lets it decide something is out of date and reconcile it.
    #
    # Compose v2 keeps dashes and underscores; only characters outside [a-z0-9_-] are dropped, and
    # the name must start with a letter or digit. Stripping dashes was the v1 rule, and under v2 it
    # made every checkout in a hyphenated directory - this one included - look at a project that
    # does not exist, so the tier refused to run at all.
    explicit = os.environ.get("COMPOSE_PROJECT_NAME", "").strip()
    if explicit:
        return explicit
    kept = "".join(c for c in ROOT.name.lower() if c.isalnum() or c in "_-")
    return kept.lstrip("_-")


def find_worker(project: str, service: str = "worker") -> str:
    # Labels, not names: the label is what Compose itself uses to own a container, and it survives
    # a rename. `docker ps` filters server-side and lists nothing else.
    found = _docker("ps", "--filter", f"label=com.docker.compose.project={project}",
                    "--filter", f"label=com.docker.compose.service={service}",
                    "--format", "{{.ID}}")
    ids = [line for line in found.splitlines() if line.strip()]
    if not ids:
        raise Refused(
            f"no running container is labelled as service '{service}' of Compose project "
            f"'{project}'.\n\n"
            "The tier runs INSIDE that container, so there is nothing to run in. Start the stack, "
            "or set COMPOSE_PROJECT_NAME if the project is named differently from this directory.")
    if len(ids) > 1:
        raise Refused(
            f"{len(ids)} running containers claim to be service '{service}' of project "
            f"'{project}': {', '.join(ids)}.\n\n"
            "Refusing to pick one. A tier that silently chooses among candidates proves nothing "
            "about the one you meant. Remove the duplicates, or name the project explicitly.")
    return ids[0]


def read_stamp(container: str) -> str | None:
    # From the container's recorded configuration - NOT `docker exec`. Exec would start a process
    # inside a container this phase has not yet decided to trust, which is not a read.
    env = _docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container)
    for line in env.splitlines():
        name, sep, value = line.partition("=")
        if sep and name == STAMP:
            return value
    return None


def check_stamp(container: str, want: str) -> None:
    got = read_stamp(container)
    if got is None:
        raise Refused(
            f"the running worker carries no {STAMP}, so nothing states which source it was built "
            "from.\n\n" + _rebuild_hint())
    if not got.strip():
        raise Refused(
            f"the running worker's {STAMP} is empty, so nothing states which source it was built "
            "from. An image built outside the supported path carries no proof of its source.\n\n"
            + _rebuild_hint())
    if not _DIGEST.match(got.strip()):
        raise Refused(
            f"the running worker's {STAMP} is not a tracked-tree digest: {got.strip()!r}.\n\n"
            + _rebuild_hint())
    if got.strip() != want:
        raise Refused(
            "the running worker was built from different source than the committed tree, so a "
            "green tier would prove nothing about the commit under test.\n\n"
            f"  committed tree : {want}\n"
            f"  image stamp    : {got.strip()}\n\n" + _rebuild_hint())


def _rebuild_hint() -> str:
    # Plain text. This function returns a STRING and runs nothing: the message that recommends a
    # rebuild must never be able to perform one. That is not a stylistic preference - a refusal
    # written as an unquoted shell heredoc once executed the very command it was naming.
    return ("Rebuild the application images from this checkout, then run the tier again:\n\n"
            "  make rebuild\n\n"
            "That target stamps the tracked-tree digest into each image. It rebuilds and restarts "
            "the application services only, and touches no database, no volume and no data "
            "service.")


def resolve() -> str:
    require_clean_tree()
    want = committed_tree_digest()
    worker = find_worker(project_name())
    check_stamp(worker, want)
    return worker


def main() -> int:
    try:
        worker = resolve()
    except Refused as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 1
    sys.stdout.write(worker + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
