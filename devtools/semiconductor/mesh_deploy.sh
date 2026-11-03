#!/bin/bash

# RETIRED 2026-11-03. This script wrote to GCP project `hexera`, whose billing account is
# being wound down. The personal environment dev-rehaan in hexera-dev is labelled
# managed-by=deploy, so its mesh job must be refreshed through the GitHub Actions workflow
# (the file .github/workflows/deploy.yml, dispatched with slug=rehaan), never by a hand-rolled
# jobs replace from here. Doing it by hand also fights create-mesh-tier.sh, which branches on
# that same label.
echo "RETIRED: this script targeted the old hexera project. Use the GitHub Actions workflow instead." >&2
exit 1

# Push the image just built and point the SEMI job at its digest. Areen's hexera-mesh is never
# touched: this replaces hexera-mesh-semi only.
set -uo pipefail
GC=/usr/lib/google-cloud-sdk/bin/gcloud
cd ~/hexera-platform-semi
TAG=$(cat /tmp/semi-image/TAG)
echo "pushing $TAG"
docker push "$TAG" 2>&1 | tail -2
DIGEST=$("$GC" artifacts docker images describe "$TAG" --project hexera \
  --format="value(image_summary.digest)" 2>/dev/null)
echo "digest $DIGEST"
export CLOUDRUN_MESH_JOB=hexera-mesh-semi DEPLOYMENT_ID=hexera-semi
export MESH_SA_EMAIL=hexera-mesh@hexera.iam.gserviceaccount.com
export MESH_IMAGE="us-central1-docker.pkg.dev/hexera/mesh/mesh@${DIGEST}"
export MESH_TIMEOUT_SECONDS=14400 MESH_CPU=8 MESH_MEMORY=32Gi
envsubst < deploy/gcp/cloud-run/mesh-job.yaml > /tmp/semi-image/mesh-job-semi.yaml
"$GC" run jobs replace /tmp/semi-image/mesh-job-semi.yaml --region us-central1 --project hexera 2>&1 | tail -3
echo "semi job now on ${MESH_IMAGE##*@}"
echo "--- Areen's job, unchanged:"
"$GC" run jobs describe hexera-mesh --region us-central1 --project hexera \
  --format="value(spec.template.spec.template.spec.containers[0].image)" 2>/dev/null
