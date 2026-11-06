#!/bin/bash
# Collect the docs worth sending a co-founder into one flat folder, numbered in reading order.
#
# COPIES, not the originals, and the folder is gitignored. Two copies of a doc in one repo drift,
# and the drift is silent. So this is a build output: rerun it and the bundle is current. The
# script is what is committed, not the bundle.
set -e
cd /home/rehaan/hexera-platform-semi
OUT=handover
rm -rf "$OUT"; mkdir -p "$OUT"

# source path                                        -> name in the bundle
pick() {
  [ -f "$1" ] || { echo "MISSING $1"; return 1; }
  cp "$1" "$OUT/$2"
  printf "  %-46s -> %s\n" "$1" "$2"
}

echo "orientation:"
pick docs/README.md                                  01-start-here.md
pick docs/architecture/overview.md                   02-architecture.md
pick docs/product/existing-capabilities.md           03-what-works-today.md
pick docs/engines/overview.md                        04-the-meshers.md
pick docs/RULES.md                                   05-house-rules.md
echo "getting it running:"
pick docs/getting-started/setup.md                   06-run-it-locally.md
pick docs/deployment/dev-raymond-setup.md            07-your-own-cloud-env.md
pick docs/deployment/overview.md                     08-how-we-ship.md
echo "what it costs and what is live:"
pick docs/deployment/gcp-live-inventory.md           09-what-is-running.md
pick docs/deployment/cost-estimate.md                10-what-it-costs.md
echo "the Corintis job:"
pick docs/semiconductor/COUPLED_MESH_DELIVERY.md     11-corintis-what-we-shipped.md
pick docs/semiconductor/MESH_QUALITY_ASSESSMENT.md   12-corintis-how-good-is-it.md
pick docs/semiconductor/MESH_QUANTISATION.md         13-corintis-the-defect-we-found.md

cat > "$OUT/00-READ-THIS-FIRST.md" <<'IDX'
# Hexera, in thirteen documents

Everything here is a copy out of `docs/` in the platform repo, numbered in the order I would read
them. The repo is the source of truth; this folder is a snapshot so it can be sent as one thing.

## Orientation, about two hours

| | |
|---|---|
| `01-start-here.md` | the docs index, so you can find anything I left out |
| `02-architecture.md` | how the system is put together |
| `03-what-works-today.md` | what actually ships, as opposed to what is planned |
| `04-the-meshers.md` | the engine lineup and why each one is in it |
| `05-house-rules.md` | how we work on this codebase |

## Getting it running

| | |
|---|---|
| `06-run-it-locally.md` | local setup |
| `07-your-own-cloud-env.md` | your own API, console, mesh job and worker fleet in `hexera-dev` |
| `08-how-we-ship.md` | the deploy model |

Step 0 of `07` is `make seed-secrets`. It is easy to skip and the environment deploys fine without
it, then cannot run a single job. Do not skip it.

## Money and what is live

| | |
|---|---|
| `09-what-is-running.md` | the actual inventory in Google Cloud |
| `10-what-it-costs.md` | the cost model |

## The Corintis job, our first real customer part

Read these three in order. **11 and 12 describe what we shipped. 13 partly contradicts 12**, and
that is deliberate rather than an oversight: it is a defect found afterwards, on 5 November, by
measuring the delivered mesh instead of the CAD it was built from.

| | |
|---|---|
| `11-corintis-what-we-shipped.md` | the delivery note, the coupled plate and chip mesh |
| `12-corintis-how-good-is-it.md` | quality graded bad / okayish / pretty good / great |
| `13-corintis-the-defect-we-found.md` | the grid rounds every 100 um channel to 88, 110 or 132 um |

The short version of 13: the coolant boundary sits on mesh cell faces, so a channel has to be a
whole number of cells wide, and at our cell size there is no whole number that gives 100 um. Flow
through a slot goes as width cubed, so two channels the CAD made identical come out 1.98x apart.
It is fixable by changing one number, and the mesh gets smaller when we do.

## Deliberately not in here

- `docs/semiconductor/DEVLOG.md`, 4,758 lines of working log including every dead end. Useful to
  whoever is in the code, not to someone getting oriented.
- `docs/superpowers/`, 33 files and 25,600 lines of plans and design specs. Working papers.
- Anything under `deploy/` or `devtools/`. Read those in the repo, not as a snapshot.

I checked all 54 tracked documents for leaked credentials before building this. Nothing matched.
IDX
echo "  (index written: 00-READ-THIS-FIRST.md)"

grep -qx "/handover/" .gitignore 2>/dev/null || printf "\n# handover bundle: a build output of devtools/handover.sh, never a second source of truth\n/handover/\n" >> .gitignore
cp "$0" devtools/handover.sh 2>/dev/null || true
chmod +x devtools/handover.sh 2>/dev/null || true

echo
echo "bundle: $(ls -1 "$OUT" | wc -l) files, $(du -sh "$OUT" | cut -f1), $(cat "$OUT"/*.md | wc -l) lines"
ls -1 "$OUT"
