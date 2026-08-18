# Responsibility: Keep every external build/deployment acquisition immutably identified and content-verified.
# Owns: the acquisition discovery rules and the per-class lock assertions.
# Boundaries: it derives the inventory from the tracked tree and fetches nothing.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "docker-compose.yml"
CI = REPO / ".github/workflows/ci.yml"

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256_LITERAL = re.compile(r"\b[0-9a-f]{64}\b")

#: Hosts whose traffic is a runtime service call, never a build input.
RUNTIME_HOSTS = ("deepseek.com", "deepinfra.com", "googleapis.com", "google.internal",
                 "tavily.com", "localhost", "127.0.0.1", "example.com")


def tracked(*paths: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", *paths], cwd=REPO, capture_output=True,
                         text=True).stdout.split()
    assert out, f"git ls-files {paths} returned nothing - the scan subject is empty"
    return out


def read(p: Path) -> str:
    assert p.is_file(), f"{p} is missing - the scan subject is empty"
    return p.read_text(errors="replace")


def image_references() -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []

    dockerfile = read(DOCKERFILE)
    stages = {m.group(1).lower()
              for m in re.finditer(r"^\s*FROM\s+\S+\s+AS\s+(\S+)", dockerfile, re.I | re.M)}
    for m in re.finditer(r"^\s*FROM\s+(\S+)", dockerfile, re.I | re.M):
        ref = m.group(1)
        if ref.lower() in stages or ref.lower() == "scratch":
            continue
        refs.append(("Dockerfile", ref))

    for m in re.finditer(r"^\s*image:\s*['\"]?(\S+?)['\"]?\s*$", read(COMPOSE), re.M):
        refs.append(("docker-compose.yml", m.group(1)))

    for m in re.finditer(r"docker\s+run\s+(?:[-\w=.]+\s+)*([\w./-]+:[\w.@:-]+)", read(CI)):
        refs.append((".github/workflows/ci.yml", m.group(1)))

    assert refs, "no image references discovered - the scan subject is empty"
    return refs


def run_blocks() -> list[tuple[int, str]]:
    lines = read(DOCKERFILE).splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("RUN"):
            start, chunk = i, [lines[i]]
            while lines[i].rstrip().endswith("\\") and i < len(lines) - 1:
                i += 1
                chunk.append(lines[i])
            blocks.append((start + 1, "\n".join(chunk)))
        i += 1
    assert blocks, "no RUN blocks parsed - the scan subject is empty"
    return blocks


def arg_values() -> dict[str, str]:
    return {m.group(1): m.group(2).strip('"\'')
            for m in re.finditer(r"^ARG\s+(\w+)=(\S+)", read(DOCKERFILE), re.M)}


def expand_args(text: str) -> str:
    args = arg_values()
    for _ in range(4):
        expanded = re.sub(r"\$\{(\w+)\}", lambda m: args.get(m.group(1), m.group(0)), text)
        if expanded == text:
            break
        text = expanded
    return text


def curl_downloads() -> list[tuple[int, str, str]]:
    out = []
    for lineno, raw_block in run_blocks():
        if not re.search(r"\b(?:curl|wget)\b", raw_block):
            continue
        block = expand_args(raw_block)
        for m in re.finditer(r"(https?://[^\s\"'\\]+)", block):
            url = m.group(1)
            if url.rstrip("/").endswith((".com", ".org", ".net")):
                continue                       # a bare host in prose, not a fetched artifact
            out.append((lineno, url, block))
    return out


# OCI
def test_every_third_party_image_is_pinned_by_digest():
    unpinned = [f"{src}: {ref}" for src, ref in image_references() if not DIGEST.search(ref)]
    assert unpinned == [], f"images without an immutable digest: {unpinned}"


def test_no_image_resolves_through_a_floating_or_latest_tag():
    bad = []
    for src, ref in image_references():
        if not DIGEST.search(ref):
            bad.append(f"{src}: {ref} (no digest)")
            continue
        tag_part = ref.split("@", 1)[0]
        tag = tag_part.rsplit(":", 1)[-1] if ":" in tag_part.rsplit("/", 1)[-1] else ""
        if tag == "latest":
            bad.append(f"{src}: {ref} (tag 'latest' beside the digest is misleading)")
    assert bad == [], bad


