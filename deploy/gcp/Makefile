# Responsibility: Name each stage of mesh provisioning and dispatch it to the script that performs it.
# Boundaries: a wrapper - each script loads its own configuration, and mesh-deploy is the one entry point.

# deploy/gcp - thin wrapper over scripts/. Each script loads ./generated.env itself.
#
# This tooling owns ONE remote resource tier: the Cloud Run mesh job the local pipeline submits
# to, its image, its identity and the GCS exchange bucket they trade workspaces through. The API,
# the pipeline and every data store run locally and are not deployed from here.
#
# THE ENTRY POINT is `make mesh-deploy` from the repository root; it runs every stage below in
# order, idempotently. The individual targets exist for re-running one stage while debugging.

SHELL := /usr/bin/env bash
S     := ./scripts

.PHONY: help mesh-deploy doctor bootstrap validate preflight enable-apis artifact-registry \
        service-account mesh-tier iam state provision publication-target smoke check

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

mesh-deploy: ## THE one command: provision and update the mesh executor (idempotent)
	@bash $(S)/deploy.sh

doctor: ## Read-only configuration diagnosis (no gcloud mutation)
	@bash $(S)/deploy-doctor.sh
bootstrap: ## Generate ./generated.env by discovering the named mesh resources
	@bash $(S)/bootstrap-env.sh $(ARGS)
validate: ## Validate generated.env as one typed schema (read-only; no gcloud)
	@bash $(S)/validate-config.sh
preflight: ## Read-only checks: auth, project, permissions, region, existing mesh resources
	@bash $(S)/preflight.sh

enable-apis: ## Enable the Google APIs the mesh tier needs (idempotent)
	@bash $(S)/enable-apis.sh
artifact-registry: ## Create the regional Docker repo for the mesh image (idempotent)
	@bash $(S)/create-artifact-registry.sh
service-account: ## Create or validate the mesh-job runtime identity (idempotent)
	@bash $(S)/create-service-accounts.sh
mesh-tier: ## Create or validate the exchange bucket + mesh job from the promoted digest
	@bash $(S)/create-mesh-tier.sh
iam: ## Apply least-privilege IAM for mesh invocation and exchange (idempotent)
	@bash $(S)/apply-iam.sh
state: ## Write deploy/output/deployment.json (mesh resources + digests)
	@bash $(S)/write-deployment-state.sh

# What `make release-publish` needs before it can push: a discovered environment (so the
# registry host, project and repository are known) and that repository actually existing.
# Ordered as its own target because publication happens BEFORE the mesh job can be created -
# the job is pinned to the digest publication produces.
# Enabling comes FIRST: discovery asks Cloud Run and Cloud Storage what already exists, and on a
# project where those APIs have never been turned on the question itself fails. A blank project is
# the case this target exists for, so the order that only works on an already-provisioned project
# is the wrong one. Enabling is idempotent, so paying for it on every run costs nothing.
publication-target: enable-apis bootstrap validate artifact-registry ## Environment + registry that release-publish pushes to

provision: enable-apis artifact-registry service-account mesh-tier iam ## All mesh provisioning

smoke: ## Real mesh smoke - requires explicit GCP authority: make smoke JOB_ID=<uuid>
	@bash $(S)/mesh-smoke.sh $(JOB_ID)

check: ## Local sanity: shellcheck + bash -n every script + validate manifests/lifecycle
	@for f in $(S)/*.sh; do bash -n "$$f" && echo "  ok  $$f"; done
	@if command -v shellcheck >/dev/null 2>&1; then \
	  ( cd $(S) && shellcheck -x *.sh ) && echo "  ok  shellcheck"; \
	else echo "  .. shellcheck not installed - skipped (recommended: run it in CI)"; fi
	@python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" cloud-run/*.yaml && echo "  ok  cloud-run/*.yaml"
	@python3 -c "import json,glob; [json.load(open(f)) for f in glob.glob('storage/*.json')]" && echo "  ok  storage/*.json"
