# Responsibility: Name every developer and release command, and the gate each one stands for.
# Owns: the interpreter and lint-path selection, declared once so no two targets can disagree about them.
# Boundaries: it dispatches to the devtools/ and deploy/ scripts; what they do is not restated here.

# Hexera - Makefile
#
#   make setup     one-time: prepare a fresh clone (venv, deps, dirs, images). It creates .env
#                  from .env.example the first time and never touches it again.
#   edit .env      the one configuration file a developer owns - nothing else writes it
#   make dev-up    start the local stack; it validates .env first and names every gap at once
#   make dev-up    start the local stack
#   make help      the common commands;  make help-all  for advanced/internal ones
#
# Host-run targets (check/test/lint/typecheck/wheel) use the virtualenv `make setup`
# creates (.venv) automatically - no `source .venv/bin/activate` needed. If .venv is
# absent they fall back to whatever python/ruff is on PATH.

# Run every recipe under bash with pipefail so a failing command in a pipe fails the recipe.
COMPOSE_QUIET ?= --quiet
SHELL := /bin/bash
.SHELLFLAGS := -eo pipefail -c

# THE image architecture, declared once rather than inherited from whoever runs the build.
# Every image this repository produces is amd64 BY CONSTRUCTION: the Dockerfile fetches the
# OpenFOAM .deb from binary-amd64, adds the Docker CE apt repo with arch=amd64, and installs an
# amd64 Chrome. Several pinned wheels (gmsh ships manylinux x86_64 only) have no aarch64 build at
# all. Left unset, Docker targets the HOST architecture, so the same commit builds on an x86_64
# CI runner and fails on an Apple Silicon laptop at the first wheel with no aarch64 distribution -
# an accident of hardware, reported as a dependency conflict. amd64 is also what Cloud Run and the
# worker VMs execute, so this is the artifact the deployment actually needs. Building it on
# Apple Silicon goes through emulation and is slow; it is correct, which the alternative is not.
DOCKER_DEFAULT_PLATFORM ?= linux/amd64
export DOCKER_DEFAULT_PLATFORM

VENV := .venv
PY   := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
RUFF := $(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff)

# Everything ruff lints, named once: `lint` and `check-fast` both use it, so the set cannot
# drift between them. devtools/ is maintained Python and is held to the same rules as the rest;
# the shell tools there are checked with `bash -n` / shellcheck, not with ruff.
LINT_PATHS := src tests alembic devtools