# direct downloads
def test_every_direct_download_is_content_verified():
    downloads = curl_downloads()
    assert downloads, "no downloads discovered in the Dockerfile - the scan subject is empty"
    assert any("micromamba" in u for _l, u, _b in downloads), (
        "the micromamba download was not discovered - the scan is blind to continuation lines")

    for lineno, url, block in downloads:
        if any(h in url for h in RUNTIME_HOSTS):
            continue
        # An archive or script must be CHECKSUMMED. A version or fingerprint check is a different
        # assertion - it says what the artifact claims to be, not that its bytes are the reviewed
        # ones - and must never satisfy this on its own.
        is_key = url.endswith((".gpg", ".asc"))
        if is_key:
            assert "grep -qx" in block or "--fingerprint" in block, (
                f"Dockerfile:{lineno} fetches key {url} without a fingerprint check")
            continue

        assert "sha256sum -c" in block, (
            f"Dockerfile:{lineno} downloads {url} with no content verification "
            "(sha256sum) in its RUN block")

        # A block-level check is not enough once a block fetches more than one artifact: a single
        # surviving sha256sum would vouch for a download whose own checksum had been dropped. Tie
        # each download to a verification of ITS OWN output path.
        segment = next((s for s in block.split("curl ") if url in s), "")
        target = re.search(r"-o\s+(\S+)", segment)
        if target:
            path = target.group(1).strip('"\'')
            verified = re.findall(r"sha256sum -c", block)
            assert any(path in line for line in block.splitlines() if "sha256sum -c" in line), (
                f"Dockerfile:{lineno} downloads {url} to {path}, but no sha256sum -c line in the "
                f"block verifies that path ({len(verified)} checksum(s) present, none for it)")


def test_no_download_is_piped_straight_into_a_shell():
    offenders = [f"Dockerfile:{i}" for i, line in enumerate(read(DOCKERFILE).splitlines(), 1)
                 if not line.strip().startswith("#")
                 and re.search(r"(?:curl|wget)[^\n|]*\|\s*(?:ba)?sh\b", line)]
    assert offenders == [], f"download piped into a shell: {offenders}"


def test_release_assets_use_a_build_specific_url_not_a_version_redirector():
    text = read(DOCKERFILE)
    # EXECUTABLE lines only. The comments deliberately name the old redirector to explain why it
    # was replaced; a guard that reads prose would forbid documenting the very defect it guards.
    executable = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    assert "micro.mamba.pm/api/micromamba" not in executable, (
        "the micromamba version redirector is back; use the immutable release asset")
    assert "micromamba-releases/releases/download/" in text, (
        "micromamba must resolve through an immutable release-asset URL")
    assert re.search(r"ARG MICROMAMBA_RELEASE=\d+\.\d+\.\d+-\d+", text), (
        "the micromamba RELEASE identity (build-specific, e.g. 2.8.1-1) is not declared")


def test_every_declared_checksum_is_a_full_sha256_literal():
    text = read(DOCKERFILE)
    args = re.findall(r"^ARG\s+(\w*SHA256\w*)=(\S+)", text, re.M)
    assert args, "no checksum ARGs found - the scan subject is empty"
    for name, value in args:
        assert SHA256_LITERAL.fullmatch(value), f"{name} is not a literal SHA-256: {value}"
        assert "$" not in value, f"{name} is computed rather than reviewed"


def test_signing_keys_are_verified_by_complete_fingerprint():
    text = read(DOCKERFILE)
    if "pubkey.gpg" not in text:
        pytest.skip("no key acquisition in the build")
    fprs = re.findall(r"^ARG\s+\w*FPR\w*=([0-9A-Fa-f]+)", text, re.M)
    assert fprs, "a signing key is fetched but no fingerprint is declared"
    for f in fprs:
        assert len(f) == 40, f"fingerprint {f} is not a complete 40-character fingerprint"


# Python
def test_the_python_build_toolchain_is_pinned():
    text = read(DOCKERFILE)
    m = re.search(r"pip install --upgrade([^\n\\]*(?:\\\n[^\n\\]*)*)", text)
    assert m, "no pip bootstrap found - the scan subject is empty"
    body = m.group(1)
    for tool in ("pip", "setuptools", "wheel"):
        assert re.search(rf'{tool}==\$\{{\w+\}}|{tool}==[\d.]+', body), (
            f"the pip bootstrap does not pin {tool}: {body.strip()[:120]}")


