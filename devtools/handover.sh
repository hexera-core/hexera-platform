#!/bin/bash
# Collect the docs worth sending a co-founder into one flat folder, numbered in reading order.
#
# COPIES into a gitignored handover/, not a second tracked set. Two copies of a doc in one repo
# drift, and the drift is silent. So this is a build output: rerun it and the bundle is current.
# The script is the artifact, not the bundle.
set -e
cd "$(git rev-parse --show-toplevel)"
OUT=handover
rm -rf "$OUT"; mkdir -p "$OUT"

pick() {
  [ -f "$1" ] || { echo "  MISSING $1"; return 1; }
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
pick docs/semiconductor/CORINTIS.md                  11-corintis-the-account.md
pick docs/semiconductor/COUPLED_MESH_DELIVERY.md     12-corintis-what-we-shipped.md
pick docs/semiconductor/MESH_QUALITY_ASSESSMENT.md   13-corintis-how-good-is-it.md
pick docs/semiconductor/MESH_QUANTISATION.md         14-corintis-the-defect-we-found.md
pick docs/semiconductor/ROUTE_TO_FINAL_MESH.md       15-the-plan-from-here.md
pick docs/semiconductor/MESH_TIME.md                 16-why-meshes-take-what-they-take.md
pick docs/semiconductor/RESEARCH_BOUNDARY_LAYERS.md  17-the-research.md
pick docs/semiconductor/VIEWER.md                    18-how-the-viewer-works.md
echo "the raw logs, included on request:"
pick docs/semiconductor/DEVLOG.md                    19-devlog-semiconductor.md
pick docs/engines/fitness-changelog.md               20-engine-fitness-changelog.md

cat > "$OUT/00-READ-THIS-FIRST.md" <<'IDX'
# Hexera, in twenty documents

Copies out of `docs/` in the platform repo, numbered in the order I would read them. The repo is
the source of truth; this folder is a snapshot so it can be sent as one thing. All of it is
internal engineering material.

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
it, then cannot run a single job.

## Money and what is live

| | |
|---|---|
| `09-what-is-running.md` | the actual inventory in Google Cloud |
| `10-what-it-costs.md` | the cost model |

## The Corintis job, our first real customer part

Read `11` first. Then `12` and `13` describe the mesh we built, and **`14` partly contradicts
`13`** on purpose: it is a defect found afterwards, by measuring the delivered mesh instead of the
CAD it was built from.

| | |
|---|---|
| `11-corintis-the-account.md` | who they are, their part as measured, the six readings they asked for by name, and what is still unknown |
| `12-corintis-what-we-shipped.md` | what the mesh is and what was checked |
| `13-corintis-how-good-is-it.md` | quality graded bad / okayish / pretty good / great. Its section 5 conclusion is marked superseded |
| `14-corintis-the-defect-we-found.md` | the grid rounds every 100 um channel to 88, 110 or 132 um |
| `15-the-plan-from-here.md` | every step from the mesh we have to the mesh we want, in dependency order |
| `16-why-meshes-take-what-they-take.md` | why one run took 38.7 h and the next takes about 2.2 |
| `17-the-research.md` | the 37-agent sweep into boundary layers, grading and cheaper alternatives |
| `18-how-the-viewer-works.md` | the viewer, and the boundary index that lets it work with the mesh deleted |

The short version of `14`: the coolant boundary sits on mesh cell faces, so a channel has to be a
whole number of cells wide, and at our cell size no whole number gives 100 um. Flow through a slot
goes as width cubed, so two channels the CAD made identical come out 1.98x apart. One number in
one dict fixes it, and the mesh gets smaller.

The short version of `17`: boundary layers are achievable on the mesh we already have, it had
looked impossible because of one wrong value in a shipped config file, and they are still not the
first thing to do because of a cancellation explained in `15`.

## The raw logs

| | |
|---|---|
| `19-devlog-semiconductor.md` | 4,758 lines. Every change, what it measured, and what it cost to find out |
| `20-engine-fitness-changelog.md` | 36 lines. How engine selection has moved |

**`19` is a working log, not a document.** Chronological, includes every dead end, and later
entries correct earlier ones. Three in particular: an earlier figure of "9% low on Nusselt" is
wrong in sign and subject, the near-wall cell count was treated as the dominant error when it is
not, and a planned 64-bit image rebuild turned out to be a regression. Reading it front to back is
the honest version of how the mesh got built. Reading one entry out of order is a way to quote a
number we no longer stand behind.

For conclusions rather than the path, read `11` through `18`.

## Deliberately not in here

- `docs/superpowers/`, 33 files and 25,600 lines of plans and design specs. Working papers.
- Anything under `deploy/` or `devtools/`. Read those in the repo, not as a snapshot.

I checked every tracked document for leaked credentials before building this. Nothing matched.
IDX
echo "  (index written: 00-READ-THIS-FIRST.md)"

cd "$(git rev-parse --show-toplevel)"
grep -qx "/handover/" .gitignore 2>/dev/null || \
  printf "\n# handover bundle: a build output of devtools/handover.sh, never a second source of truth\n/handover/\n" >> .gitignore

echo
echo "bundle: $(ls -1 "$OUT" | wc -l) files, $(du -sh "$OUT" | cut -f1), $(cat "$OUT"/*.md | wc -l) lines"
