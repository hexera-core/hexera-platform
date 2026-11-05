#!/usr/bin/env bash
# Responsibility: Refuse a mesh image that was not built from the current tracked working tree.
# Boundaries: it inspects an existing image and exits; it never builds, pulls or recreates one.

# A native run proves what the image contains, so an image built from other source proves nothing
# about this checkout. The digest comes from tests/integration/source_digest.sh - the same single
# authority the four application images use - so there is one definition of "current".
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

IMAGE="${1:?usage: assert_mesh_image_current.sh <image>}"
WANT="$(bash tests/integration/source_digest.sh)"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "FATAL: mesh image '$IMAGE' does not exist - build it with: make mesh-image" >&2
  exit 1
fi

GOT="$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMAGE" \
        | sed -n 's/^MESH_SOURCE_TREE=//p' | head -1)"

if [ -z "$GOT" ] || [ "$GOT" = "unknown" ] || [ "$GOT" != "$WANT" ]; then
  cat >&2 <<EOF
FATAL: the mesh image is stale - refusing before any native execution.

  working tree : $WANT
  image tree   : ${GOT:-<empty>}

A native result from this image would describe source you do not have checked out. Rebuild it:

  make mesh-image

then run this command again.
EOF
  exit 1
fi
echo "  mesh image tree matches the working tree ($WANT)"