def test_runtime_requirements_are_exact_pins_without_ranges():
    text = (REPO / "requirements/runtime.txt").read_text()
    pins, ranges = 0, []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if re.match(r"^[A-Za-z0-9_.\-\[\]]+==[^\s,]+$", line):
            pins += 1
        else:
            ranges.append(line)
    assert pins >= 38, f"only {pins} exact pins found - the scan subject looks wrong"
    assert ranges == [], f"non-exact requirement specifiers: {ranges}"


def test_apt_installs_of_third_party_packages_are_version_pinned():
    text = read(DOCKERFILE)
    apt_pinned = re.search(r'"openfoam\$\{OPENFOAM_SERIES\}(?:-common)?=\$\{OPENFOAM_PKG_VERSION\}"', text)
    archive_pinned = re.search(r"openfoam\$\{OPENFOAM_SERIES\}[\w-]*_\$\{OPENFOAM_PKG_VERSION\}_\w+\.deb", text)
    assert apt_pinned or archive_pinned, (
        "the OpenFOAM packages are not acquired at a pinned version")
    assert re.search(r"ARG OPENFOAM_PKG_VERSION=[\d.\-]+", text), (
        "OPENFOAM_PKG_VERSION is not declared as an exact version")
    assert "apt-get upgrade" not in text, "apt-get upgrade makes the image contents unbounded"


def test_mirror_redirected_apt_installs_survive_a_dead_mirror():
    blocks = [body for _, body in run_blocks() if re.search(r"add-\w+-repo\.sh", body)]
    assert blocks, "no downloaded repository-setup script found - the scan subject is empty"
    offenders = []
    for body in blocks:
        for verb in ("update", "install"):
            for m in re.finditer(rf"apt-get\s+([^\n]*?)\b{verb}\b", body):
                if "Acquire::Retries" not in m.group(1):
                    offenders.append(f"apt-get {verb} without Acquire::Retries")
    assert offenders == [], offenders


def test_conda_environment_pins_its_package_versions():
    text = read(DOCKERFILE)
    if "micromamba create" not in text:
        pytest.skip("no conda environment in the build")
    assert re.search(r'"vmtk=\$\{VMTK_VERSION\}"', text), "vmtk is not version-pinned"
    assert re.search(r"ARG VMTK_VERSION=[\d.]+", text), "VMTK_VERSION is not an exact version"
    assert re.search(r'"python=\$\{VMTK_PYTHON\}"', text), "the conda python is not pinned"


# CI and VCS
def test_ci_actions_are_pinned_to_full_commit_shas():
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", read(CI), re.M)
    assert uses, "no CI actions found - the scan subject is empty"
    for ref in uses:
        assert "@" in ref, f"CI action without a pinned ref: {ref}"
        sha = ref.split("@", 1)[1]
        assert FULL_SHA.match(sha), (
            f"CI action {ref} is not pinned to a full 40-character commit SHA")


def test_no_cloned_dependency_or_submodule_is_unpinned():
    for rel in tracked():
        if rel == ".gitmodules":
            pytest.fail("a submodule was added; pin it to a full commit SHA and update this guard")
    clones = []
    for rel in tracked("Dockerfile", "Makefile", "devtools", "deploy", ".github"):
        p = REPO / rel
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"\bgit\s+clone\b", line) and "--branch" not in line:
                clones.append(f"{rel}:{i}")
    assert clones == [], f"git clone without a pinned commit: {clones}"


def test_no_node_install_appears_without_a_frozen_lockfile():
    manifests = [f for f in tracked() if Path(f).name in
                 ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")]
    installs = []
    for rel in tracked("Dockerfile", "Makefile", "devtools", "deploy", ".github"):
        p = REPO / rel
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"\b(?:npm|pnpm|yarn)\s+(?:install|add)\b", line):
                installs.append(f"{rel}:{i}")
    if not manifests and not installs:
        return                                  # the ecosystem is genuinely absent
    assert not installs, f"non-frozen Node install (use `npm ci`): {installs}"