# The product version, READ from the one authority (src/meshpipeline/__init__.py) rather than
# restated here. Parsed textually so it resolves without the package installed.
PRODUCT_VERSION := $(shell sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' src/meshpipeline/__init__.py | head -1)

.PHONY: help help-all setup check test test-fast logs clean \
        dev-doctor dev-up dev-down dev-logs dev-reset \
        mesh-setup mesh-deploy mesh-doctor mesh-adopt mesh-destroy dev-images \
        rebuild restart logs-api logs-worker logs-all migrate migrate-auto db-shell \
        shell-api shell-worker test-integration test-container test-external-fixtures \
        test-ui test-all smoke wheel dependencies deps lint typecheck wait-postgres \
        check-fast release-validate release-publish mesh-preflight validate \
        mesh-image mesh-toolchain \
        clean-workspaces

# Primary developer commands (shown by `make help`)

setup: ## One-time: prepare a fresh clone (host tools, .env, venv, deps, dirs, images)
	@bash devtools/env/setup.sh

dev-doctor: ## Hybrid dev preflight: local control plane + Cloud Run mesh (no mesh started)
	@bash devtools/env/doctor.sh

dev-up: ## Start the dev stack: local UI/API/pipeline/data + Cloud Run mesh compute
	@# THE runtime configuration gate. What is required comes from the settings catalogue, so no
	@# list of variable names is kept here; every fault is reported in one response and nothing
	@# starts until they are fixed in .env.
	@$(PY) devtools/env/check_runtime_config.py
	@# The same diagnosis `make mesh-doctor` runs. Startup asks it so the normal path is one
	@# command: a configured-but-unreachable executor is found here, not by a mesh that fails
	@# twenty minutes in. Nothing has started at this point.
	@$(MAKE) --no-print-directory mesh-doctor
	@# Images must correspond to the committed source. `dev-build` is the one build authority;
	@# this only decides whether it needs to run, using the stamp the images already carry. It
	@# also guarantees the worker image the in-container preflight below runs in, which is why
	@# that preflight no longer builds one of its own.
	@$(MAKE) --no-print-directory dev-images
	@# Every mesh is dispatched to the Cloud Run mesh Job; nothing meshes here. The preflight below
	@# runs INSIDE an application container, so what it proves is what the stack will actually do.
	@# The credential path comes from the SAME resolver the gate just used, exported so Compose
	@# binds exactly the file that was validated - a relative value cannot be re-interpreted
	@# against some other directory on the way to the mount. Running the preflight creates this
	@# project's volumes, so a refusal removes them again: a run that decided not to start must
	@# leave the machine as it found it, or the next attempt is no longer a first attempt.
	@# The source stamp is exported for the same reason `rebuild` supplies it: if Compose decides
	@# it has to build anything below - a missing image, a renamed project - an unexported value
	@# bakes in an EMPTY stamp, and the integration tier then refuses an image that is in fact
	@# current. Exporting it here means a stack started the supported way is always provably built
	@# from this checkout.
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	 export MESH_SOURCE_TREE="$(SOURCE_DIGEST)" APP_VERSION="$(PRODUCT_VERSION)"; \
	 GOOGLE_ADC_FILE="$$($(PY) devtools/env/check_runtime_config.py --credential-path)"; \
	 export GOOGLE_ADC_FILE; \
	 if ! docker compose run --rm --no-deps -T --entrypoint python worker - \
	      < devtools/env/preflight_cloudrun.py; then \
	   docker compose down -v --remove-orphans >/dev/null 2>&1 || true; \
	   exit 1; \
	 fi; \
	 docker compose up -d
	@echo ""
	@echo "  Dev stack starting - LOCAL control plane + CLOUD mesh compute."
	@echo "  Migrations apply automatically on API start. Meshing runs OFF-BOX on Cloud Run."
	@echo "  UI:     http://localhost:8000/ui"
	@echo "  API:    http://localhost:8000     (health: /health   readiness: /readyz)"
	@echo "  MinIO:  http://localhost:9001"
	@echo ""
	@echo "  Watch it come up:  make dev-logs      Stop:  make dev-down"

dev-images: ##! Build the application images only if their source stamp is not this working tree
	@# Currency is decided from the stamp an image already carries, so a machine that has not
	@# changed its checkout does not rebuild four images to start the stack. An OCI revision
	@# label is accepted first, for an image built elsewhere that records its source that way;
	@# images built here carry MESH_SOURCE_TREE instead, so the second lookup is the ordinary
	@# path rather than a fallback for old images. Any image that cannot prove it matches this
	@# tree counts as stale, so an unreadable stamp rebuilds rather than assumes.
	@# Skipping a build is a claim that these images were built from these bytes, so only positive
	@# proof may make it: a digest, a stamp, and the two equal. An unreadable digest and an
	@# unstamped image are both the empty string, and comparing them agreed - so the one tree the
	@# digest refused to describe was the one tree that skipped its rebuild. Absence of evidence
	@# is not evidence of currency, and every way of having none now rebuilds.
	@digest="$(SOURCE_DIGEST)"; current=1; \
	 [ -n "$$digest" ] || { current=0; echo "  No source digest - rebuilding rather than assuming."; }; \
	 for svc in api worker worker-utility beat; do \
	   img="$(COMPOSE_PROJECT)-$$svc"; \
	   stamp="$$(docker image inspect "$$img" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"; \
	   [ -n "$$stamp" ] || stamp="$$(docker run --rm --entrypoint sh "$$img" -c 'printenv MESH_SOURCE_TREE' 2>/dev/null || true)"; \
	   { [ -n "$$stamp" ] && [ "$$stamp" = "$$digest" ]; } || current=0; \
	 done; \
	 if [ "$$current" = "1" ]; then echo "  Images already built from this source - not rebuilding."; \
	 else echo "  Building images for the current source..."; $(MAKE) --no-print-directory dev-build; fi

dev-build: ## Build the local application images from this checkout (no cloud configuration needed)
	@# Building needs nothing from Google Cloud, so it does not sit behind the runtime gate: a peer
	@# can get the slow part done - and find out that the build works - before they have a project.
	@# `dev-up` still gates. This starts nothing.
	@# Stamped with the tree it was built from. The staleness check has nothing to compare an
	@# unstamped image against, so leaving it out does not make that check lenient - it makes it
	@# permanently negative, and every start would rebuild what it just built.
	MESH_SOURCE_TREE="$(SOURCE_DIGEST)" APP_VERSION="$(PRODUCT_VERSION)" docker compose build $(COMPOSE_QUIET) api worker worker-utility beat
	@echo "  Application images built from this checkout. Nothing was started."
	@echo "  make dev-up also needs the Cloud Run mesh configured. Check it with: make mesh-doctor"

dev-down: ## Stop the local dev stack (keeps volumes; never touches cloud or unrelated containers)
	docker compose down
	@echo "  Local dev services stopped. Data volumes kept. For a full local wipe: make dev-reset"

dev-logs: ## Tail the hybrid dev stack logs
	docker compose logs -f --tail=100

dev-reset: ## Destructive: stop the dev stack AND delete its local data volumes (never cloud)
	docker compose down -v
	@echo "  Local dev services + LOCAL data volumes removed (Postgres/Redis/MinIO/SearXNG)."
	@echo "  Cloud resources (mesh Job, GCS exchange) are NEVER touched by this target."

dev-uninstall: ## Destructive: everything dev-reset removes, PLUS this project's images, .env, .venv and build residue
	@# The complete uninstall. `dev-reset` is the routine one - it returns you to an empty database
	@# and keeps the machine set up. This removes what setup created, so the next start builds and
	@# configures from nothing. It is scoped by THIS compose project and this directory: unrelated
	@# containers, images, volumes and other checkouts of this product are never touched.
	docker compose down -v --remove-orphans
	-@docker image rm -f $$(docker images --filter "reference=$(COMPOSE_PROJECT)-*" -q | sort -u) 2>/dev/null || true
	rm -rf .venv build dist src/*.egg-info *.egg-info .pytest_cache
	rm -f .env
	find output -mindepth 1 -delete 2>/dev/null || true
	@echo "  Removed: local services, data volumes, $(COMPOSE_PROJECT) images, .env, .venv, build residue."
	@echo "  Kept: your git checkout, secrets/, and every unrelated Docker resource."
	@echo "  Cloud resources (mesh Job, GCS exchange) are NEVER touched by this target."

# The five gates. Each answers ONE question; see docs/development/gates.md.
#
#   A  check-fast        am I safe to keep coding?          seconds, hermetic
#   B  check             may this COMMIT produce a release? the full source gate
#   C  release-validate  does the ARTIFACT work?            builds it once, tests THAT
#      release-publish   push THOSE bytes, record digests   never rebuilds
#   D  mesh-preflight    may the mesh image be promoted?    read-only, no source tests
#
# The direction matters: B gates a COMMIT, C builds ONE artifact set from that commit, validates
# it (native-terminal included, mandatory) and publishes those exact bytes to obtain immutable
# registry digests; D checks that the recorded digests are what would be promoted; `mesh-deploy`
# promotes the mesh image. Only the MESH image is deployed remotely - the API, the pipeline and
# every data store run locally.

check-fast: ##! GATE A - seconds: lint + import sweep + graph wiring + configuration certification. No services, no containers.
	$(RUFF) check $(LINT_PATHS)
	@# BLOCKING: no supported configuration may exist outside the settings catalogue - no
	@# undeclared read, no direct bypass, and no environment name built from data.
	$(PY) devtools/quality/config_inventory.py --full
	$(PY) -m pytest -q -p no:cacheprovider tests/unit/pipeline/test_graph_real.py \
		tests/unit/infra/test_import_sweep.py \
		tests/unit/hygiene/test_publication_authority_manifest.py

check: lint typecheck dependencies ## GATE B - may this commit produce a release? (ruff + mypy + deps + the full hermetic suite + UI)
	$(PY) -m pytest -q tests/unit -m "not external_fixture"
	$(PY) -m pytest -q tests/ui

release-validate: ##! GATE C - build the deployable images ONCE from this commit and validate THOSE artifacts, native-terminal included (writes deploy/output/release.json)
	@bash devtools/release/validate.sh

release-publish: ##! GATE C, part 2 - push the exact validated images without rebuilding and record their immutable registry digests
	@bash devtools/release/publish.sh

mesh-preflight: ##! GATE D - read-only: may the mesh image Gate C validated be promoted here? Deploys nothing.
	@bash deploy/gcp/scripts/deploy-preflight.sh

test: test-fast ## Run the everyday hermetic unit suite

logs: ## Tail all service logs
	docker compose logs -f --tail=100

clean: ## Remove this Compose project's containers, volumes, and locally built images
	docker compose down -v --rmi local
	@echo "This Compose project's containers, volumes and locally built images removed."

mesh-setup: ## FIRST developer in a blank GCP project: build, validate, publish and provision the mesh job
	@# Each step below is an existing authority invoked unchanged. Every one is idempotent, so an
	@# interrupted run resumes by re-running this target.
	@# Every prerequisite is checked BEFORE the first mutation, because this target creates real
	@# billable resources and a run that dies halfway has already made some of them. gcloud is
	@# required HERE and only here: provisioning is the one thing that needs the CLI. It is never
	@# invoked to sign anybody in - an unauthenticated host is told the command and stops.
	@# The configuration gate below is the same one dev-up runs: it proves the required
	@# settings are present and that the credential the operator placed is loadable, so a
	@# missing key or an unusable ADC file stops this before any resource exists.
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	 if [ -z "$${GCP_PROJECT_ID:-}" ]; then \
	   echo "  Set GCP_PROJECT_ID in .env first - it names the project this will provision into."; \
	   exit 1; \
	 fi; \
	 if ! command -v gcloud >/dev/null 2>&1; then \
	   echo ""; \
	   echo "  Nothing was created - provisioning needs the Google Cloud CLI on this host."; \
	   echo ""; \
	   echo "    install: https://cloud.google.com/sdk/docs/install"; \
	   echo ""; \
	   echo "  gcloud is a HOST tool. It is in no image and no runtime dependency."; \
	   echo ""; exit 1; \
	 fi; \
	 $(PY) devtools/env/check_runtime_config.py || exit 1; \
	 if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then \
	   echo ""; \
	   echo "  Nothing was created - this host is not authenticated to Google Cloud."; \
	   echo ""; \
	   echo "    run: gcloud auth application-default login"; \
	   echo ""; \
	   echo "  Then rerun: make mesh-setup"; \
	   echo ""; exit 1; \
	 fi; \
	 echo ""; \
	 echo "  This BUILDS and PUSHES images and CREATES billable Google Cloud resources in"; \
	 echo "  project '$$GCP_PROJECT_ID': an Artifact Registry repo, a GCS exchange bucket, a"; \
	 echo "  service account, IAM bindings and a Cloud Run job. The job is private."; \
	 echo ""; \
	 if [ "$${ASSUME_YES:-0}" != "1" ]; then \
	   printf "  Type the project id to continue: "; read -r reply; \
	   [ "$$reply" = "$$GCP_PROJECT_ID" ] || { echo "  Cancelled - nothing was created."; exit 1; }; \
	 fi
	@# THE LOCAL CALLER, derived rather than typed. Provisioning grants the principal this machine
	@# authenticates as the two roles the adapter actually uses; asking the operator to spell it
	@# out meant a correct-looking run that could not invoke the job it had just created.
	@ACCOUNT="$$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"; \
	 [ -n "$$ACCOUNT" ] || { echo "  No active gcloud account - run: gcloud auth login"; exit 1; }; \
	 case "$$ACCOUNT" in *.gserviceaccount.com) echo "  Local caller: serviceAccount:$$ACCOUNT";; \
	                     *) echo "  Local caller: user:$$ACCOUNT";; esac
	$(MAKE) mesh-image
	$(MAKE) release-validate
	@# Publication needs a registry to push to, and a blank project has neither a discovered
	@# environment nor the repository. Establishing both is deploy/gcp's own composite target, so
	@# the order lives there and nothing about discovery or provisioning is restated here.
	$(MAKE) -C deploy/gcp publication-target
	$(MAKE) release-publish
	@# The binding that matters: apply-iam grants the local caller only when MESH_INVOKER is
	@# set, and it grants exactly the job-execution and exchange-object roles the adapter
	@# uses - nothing wider. The report above is informational; this is the value provisioning
	@# actually receives.
	@ACCOUNT="$$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"; \
	 case "$$ACCOUNT" in *.gserviceaccount.com) MESH_INVOKER="serviceAccount:$$ACCOUNT";; \
	                     *) MESH_INVOKER="user:$$ACCOUNT";; esac; \
	 export MESH_INVOKER; bash deploy/gcp/scripts/deploy.sh
	@# .env is the developer's file and stays input-only: the names provisioned are the names it
	@# already declared, so nothing needs to go back into it. This prints them, for comparison.
	@$(PY) devtools/env/print_deployment_settings.py
	@# The same read-only authority the operator can run by hand. Provisioning that cannot be
	@# diagnosed as healthy has not finished, so this decides the exit code.
	$(MAKE) mesh-doctor
	@echo ""
	@echo "  Provisioned and verified. Next:  make dev-up"
	@echo ""

mesh-deploy: ## Provision or update the Cloud Run MESH JOB and its exchange bucket (idempotent)
	@bash deploy/gcp/scripts/deploy.sh

mesh-destroy: ## Remove ONLY the cloud resources the deployment record says mesh-setup created (dry run by default)
	@# Local uninstall is `make dev-uninstall`; this is the cloud half, and they are deliberately
	@# separate commands. Ownership comes from the deployment record, never from a name pattern.
	@bash deploy/gcp/scripts/mesh-destroy.sh $(DESTROY_ARGS)

mesh-doctor: ## Diagnose mesh-executor configuration (read-only; reveals no values)
	@bash deploy/gcp/scripts/deploy-doctor.sh

mesh-adopt: ## Fill .env's mesh settings from the executor this session can already see
	@# The doctor can say a setting is missing but not what it should be, and the four values are
	@# not ones an operator invents - they name resources that already exist. Everything needed to
	@# read them off the project is in the session the operator just authenticated, so asking them
	@# to go and find the same values by hand is asking them to re-derive what this can look up.
	@# It creates nothing, and fills only settings that are empty.
	@$(PY) devtools/env/adopt_executor.py

# Advanced / internal targets (shown by `make help-all`)

#: The one working-tree content digest. Computed by tests/integration/source_digest.sh and used
#: by nothing else - the integration preflight reads the same script, so an image can only look
#: current if it really was built from these bytes. Recursive on purpose: only the recipes below
#: pay for the shell call.
SOURCE_DIGEST = $(shell bash tests/integration/source_digest.sh)
# The compose project this checkout owns - the scope of every destructive target here. Compose
# derives it by lowercasing the directory name, so this must too, or a destructive target would
# match nothing and quietly leave what it promised to remove.
COMPOSE_PROJECT ?= $(shell basename "$(CURDIR)" | tr '[:upper:]' '[:lower:]')

rebuild: ##! Rebuild app images from the CURRENT working tree and restart them (no teardown, no volume loss)
	@# The digest is stamped into every application image as MESH_SOURCE_TREE so
	@# `make test-integration` can prove the image matches the checkout. Supplying it here is what
	@# makes the supported rebuild self-contained - no operator has to pass a build argument.
	MESH_SOURCE_TREE="$(SOURCE_DIGEST)" APP_VERSION="$(PRODUCT_VERSION)" docker compose up -d --build api worker worker-utility beat

restart: ##! Full teardown + clean no-cache rebuild + up
	docker compose down && APP_VERSION="$(PRODUCT_VERSION)" docker compose build --no-cache && APP_VERSION="$(PRODUCT_VERSION)" docker compose up -d

logs-api: ##! Tail API logs only
	docker compose logs -f --tail=100 api
logs-worker: ##! Tail simulation worker logs only
	docker compose logs -f --tail=100 worker
logs-all: ##! Tail api + worker + worker-utility
	docker compose logs -f --tail=100 api worker worker-utility

migrate: ##! Run Alembic migrations manually (they auto-run on API start)
	docker compose exec -w /srv api alembic upgrade head
migrate-auto: ##! Auto-generate a migration from model changes (msg="…")
	docker compose exec api alembic revision --autogenerate -m "$(msg)"
db-shell: ##! Open a psql shell in the postgres container
	docker compose exec postgres psql -U $${POSTGRES_USER:-meshpipeline} -d $${POSTGRES_DB:-meshpipeline}
shell-api: ##! Shell into the API container
	docker compose exec api bash
shell-worker: ##! Shell into the worker container
	docker compose exec worker bash

# test tiers (each its own pytest session; see docs/development/overview.md)
test-fast: ##! Hermetic unit tier (framework deps stubbed; no services/containers/licensed data)
	$(PY) -m pytest tests/unit -m "not external_fixture"
test-random: ##! Unit tier under a fixed matrix of random ORDER seeds (surfaces inter-test leakage)
	@for seed in 1 42 8675309; do \
	  echo "── randomly-seed=$$seed ──"; \
	  $(PY) -m pytest -q tests/unit -m "not external_fixture" -p no:cacheprovider --randomly-seed=$$seed || exit 1; \
	done
test-integration: ##! Real-dependency tier inside the worker container, against a database provisioned for this run
	@# The tier drops and rebuilds the public schema on every test, so it must never be handed the
	@# shared development database - doing so once destroyed seven pre-existing job rows. The
	@# runner below creates ONE uniquely named database for this run, stamps it with a run identity
	@# the suite re-proves from the live server, and drops it on exit. It also refuses to run at
	@# all if the worker image was built from a different source tree than the checkout.
	@$(MAKE) --no-print-directory wait-postgres
	@bash tests/integration/run_disposable.sh

wait-postgres: ##! Block until the local postgres service reports healthy (readiness, never sleep)
	@for _ in $$(seq 1 60); do \
	   state=$$(docker inspect -f '{{.State.Health.Status}}' $$(docker compose ps -q postgres) 2>/dev/null); \
	   [ "$$state" = "healthy" ] && break; \
	 done; \
	 [ "$$state" = "healthy" ] || { echo "  postgres is $${state:-unreachable} - the real-service tier cannot run"; exit 1; }; \
	 echo "  postgres healthy"
test-container-smoke: ##! HERMETIC image check: build the pipeline image, verify import/version/prompts/entrypoint, run ONLY the service-free integration tests (no Postgres/Redis/MinIO)
	docker build --target pipeline --build-arg APP_VERSION="$(PRODUCT_VERSION)" -t meshpipeline-pipeline:test .
	# image-level assertions: the installed distribution imports, reports the version the checkout
	# declares, ships exactly the live prompts and no closer asset, and celery is importable.
	# The expected prompt set is the policy's own REQUIRED_PROMPTS, so adding a role updates one list.
	docker run --rm --network none -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x -e POSTGRES_PASSWORD=x \
	  --entrypoint python meshpipeline-pipeline:test -c "\
import meshpipeline, os, pathlib, meshpipeline.settings.policy as pol; \
p = pathlib.Path(meshpipeline.__file__).parent / 'prompts'; \
names = sorted(str(x.relative_to(p)) for x in p.rglob('*.txt')); \
assert names == sorted(f for _, f in pol.REQUIRED_PROMPTS), names; \
assert not list(p.rglob('outcome.txt')); \
assert [n for n, _ in pol.REQUIRED_PROMPTS] == ['reviewer_system', 'intake_system', 'builder_system']; \
import meshpipeline.adapters.pipeline_execution.celery_app as c; assert c.celery_app is not None; \
print('image smoke OK: version', meshpipeline.__version__)"
	# hermetic integration subset - NO services, NO network. A service-touching test is NOT marked
	# `hermetic`, so it cannot be selected here; --network none makes any accidental dial fail hard.
	docker run --rm --network none -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x -e POSTGRES_PASSWORD=x \
	  -v "$$PWD/tests:/srv/tests:ro" --entrypoint python meshpipeline-pipeline:test \
	  -m pytest -q -rs -o asyncio_mode=auto -p no:cacheprovider -m hermetic /srv/tests/integration

test-container-integration: ##! DEPENDENCY-BACKED: provision Postgres+Redis+MinIO, health-check all three, run the FULL integration tier in the pipeline image, fail loudly on any missing service, then tear the stack down
	@# The image is built HERE, stamped with the working-tree digest, so the runner can prove it
	@# was built from this checkout instead of building one itself mid-preflight.
	@# --target validation, not pipeline: it derives FROM pipeline, so the wheel, the runtime
	@# dependencies and the image stamp are the same bytes - it only adds requirements/dev.txt.
	@# Without pytest-randomly the runner's randomised pass silently runs in declaration order.
	docker build --target validation --build-arg MESH_SOURCE_TREE="$(SOURCE_DIGEST)" --build-arg APP_VERSION="$(PRODUCT_VERSION)" -t $(CONTAINER_TIER_IMAGE) .
	@bash tests/integration/run_in_container.sh

test-container: test-container-integration ##! Alias of test-container-integration (the full dependency-backed tier). For the hermetic image check use `make test-container-smoke`.

# native mesh-toolchain tiers (run INSIDE the `mesh` image; see docs/development/overview.md)
MESH_IMAGE ?= meshpipeline-mesh:native
#: The image the dependency-backed container tier runs. Built by its Make target, never by the runner.
CONTAINER_TIER_IMAGE ?= meshpipeline-pipeline:test

MESH_TOOLCHAIN_TAG ?= meshpipeline-mesh-toolchain:openfoam2412.260127-1

mesh-image: ##! Build the production mesh image from source - OpenFOAM 2412 + vmtk + gmsh + the app. Self-contained: no registry, no prebuilt base, no credentials. First build downloads OpenFOAM (slow); Docker caches that layer afterwards.
	@# Stamped with the same working-tree digest the application images carry, so the native
	@# runners can prove the image was built from the checkout they are testing.
	docker build --target mesh --build-arg MESH_SOURCE_TREE="$(SOURCE_DIGEST)" --build-arg APP_VERSION="$(PRODUCT_VERSION)" -t $(MESH_IMAGE) .
	@echo "built $(MESH_IMAGE)"

mesh-toolchain: ##! OPTIONAL: build just the pinned native floor (OpenFOAM 2412 + vmtk + gmsh libs) to prewarm the cache or inspect it. `make mesh-image` builds this automatically - you never need to run it first.
	docker build --target mesh-toolchain -t $(MESH_TOOLCHAIN_TAG) .
	@echo "built $(MESH_TOOLCHAIN_TAG)"
test-native-smoke: ##! Build the mesh image and run the BOUNDED packaging capability smoke inside it: every engine executable/module present, imports/initialises, versionable, and the dispatch registry resolves all five. FAILS the release if a declared engine (e.g. gmsh) cannot initialize in the mesh image.
	@bash tests/native/run_tier.sh smoke
test-native-all: ##! Build the mesh image and run the FULL native tier inside it: packaging smoke + minimal five-engine native execution + controlled native failure cases (the longer multiregion/VMTK cases included - expect minutes). NOT collected by the hermetic run; part of the blocking pre-release plan. Excludes the service-backed native-terminal matrix (see test-native-terminal).
	@bash tests/native/run_tier.sh all
test-native-terminal: ##! Build the mesh image + provision real Postgres/Redis/MinIO, then drive each engine's GENUINE native mesh through the REAL production terminal chain (_run_async) and assert REST/Redis/WS/final_result agreement, restart/replay, and native-terminal failure containment. Deterministic Reviewer; no model calls. Local proof only - not hosted.
	@bash tests/native/run_terminal_matrix.sh
validate: ##! Run one tier of the test contract in the PINNED validation image (TIER=collect|unit|ui|integration|native|release)
	@# The host tier assumes `make setup` prepared a cp311 .venv. Where that is not true - a host on
	@# another interpreter, or none - this runs the same tests in the pipeline image plus
	@# requirements/dev.txt, with the checkout mounted read-only. Same wheels as production, same
	@# tests, and a result file that proves which ones actually executed.
	@bash devtools/validation/run.sh $(or $(TIER),collect)

test-external-fixtures: ##! Only tests needing licensed CAD (tests/fixtures/external/); loud on absence
	$(PY) -m pytest tests/unit -m external_fixture -rs
test-ui: ##! Drive the shipped UI in a REAL headless Chrome (tests/ui/). Needs Chrome/Chromium or CHROME_BIN; fails loudly without one.
	$(PY) -m pytest tests/ui
test-all: ##! Every tier as separate sessions (fast, ui, external-fixtures, integration)
	$(MAKE) test-fast && $(MAKE) test-ui && $(MAKE) test-external-fixtures && $(MAKE) test-integration
smoke: ##! Graph wiring + import sweep (no API calls, no Docker)
	$(PY) -m pytest -q tests/unit/pipeline/test_graph_real.py tests/unit/infra/test_import_sweep.py

# static gates + build
lint: ##! Ruff lint (src + tests + alembic + devtools)
	$(RUFF) check $(LINT_PATHS)
typecheck: ##! mypy ratchet - 0 exit while the baseline holds, fail on any NEW error
	$(PY) devtools/quality/mypy_ratchet.py
dependencies: ##! Validate the one dependency source of truth (requirements/runtime.txt + dev.txt)
	$(PY) devtools/quality/check_dependency_drift.py
deps: dependencies ##! Alias for `make dependencies`
# Maintainer check, not a release step: prove the distribution still builds and still contains
# exactly ONE top-level package, then delete every artifact. Nothing is uploaded, and nothing is
# left behind - a stale dist/ shadows the source tree (see test_no_stale_build_artifacts_shadow_
# the_source), so the cleanup runs from a trap and fires on failure as well as success.
wheel: ##! Build + validate the distribution wheel, then remove all local build artifacts
	@set -euo pipefail; \
	cleanup() { rm -rf build dist src/*.egg-info; }; \
	trap cleanup EXIT; \
	cleanup; \
	$(PY) -m build --wheel; \
	$(PY) -c "import glob,zipfile,sys; z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]); \
	n=z.namelist(); tops={x.split('/')[0] for x in n if '/' in x and not x.endswith('.dist-info')}; \
	bad=[t for t in tops if t not in ('meshpipeline',) and not t.endswith('.dist-info')]; \
	sys.exit(f'FAIL: wheel contains foreign top-level entries: {bad}') if bad else \
	print(f'wheel OK: {len(n)} files, single top-level package')"

clean-workspaces: ##! Remove all job workspaces (failed jobs) from the worker
	docker compose exec worker find /srv/workspaces -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

# Help

help: ## Show the common developer commands
	@printf '\nHexera - common commands\n\n'
	@printf '  \033[36m%-16s\033[0m %s\n' \
	  setup           "one-time: prepare a fresh clone (venv, deps, dirs, images)" \
	  dev-up          "start the local stack (UI at http://localhost:8000/ui)" \
	  dev-down        "stop the local stack" \
	  check           "ruff + mypy + dependency check + the full unit suite + the UI suite" \
	  test            "run the everyday hermetic unit suite" \
	  logs            "tail all service logs" \
	  clean           "remove this Compose project's containers, volumes, and locally built images" \
	  mesh-adopt      "mesh job already exists? fill .env from it (creates nothing)" \
	  mesh-setup      "no mesh job yet? provision one (creates billable cloud resources)" \
	  mesh-deploy     "provision/update the Cloud Run mesh job + exchange bucket" \
	  mesh-doctor     "diagnose mesh-executor configuration (read-only)"
	@printf '\nFirst time?  make setup, then follow docs/getting-started/setup.md\n'
	@printf '  Setup finishing does not mean the stack can start: dev-up needs a gcloud identity,\n'
	@printf '  a credential file and a Cloud Run mesh job as well.\n'
	@printf '  Which path you are on is decided by  gcloud run jobs list --project=<PROJECT_ID>:\n'
	@printf '  a job is listed -> make mesh-adopt.   nothing listed -> make mesh-setup.\n'
	@printf 'Advanced/internal targets:  make help-all\n\n'

help-all: ## Show every target (including advanced/internal)
	@printf '\nAll targets (## public, ##! internal):\n\n'
	@grep -hE '^[a-zA-Z_-]+:.*?##!?' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?##"}; {tag=substr($$2,1,1)=="!" ? "[internal] " : ""; \
	    d=$$2; sub(/^!/,"",d); printf "  \033[36m%-22s\033[0m %s%s\n", $$1, tag, d}'
