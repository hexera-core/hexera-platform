#!/usr/bin/env bash
# Responsibility: Print one digest of the ENTIRE tracked working tree, as the bytes on disk right now.
# Boundaries: content, path and mode only - it reads no history, no timestamps and no untracked evidence.

# Every tracked path counts: src, migrations, Dockerfiles, Compose, deploy/docker, ui, dependency
# pins, Makefile, scripts, tests, docs and policy files. The consequence is deliberate - ANY
# tracked change invalidates a built application image. A curated list of "build-relevant"
# directories is what let a Dockerfile or ui/ edit slip past the freshness check.
#
# `git rev-parse HEAD^{tree}` names the COMMITTED tree and `git ls-files -s` names the INDEX;
# neither sees an unstaged edit, which is the case that let a stale image be tested against. This
# reads the working file.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

exec python3 - "$PWD" <<'PY'
import hashlib
import os
import subprocess
import sys

root = os.fsencode(sys.argv[1])


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def nul(raw: bytes) -> list[bytes]:
    return [p for p in raw.split(b"\0") if p]


def h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The boundary an image could cross without the digest noticing.
# Nothing untracked may reach a revision-checked image. The Dockerfile's own COPY sources say
# which directories can, so the check is derived from the build, never from a list kept by hand.
copy_roots: set[bytes] = set()
for line in open(os.path.join(os.fsdecode(root), "Dockerfile"), "rb").read().splitlines():
    parts = line.split()
    if not parts or parts[0].upper() not in (b"COPY", b"ADD"):
        continue
    if any(p.startswith(b"--from=") for p in parts):
        continue                                    # copies from an earlier stage, not the context
    for src in parts[1:-1]:
        if src.startswith(b"--"):
            continue
        copy_roots.add(src.rstrip(b"/").split(b"/")[0])

strays = [p for p in nul(git("ls-files", "-z", "--others", "--exclude-standard"))
          if p.split(b"/")[0] in copy_roots]
if strays:
    listed = "\n  ".join(os.fsdecode(p) for p in sorted(strays)[:20])
    sys.exit(
        "source_digest: these files are untracked but sit inside a directory the Dockerfile "
        "copies into the image, so they would ship in a build that this digest cannot account "
        f"for:\n  {listed}\n"
        "Track them (`git add`) or remove them. Hashing them instead would mean hashing "
        "untracked content, and there is exactly one digest here.")

# One record per tracked path.
# Framing is fixed-width and delimiter-free: <mode> <content-hash> <path-hash>. The path is
# HASHED rather than printed, so a name containing a space, a newline, a quote or a shell
# metacharacter cannot collide with another record or break the enumeration.
gitlinks: dict[bytes, bytes] = {}
for entry in nul(git("ls-files", "-s", "-z")):
    meta, _, path = entry.partition(b"\t")
    mode, obj, _stage = meta.split(b" ")
    if mode == b"160000":
        gitlinks[path] = obj

records: list[str] = []
for path in nul(git("ls-files", "-z")):
    full = os.path.join(root, path)
    path_hash = h(path)

    if path in gitlinks:
        # A submodule is a recorded commit plus whatever its working tree currently looks like.
        # Both are included; it is never silently skipped.
        try:
            dirty = subprocess.run(["git", "status", "--porcelain=v1", "-z"],
                                   cwd=full, capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            sys.exit(f"source_digest: submodule {os.fsdecode(path)} is tracked but its working "
                     "tree cannot be inspected, so its state cannot be included. Initialise it "
                     "(`git submodule update --init`) or remove it.")
        submodule_state = gitlinks[path] + b"\0" + dirty
        records.append(f"160000 {h(submodule_state)} {path_hash}")
        continue

    if os.path.islink(full):
        # The TARGET is the content of a symlink. Never dereferenced: a link that points outside
        # the tree, or at nothing, still has a definite value.
        records.append(f"120000 {h(os.readlink(full))} {path_hash}")
        continue

    if not os.path.lexists(full):
        # Tracked but deleted from the working tree. A distinct record, so a deletion changes the
        # digest instead of merely dropping a line that something else might replace.
        records.append(f"000000 {h(b'<deleted>')} {path_hash}")
        continue

    st = os.stat(full)
    mode_s = "100755" if st.st_mode & 0o111 else "100644"
    digest = hashlib.sha256()
    with open(full, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    records.append(f"{mode_s} {digest.hexdigest()} {path_hash}")

records.sort()
print(h("\n".join(records).encode()))
PY