# reconciliation
def test_every_discovered_acquisition_site_is_classified():
    recognised = 0
    unclassified = []
    for src, ref in image_references():
        recognised += 1
        if not DIGEST.search(ref):
            unclassified.append(f"{src}: {ref}")
    for lineno, url, _block in curl_downloads():
        recognised += 1
        if not any(h in url for h in RUNTIME_HOSTS) and not url.startswith("https://"):
            unclassified.append(f"Dockerfile:{lineno} {url}")
    assert recognised >= 10, f"only {recognised} acquisition sites seen - discovery looks broken"
    assert unclassified == [], f"unclassified acquisition sites: {unclassified}"


def test_the_build_input_reference_documents_every_locked_class():
    doc = (REPO / "docs/reference/build-inputs.md").read_text()
    for token in ("ubuntu:22.04@sha256", "micromamba", "OpenFOAM", "postgres", "redis",
                  "searxng", "minio", "CI actions", "availability"):
        assert token.lower() in doc.lower(), f"build-inputs.md does not cover {token!r}"


def test_build_and_parser_directives_stay_in_their_required_positions():
    # POSITION IS THE CONTRACT, not tidiness. BuildKit reads a parser directive only as the very
    # first line and silently ignores it anywhere else, and two of these scripts are execed
    # directly as container ENTRYPOINTs - a shebang off line one is ENOEXEC, not a style lapse.
    docker = read(DOCKERFILE).splitlines()
    for i, line in enumerate(docker):
        if re.match(r"^#\s*(syntax|escape)\s*=", line):
            assert i == 0, "a Dockerfile parser directive must be the first line to take effect"

    for rel in tracked("*.sh"):
        first = read(REPO / rel).splitlines()[:1]
        assert first and first[0].startswith("#!"), (
            f"{rel}: a shell script must keep its shebang on line one")


def test_the_requirements_pins_are_never_carried_into_the_toolchain_by_their_bytes():
    docker = read(DOCKERFILE)
    assert "COPY --from=requirements" in docker, (
        "base no longer consumes the normalised requirements - a comment edit would rebuild "
        "the OpenFOAM layer again")
    base_section = docker.split("AS base", 1)[1].split("AS wheel", 1)[0]
    assert "COPY requirements/runtime.txt" not in base_section, (
        "base copies the raw requirements file, which re-couples comment bytes to the toolchain")


def test_the_requirements_normalizer_preserves_every_pin_and_hash():
    dockerfile = read(DOCKERFILE)
    m = re.search(r"(sed -e [^\n]*?/in/runtime\.txt\s*>\s*/pins/runtime\.txt)", dockerfile)
    assert m, "the requirements normalizer command was not found - the scan subject is empty"
    sed_cmd = m.group(1)

    source = REPO / "requirements/runtime.txt"
    result = subprocess.run(["bash", "-c", sed_cmd.replace("/in/runtime.txt", str(source))
                             .replace("> /pins/runtime.txt", "")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    normalized = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]

    def requirement_lines(text: str) -> set[str]:
        out = set()
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                out.add(line)
        return out

    expected = requirement_lines(source.read_text())
    assert expected, "no requirement lines parsed - the scan subject is empty"
    missing = sorted(expected - set(normalized))
    assert missing == [], f"the normalizer dropped requirement line(s): {missing}"

    extra = sorted(set(normalized) - expected)
    assert extra == [], f"the normalizer invented line(s): {extra}"


def test_the_requirements_normalizer_reacts_to_a_pin_change_but_not_a_comment_change(tmp_path):
    dockerfile = read(DOCKERFILE)
    m = re.search(r"(sed -e [^\n]*?)/in/runtime\.txt", dockerfile)
    assert m, "the requirements normalizer command was not found"
    sed_prefix = m.group(1)

    def normalize(path: Path) -> str:
        r = subprocess.run(["bash", "-c", f"{sed_prefix}{path}"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout

    base = tmp_path / "r.txt"
    base.write_text("# a comment\nfastapi==0.115.5        # inline note\n\nredis==5.2.1\n")
    original = normalize(base)

    base.write_text("# a DIFFERENT comment\nfastapi==0.115.5        # changed note\n\nredis==5.2.1\n")
    assert normalize(base) == original, "a comment-only edit changed the normalized pins"

    base.write_text("# a comment\nfastapi==0.116.0        # inline note\n\nredis==5.2.1\n")
    assert normalize(base) != original, "a VERSION change did not change the normalized pins"
