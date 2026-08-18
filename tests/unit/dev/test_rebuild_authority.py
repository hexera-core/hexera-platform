# Responsibility: Verify the rebuild command the runner recommends exists and stamps the working-tree digest.
# Boundaries: the rebuild contract - whether an image then passes the preflight is the integration tier's own proof.
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
MAKEFILE = (REPO / "Makefile").read_text()
COMPOSE_TEXT = (REPO / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
RUNNER = (REPO / "tests" / "integration" / "run_disposable.sh").read_text()
PREFLIGHT = (REPO / "tests" / "integration" / "preflight.py").read_text()
DIGEST_SCRIPT = REPO / "tests" / "integration" / "source_digest.sh"

#: Every application image whose freshness the integration preflight checks. Each must receive the
#: digest, or a rebuild would leave one of them unstamped and silently older than the checkout.
REVISION_CHECKED = ("api", "worker", "worker-utility", "beat")
BUILD_ARG = "MESH_SOURCE_TREE"


def _preflight_block() -> str:
    # The decision moved out of the shell script and into preflight.py, because a refusal written
    # as an unquoted heredoc executed the rebuild it was recommending. It is still ONE authority,
    # so this reads that one - located by the function's words, not by surrounding decoration.
    start = PREFLIGHT.index("def check_stamp")
    return PREFLIGHT[start:PREFLIGHT.index("def _rebuild_hint", start)]


def _stale_message() -> str:
    start = PREFLIGHT.index("def check_stamp")
    return PREFLIGHT[start:PREFLIGHT.index("def resolve", start)]


def _recipe(target: str) -> str:
    body = MAKEFILE[MAKEFILE.index(f"\n{target}:") + 1:]
    rest = re.search(r"\n(?=[A-Za-z0-9_.-]+:)", body)
    return body[:rest.start()] if rest else body


def _make_targets() -> set[str]:
    return set(re.findall(r"^([A-Za-z0-9_.-]+):(?![=])", MAKEFILE, re.MULTILINE))


# Make commands the runner actually TELLS an operator to run: backticked `make foo`, and `make foo`
# alone on its own indented line. Prose that merely contains the word "make" is not a recommendation.
def _recommended_targets() -> list[str]:
    # Backticks inside the script's double-quoted strings are backslash-escaped; drop the
    # escapes first so an operator-facing `make foo` is found in either form.
    text = RUNNER.replace("\\`", "`")
    backticked = re.findall(r"`make ([a-z][a-z0-9-]*)`", text)
    standalone = re.findall(r"^\s*make ([a-z][a-z0-9-]*)\s*$", text, re.MULTILINE)
    return sorted(set(backticked) | set(standalone))


# the recommendation must name something that exists


def test_the_runner_recommends_at_least_one_make_command():
    assert _recommended_targets(), "the runner no longer tells an operator what to run"


def test_every_make_command_the_runner_recommends_exists():
    missing = [t for t in _recommended_targets() if t not in _make_targets()]
    assert missing == [], (
        f"the runner tells operators to run {missing}, which the Makefile does not define. "
        "Stale operational prose is how an operator ends up inventing their own rebuild - "
        "`make build && make up` were recommended for a while and neither ever existed.")


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_every_recommended_command_resolves_without_running_anything():
    for target in _recommended_targets():
        r = subprocess.run(["make", "-n", target], cwd=REPO, capture_output=True, text=True)
        assert r.returncode == 0, (
            f"`make -n {target}` failed, so the recommendation cannot be followed:\n{r.stderr}")


def test_the_runner_points_at_the_rebuild_target():
    assert "make rebuild" in RUNNER, \
        "the stale-image guidance no longer names the supported rebuild target"


# the rebuild must be safe


def test_the_rebuild_target_tears_nothing_down():
    recipe = _recipe("rebuild")
    for forbidden, why in (
            ("compose down", "recreates postgres, redis and minio - protected services"),
            ("-v", "would remove data volumes"),
            ("--rmi", "would remove images"),
            ("prune", "would sweep resources it does not own"),
            ("--no-cache", "would rebuild the whole native floor for a source change")):
        assert forbidden not in recipe, f"`make rebuild` contains {forbidden!r}: it {why}"


def test_the_rebuild_target_names_only_the_application_services():
    recipe = _recipe("rebuild")
    for data_service in ("postgres", "redis", "minio", "searxng"):
        assert data_service not in recipe, \
            f"`make rebuild` names {data_service}; data services must not be recreated"
    for app in REVISION_CHECKED:
        assert app in recipe, f"`make rebuild` no longer rebuilds {app}"


def test_the_rebuild_target_uses_no_source_bind_mount():
    recipe = _recipe("rebuild")
    assert "-v " not in recipe and ":/srv/src" not in recipe, \
        "a source bind mount would make a stale image LOOK current without being rebuilt"


# one digest authority, computed automatically


def test_the_digest_script_is_the_only_implementation():
    assert DIGEST_SCRIPT.is_file()
    # No second algorithm may appear in the runner, Make, Compose or a test: they must all call it.
    for name, text in (("Makefile", MAKEFILE), ("run_disposable.sh", RUNNER),
                       ("docker-compose.yml", COMPOSE_TEXT)):
        if "sha256sum" in text or "hash-object" in text:
            assert "source_digest.sh" in text, \
                f"{name} computes a digest of its own instead of calling source_digest.sh"


def test_the_rebuild_target_computes_the_digest_itself():
    recipe = _recipe("rebuild")
    assert BUILD_ARG in recipe, f"`make rebuild` no longer supplies {BUILD_ARG}"
    assert "SOURCE_DIGEST" in recipe, "`make rebuild` no longer computes the digest"
    assert "source_digest.sh" in MAKEFILE, "the Makefile no longer calls the digest authority"


def test_the_staleness_check_needs_a_digest_before_it_may_skip_a_build():
    # The digest script REFUSES rather than guesses - untracked content inside a copied directory
    # is one such refusal - and $(shell) captures only stdout, so a refusal reaches Make as the
    # empty string. An unstamped image reads as the empty string too, and the two compared equal:
    # the tree the digest would not describe was the tree that skipped its rebuild.
    recipe = _recipe("dev-images")
    assert '[ -n "$$digest" ]' in recipe, \
        "dev-images no longer requires a digest before it may skip a build"
    assert '[ -n "$$stamp" ]' in recipe, \
        "dev-images no longer requires the image to carry a stamp"
    assert '[ "$$stamp" = "$$digest" ]' in recipe, \
        "dev-images no longer compares the stamp against the digest it captured"


def test_an_image_built_by_the_build_target_carries_the_stamp_it_is_judged_by():
    # Nothing set the value the staleness check reads, so the comparison had an empty side no
    # matter what the tree contained.
    recipe = _recipe("dev-build")
    assert BUILD_ARG in recipe, f"`make dev-build` does not supply {BUILD_ARG}"
    assert "SOURCE_DIGEST" in recipe, "`make dev-build` does not stamp the working-tree digest"


def test_the_digest_is_not_derived_from_a_commit():
    # Executable lines only - the script's header legitimately EXPLAINS why HEAD^{tree} is wrong.
    body = "\n".join(ln for ln in DIGEST_SCRIPT.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    for forbidden in ("rev-parse HEAD", "HEAD^{tree}", "git write-tree", "diff-index"):
        assert forbidden not in body, (
            f"the digest uses {forbidden!r}; a commit-derived value cannot see uncommitted edits, "
            "which is exactly the case that let a stale image be tested against")


# compose wiring


def _build_args(service: str) -> dict:
    return (COMPOSE["services"][service].get("build") or {}).get("args") or {}


@pytest.mark.parametrize("service", REVISION_CHECKED)
def test_every_revision_checked_image_receives_the_digest(service):
    args = _build_args(service)
    assert BUILD_ARG in args, (
        f"{service} does not receive {BUILD_ARG}, so a rebuild stamps it with nothing and the "
        "integration preflight will refuse it")
    assert "MESH_SOURCE_TREE" in str(args[BUILD_ARG]), \
        f"{service}'s {BUILD_ARG} does not read the environment variable the rebuild exports"


def test_the_build_arguments_are_shared_rather_than_duplicated():
    assert "x-app-build-args" in COMPOSE_TEXT and "*app-build-args" in COMPOSE_TEXT, (
        "the build arguments were copied per service; divergent copies are how one image ends up "
        "unstamped while the others look fine")
    resolved = {s: _build_args(s) for s in REVISION_CHECKED}
    assert len({str(sorted(v.items())) for v in resolved.values()}) == 1, \
        f"the revision-checked images disagree about their build arguments: {resolved}"


def test_an_unset_digest_does_not_become_the_word_unknown():
    args = _build_args("worker")
    assert "unknown" not in str(args[BUILD_ARG]).lower(), (
        "Compose would substitute the literal 'unknown', which reads like a real value. An unset "
        "digest must produce an EMPTY stamp that the preflight refuses.")
    assert re.search(r"\$\{MESH_SOURCE_TREE-\}", str(args[BUILD_ARG])), \
        "the default must be empty, so an unstamped image is refused rather than trusted"


def test_the_preflight_still_refuses_an_unstamped_image():
    # The guard compares for equality, so "", "unknown" and a stale digest are all refused. This
    # test exists so nobody "fixes" a refusal by special-casing one of them.
    block = _preflight_block()
    # Pin the WHOLE condition, not a blacklist of known weakenings. A blacklist misses the next
    # exemption someone invents - `&& [ "$GOT_TREE" != "unknown" ]` slipped straight past one.
    conditions = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("if ")]
    assert 'if got.strip() != want:' in conditions, (
        "the equality check against the committed-tree digest is gone, so a stale image could be "
        f"accepted. The conditions are: {conditions}")
    for condition in conditions:
        assert " or " not in condition and "unknown" not in condition, (
            f"the refusal grew an exemption clause: {condition!r}. A blacklist misses the next "
            "exemption someone invents - `!= \'unknown\'` slipped straight past one.")
    for unusable in ("if got is None:", "if not got.strip():", "if not _DIGEST.match(got.strip()):"):
        assert unusable in conditions, (
            f"the refusal no longer covers {unusable!r}; an absent, empty or malformed stamp must "
            "be refused exactly as a stale one is")


def test_the_runner_never_lets_compose_rebuild_behind_the_check():
    # `docker compose <cmd>` re-resolves the project and may build and recreate services. Doing
    # that after the freshness check would make a stale image current mid-run - the check would
    # pass and then be invalidated by the runner itself.
    offenders = [ln.strip() for ln in RUNNER.splitlines()
                 if "docker compose" in ln and not ln.lstrip().startswith("#")
                 and not re.search(r"docker compose (ps|exec) ", ln)]
    assert offenders == [], (
        "the runner uses a compose command that can build or recreate a service: " + str(offenders))


def test_the_failure_guidance_leaks_nothing():
    block = _stale_message()
    for secret in ("DATABASE_URL", "POSTGRES_PASSWORD", "$ADMIN", "$TASK", "printenv", "env |"):
        assert secret not in block, f"the stale-image message would print {secret}"
    assert "committed tree" in block and "image stamp" in block
    assert "run the tier again" in _rebuild_hint_text(), \
        "the guidance does not tell the operator to rerun the integration command"
    assert "`" not in block, (
        "the refusal text contains a backtick. In an unquoted shell heredoc that is a command "
        "substitution, which is precisely how this message once rebuilt four running services.")


def _rebuild_hint_text() -> str:
    start = PREFLIGHT.index("def _rebuild_hint")
    return PREFLIGHT[start:PREFLIGHT.index("def resolve", start)]
