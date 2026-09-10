# Console on dev (sub-project A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `apps/console` deployable to `hexera-dev` through the existing manual
`workflow_dispatch`, so sub-projects B+D and C are developed against a real deployment instead of
localhost.

**Architecture:** The console becomes a third release component beside `mesh` and `app` — built
once by Gate C, published by digest, promoted into the deployment env as `CONSOLE_IMAGE`, and
rolled out by a new `create-console-service.sh` stage that `deploy.sh` selects with a new
`console` component. Nothing about the existing two components changes shape; the console follows
the conventions they already established (digest-only references, Secret Manager container names
never values, declarative env with a drift refusal).

**Tech Stack:** Next 16 standalone output on Node, pnpm workspaces, Docker multi-stage, Cloud Run,
Secret Manager, GitHub Actions, bash + pytest for the deploy tooling.

**Spec:** `docs/superpowers/specs/2026-09-07-saas-console-design.md` (§4 is this plan's scope)

## Global Constraints

- **Every image reference in a deployment is digest-qualified.** A tag-only reference is refused,
  never re-resolved. `require_digest_reference` in `deploy/gcp/scripts/lib.sh` is the check.
- **No credential value ever enters a Cloud Run spec, a generated env file, or a log.** The
  deployment names a Secret Manager container using the `<SETTING>_SECRET` convention (so
  `AUTH_SECRET` → `AUTH_SECRET_SECRET`). `devtools/quality/check_deploy_secrets.py` is the gate.
- **Gate C builds; nothing else does.** `deploy.sh` promotes an already-validated artifact.
- **New deploy stages state their skip.** Use the existing `want` / `skipped` helpers in
  `deploy.sh` so a component not selected reads differently from one that was reused.
- **Pinned base images.** Every `FROM` in the `Dockerfile` is pinned by digest
  (`ubuntu:22.04@sha256:0e0a0f…`). The console's Node base follows the same rule.
- **`apps/console` only.** `apps/admin-console` gets no image and no service in this plan.
- **Product version authority:** `src/meshpipeline/__init__.py`'s `__version__`, passed as
  `--build-arg APP_VERSION`. Never restated as a literal.
- **Existing action pins are reused verbatim.** New CI steps use
  `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`.

---

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `deploy/gcp/scripts/create-console-service.sh` | Provision the Cloud Run console service: identity, secret references, declarative env, rollout, invoker policy |
| `tests/unit/deploy/test_console_service_stage.py` | Drive that script against a fake gcloud and assert what it would mutate |
| `tests/unit/deploy/test_web_ci_lane.py` | Assert the workspace CI lane exists, is scope-gated, and is in the aggregate gate |
| `tests/unit/deploy/test_console_image_target.py` | Assert the Dockerfile console target's shape (pinned base, non-root, port) |

**Modified**

| Path | Change |
|---|---|
| `apps/console/next.config.ts` | `output: "standalone"` + `outputFileTracingRoot` for the workspace |
| `Dockerfile` | Two new stages: `console-build`, `console` |
| `.github/workflows/ci.yml` | `run_web` scope output, a `web` lane, the lane added to `ci-gate` |
| `devtools/release/validate.sh` | Build and smoke the `console` component; record it |
| `deploy/gcp/scripts/promote-release.sh` | Resolve `console` and write `CONSOLE_IMAGE` |
| `deploy/gcp/scripts/bootstrap-env.sh` | Declare the console's config and secret container names |
| `deploy/gcp/scripts/validate-config.sh` | Validate the console block |
| `deploy/gcp/scripts/create-secrets.sh` | Two new secret containers, with the console SA as reader |
| `deploy/gcp/scripts/deploy.sh` | `console` in `_known`; a console stage after the API |
| `deploy/gcp/scripts/write-deployment-state.sh` | Record `console_service` and its digest |
| `.github/workflows/deploy.yml` | `console` in the on-merge default component set |
| `docs/deployment/environments-and-delivery.md` | Document the console tier |
| `apps/console/.env.example` | Match the deployed variable set |

**Design note — why the console is its own component and not folded into `images`.** `images`
today means "the app and mesh images this deployment promotes". A console that rolled out only as
part of that could never be deployed alone, which is exactly what iterating on B+D and C needs.
Making it its own component costs one entry in `_known` and buys `DEPLOY_COMPONENTS=console`.

**Resolved from the spec's open questions.** Open question 1 (custom domain vs `run.app`) is
answered here as **`run.app`**: `apps/console/src/auth.ts` already sets `trustHost: true`, so
Auth.js derives its URL from the request host and needs no `AUTH_URL`. A custom domain is a later
additive change and is not in this plan.

---

### Task 1: The workspace CI lane

Nothing in `apps/` or `packages/` is checked by CI today. This lands first so every task after it
is covered.

**Files:**
- Modify: `.github/workflows/ci.yml` (scope step ~line 87-116; new job after `ui` at line 363; gate at line 381-435)
- Test: `tests/unit/deploy/test_web_ci_lane.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a CI job named `web` gated on `needs.preflight.outputs.run_web == 'true'`, and a
  `run_web` output on the `preflight` job. Later tasks rely on the lane existing but do not import
  anything from it.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the pnpm workspace is checked by CI and counted by the aggregate gate.
# Boundaries: it reads the workflow document; it runs no build and reaches no network.
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
CI = REPO / ".github" / "workflows" / "ci.yml"


def _doc() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_preflight_publishes_a_web_scope_output():
    jobs = _doc()["jobs"]
    outputs = jobs["preflight"]["outputs"]
    assert "run_web" in outputs, (
        "the scope step must publish run_web, so a documentation-only change skips the "
        "workspace lane exactly as it skips the python and image lanes")


def test_web_lane_exists_and_is_scope_gated():
    web = _doc()["jobs"]["web"]
    assert web["if"] == "needs.preflight.outputs.run_web == 'true'"
    steps = yaml.dump(web["steps"])
    for expected in ("pnpm install --frozen-lockfile", "pnpm typecheck",
                     "pnpm lint", "pnpm test", "pnpm build"):
        assert expected in steps, f"the web lane does not run {expected!r}"


def test_web_lane_is_counted_by_the_gate():
    gate = _doc()["jobs"]["ci-gate"]
    assert "web" in gate["needs"], "ci-gate does not wait for the web lane"
    body = yaml.dump(gate["steps"])
    assert "web=${{ needs.web.result }}" in body, (
        "ci-gate waits for the web lane but never reads its result, so a failing "
        "workspace would still post a green required check")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_web_ci_lane.py -v`
Expected: FAIL — `KeyError: 'run_web'` on the first test, `KeyError: 'web'` on the others.

- [ ] **Step 3: Add `run_web` to the scope step**

In `.github/workflows/ci.yml`, in the `preflight` job's `outputs:` block beside `run_images`:

```yaml
      run_web: ${{ steps.scope.outputs.run_web }}
```

In the `Detect changed scope` step's script, beside `run_images=true`:

```bash
          run_python=true
          run_images=true
          run_web=true
          if [ "$docs_only" = "true" ]; then
            run_python=false
            run_images=false
            run_web=false
          fi
```

and in the `{ … }` output block beside `echo "run_images=$run_images"`:

```bash
            echo "run_web=$run_web"
```

- [ ] **Step 4: Add the `web` lane**

Insert immediately after the `ui` job (before `ci-gate`):

```yaml
  web:
    name: Workspace (pnpm)
    needs: [preflight]
    if: needs.preflight.outputs.run_web == 'true'
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ needs.preflight.outputs.checkout_ref }}
          persist-credentials: false
      - uses: actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903 # v7.0.0
        with:
          node-version: 24
      # The workspace pins its package manager in package.json ("packageManager": "pnpm@11.3.0");
      # corepack honours that pin rather than installing whatever is newest.
      - run: corepack enable
      - run: pnpm install --frozen-lockfile
      - run: pnpm typecheck
      - run: pnpm lint
      - run: pnpm test
      - run: pnpm build
```

If `actions/setup-node`'s pinned SHA above does not resolve, obtain the current v7 pin with
`gh api repos/actions/setup-node/git/ref/tags/v7 --jq .object.sha` and use that, keeping the
`# v7.0.0` trailing comment convention every other pin in this file uses.

- [ ] **Step 5: Count it in the gate**

In `ci-gate`, add `- web` to `needs:` after `- ui`, and add to `SELECTED_RESULTS`:

```yaml
            web=${{ needs.web.result }}
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_web_ci_lane.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Verify the workspace actually passes what the lane will run**

Run: `pnpm install --frozen-lockfile && pnpm typecheck && pnpm lint && pnpm test && pnpm build`
Expected: all five succeed. If any fails, fix it now — the lane exists to catch exactly this, and
landing a red lane blocks every later task.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/ci.yml tests/unit/deploy/test_web_ci_lane.py
git commit -m "ci: check the pnpm workspace and count it in the gate"
```

---

### Task 2: Standalone output and the console image target

**Files:**
- Modify: `apps/console/next.config.ts`
- Modify: `Dockerfile` (append two stages after the `validation-browser` stage)
- Test: `tests/unit/deploy/test_console_image_target.py`

**Interfaces:**
- Consumes: the `web` lane from Task 1 (the build must pass there first).
- Produces: a Dockerfile target named `console` that listens on `$PORT` (default 8080), runs as
  uid 1000, and whose entry command is `node apps/console/server.js`. Task 3 builds this target;
  Task 8 deploys it.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the console image target is pinned, unprivileged and serves on the port Cloud Run sets.
# Boundaries: it reads the Dockerfile; it builds nothing.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
DOCKERFILE = REPO / "Dockerfile"
NEXT_CONFIG = REPO / "apps" / "console" / "next.config.ts"


def _stage(name: str) -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"^FROM ", text, flags=re.M)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[s:end]
        if re.match(rf"^FROM \S+ AS {re.escape(name)}\s*$", block.splitlines()[0]):
            return block
    raise AssertionError(f"no Dockerfile stage named {name!r}")


def test_console_stages_pin_their_base_by_digest():
    for name in ("console-build", "console"):
        first = _stage(name).splitlines()[0]
        assert "@sha256:" in first, (
            f"stage {name} does not pin its base by digest: {first!r}. Every other FROM in "
            f"this file is pinned, and an unpinned base makes the build unreproducible.")


def test_console_runs_unprivileged():
    block = _stage("console")
    assert re.search(r"^USER (?!root)", block, flags=re.M), (
        "the console stage does not drop to a non-root user")


def test_console_honours_the_cloud_run_port():
    block = _stage("console")
    assert "PORT=8080" in block, "the console stage does not default PORT"
    assert "HOSTNAME=0.0.0.0" in block, (
        "Next's standalone server binds localhost unless HOSTNAME is set, which on Cloud Run "
        "means the container answers nothing and the revision never becomes ready")
    assert 'CMD ["node", "apps/console/server.js"]' in block


def test_next_emits_standalone_output_rooted_at_the_workspace():
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    assert 'output: "standalone"' in text
    assert "outputFileTracingRoot" in text, (
        "in a pnpm workspace Next traces from the app directory unless told otherwise, and the "
        "standalone bundle then omits the workspace packages the console imports")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_image_target.py -v`
Expected: FAIL — `AssertionError: no Dockerfile stage named 'console-build'`

- [ ] **Step 3: Configure standalone output**

Replace `apps/console/next.config.ts` with:

```typescript
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

const here = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  transpilePackages: ["@hexera/api-client"],
  // The container copies one self-contained tree rather than installing at image build time.
  output: "standalone",
  // In a pnpm workspace, tracing must start at the repository root: the console imports
  // @hexera/api-client from packages/, and a trace rooted at apps/console silently omits it -
  // which fails at runtime, not at build time.
  outputFileTracingRoot: path.join(here, "..", ".."),
};

export default nextConfig;
```

- [ ] **Step 4: Resolve the Node base digest**

Run:

```bash
docker pull node:24-slim
docker image inspect node:24-slim --format '{{index .RepoDigests 0}}'
```

Use the printed `node:24-slim@sha256:…` verbatim in both stages below. Do not skip this — an
unpinned base fails Step 7's test and breaks build reproducibility.

- [ ] **Step 5: Add the two stages**

Append to `Dockerfile` (after the `validation-browser` stage). Substitute the digest from Step 4
for `<NODE_DIGEST>`:

```dockerfile
# ---------------------------------------------------------------------------
# THE CONSOLE. The Next.js browser front door, and the only Node image this repository builds.
# It shares no layer with the stages above: those are a Python distribution and a native mesh
# toolchain, and stacking a Node runtime on either would carry gigabytes this workload never runs.
FROM node:24-slim@<NODE_DIGEST> AS console-build
WORKDIR /build
ENV CI=1
# The workspace pins its package manager in package.json; corepack honours that pin.
RUN corepack enable
# The manifests first, so a dependency-only change is the only thing that re-resolves the store.
COPY pnpm-workspace.yaml pnpm-lock.yaml package.json tsconfig.base.json ./
COPY packages/ ./packages/
COPY apps/console/package.json ./apps/console/
RUN pnpm install --frozen-lockfile --filter @hexera/console...
COPY apps/console/ ./apps/console/
RUN pnpm --filter @hexera/console build

FROM node:24-slim@<NODE_DIGEST> AS console
ARG APP_VERSION
LABEL org.opencontainers.image.title="Hexera Console" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="The Hexera browser console" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"
WORKDIR /srv
# HOSTNAME is load-bearing: Next's standalone server binds localhost without it, so on Cloud Run
# the container would start, answer nothing, and the revision would never become ready.
ENV NODE_ENV=production \
    PORT=8080 \
    HOSTNAME=0.0.0.0
# The standalone tree carries its own trimmed node_modules; static and public are not traced into
# it and are copied beside the server that serves them.
COPY --from=console-build /build/apps/console/.next/standalone/ ./
COPY --from=console-build /build/apps/console/.next/static/ ./apps/console/.next/static/
COPY --from=console-build /build/apps/console/public/ ./apps/console/public/
RUN useradd -m -u 1000 console && chown -R console:console /srv
USER console
EXPOSE 8080
CMD ["node", "apps/console/server.js"]
```

- [ ] **Step 6: Build it and prove it serves**

```bash
APP_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' src/meshpipeline/__init__.py | head -1)"
docker build --target console --build-arg APP_VERSION="$APP_VERSION" -t hexera-console:local .
docker run --rm -d --name console-smoke -p 8080:8080 \
  -e AUTH_SECRET=smoke-only-not-a-real-secret \
  -e CONSOLE_AUTH_USERS='[]' \
  hexera-console:local
sleep 5
curl -fsS http://localhost:8080/api/internal/health && echo OK
docker rm -f console-smoke
```

Expected: the health route returns 200 and `OK` prints.

- [ ] **Step 7: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_image_target.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add Dockerfile apps/console/next.config.ts tests/unit/deploy/test_console_image_target.py
git commit -m "build: add the console image target on standalone Next output"
```

---

### Task 3: The console as a release component

**Files:**
- Modify: `devtools/release/validate.sh` (component map ~line 255, the image-behaviour stage ~line 283, the components JSON ~line 737)
- Test: `tests/unit/deploy/test_console_release_component.py`

**Interfaces:**
- Consumes: the `console` Dockerfile target from Task 2.
- Produces: `deploy/output/release.json` carrying a `console` key under `components`, with
  `dockerfile_target`, `local_tag`, `local_image_id`, `workloads: ["console-service"]`.
  `devtools/release/publish.sh` already iterates `${REC_COMPONENTS}` generically and needs no
  change. Task 4 reads `components.console.reference`.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify Gate C builds, smokes and records the console alongside app and mesh.
# Boundaries: it reads the validation script's declarations; it runs no docker build.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "devtools" / "release" / "validate.sh"
PUBLISH = REPO / "devtools" / "release" / "publish.sh"


def test_console_is_a_built_component():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "[console]=console" in text, (
        "the component-to-target map does not name the console")
    assert "for comp in app mesh console;" in text, (
        "the build loop does not cover the console, so no console image is ever validated")


def test_console_image_is_smoked_before_it_is_recorded():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "console image: serves its health route" in text, (
        "an image that builds and cannot serve is a green gate and a broken deploy")


def test_console_is_written_into_the_record():
    text = VALIDATE.read_text(encoding="utf-8")
    assert '"console":' in text, "the components JSON does not declare the console"
    assert '"console-service"' in text, (
        "the console component does not name the workload it becomes")


def test_publication_needs_no_per_component_change():
    text = PUBLISH.read_text(encoding="utf-8")
    assert "for comp in ${REC_COMPONENTS}" in text, (
        "publication stopped iterating the record's components; adding one would now "
        "silently not publish it")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_release_component.py -v`
Expected: FAIL on the first three tests; the fourth passes already.

- [ ] **Step 3: Build the console component**

In `devtools/release/validate.sh`, change the target map and the build loop:

```bash
declare -A TARGET=( [app]=pipeline [mesh]=mesh [console]=console )
declare -A IMAGE_ID=() IMAGE_TAG=()
for comp in app mesh console; do
```

- [ ] **Step 4: Smoke the console image**

In the `images: entrypoints and in-image behaviour` stage, after the existing `mesh` block, add:

```bash
if [ -n "${IMAGE_ID[console]}" ]; then
  # An image that builds and cannot serve is a green gate and a broken deploy. The container is
  # started on a published port with the two settings Auth.js requires at boot; the values are
  # deliberately not credentials - nothing here authenticates anyone.
  _cid="$(docker run --rm -d --label "amp-release=${STAMP}" -P \
            -e AUTH_SECRET=gate-c-not-a-real-secret -e CONSOLE_AUTH_USERS='[]' \
            "${IMAGE_TAG[console]}" 2>/dev/null || true)"
  if [ -n "${_cid}" ]; then
    _port="$(docker port "${_cid}" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')"
    _ok=1
    for _try in 1 2 3 4 5 6 7 8 9 10; do
      curl -fsS "http://127.0.0.1:${_port}/api/internal/health" >/dev/null 2>&1 && { _ok=0; break; }
      sleep 2
    done
    docker rm -f "${_cid}" >/dev/null 2>&1 || true
    if [ "${_ok}" = "0" ]; then
      record "console image: serves its health route" passed 1
    else
      record "console image: serves its health route" failed 1 \
        "the container started but never answered /api/internal/health"
    fi
  else
    record "console image: serves its health route" failed 1 "the container did not start"
  fi
else
  record "console image: serves its health route" not_run 1 "no console image was built"
fi
```

- [ ] **Step 5: Record the component**

Replace the `COMPONENTS="$(python3 - …)"` block with:

```bash
COMPONENTS="$(python3 - "${TARGET[app]}" "${IMAGE_TAG[app]:-}" "${IMAGE_ID[app]:-}" \
                        "${TARGET[mesh]}" "${IMAGE_TAG[mesh]:-}" "${IMAGE_ID[mesh]:-}" \
                        "${TARGET[console]}" "${IMAGE_TAG[console]:-}" "${IMAGE_ID[console]:-}" <<'PY'
import json, sys
at, atag, aid, mt, mtag, mid, ct, ctag, cid = sys.argv[1:10]
print(json.dumps({
  "app":     {"dockerfile_target": at, "local_tag": atag, "local_image_id": aid,
              "workloads": ["api-service", "pipeline-job"]},
  "mesh":    {"dockerfile_target": mt, "local_tag": mtag, "local_image_id": mid,
              "workloads": ["mesh-job"]},
  "console": {"dockerfile_target": ct, "local_tag": ctag, "local_image_id": cid,
              "workloads": ["console-service"]},
}))
PY
)"
```

- [ ] **Step 6: Update the header comment**

`validate.sh:26` says "COMPONENTS. Two images are deployable, each a Dockerfile target:". Change
"Two" to "Three" and add the console to the list beneath it. A comment that contradicts the code
is worse than no comment.

- [ ] **Step 7: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_release_component.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Run the existing deploy suite for regressions**

Run: `python -m pytest tests/unit/deploy -q`
Expected: PASS. `test_release_promotion.py` builds records by hand and may assert a component set;
if it fails, extend its fixtures with a `console` component rather than weakening the assertion.

- [ ] **Step 9: Commit**

```bash
git add devtools/release/validate.sh tests/unit/deploy/test_console_release_component.py tests/unit/deploy/test_release_promotion.py
git commit -m "release: build, smoke and record the console as a third component"
```

---

### Task 4: Promote the console image

**Files:**
- Modify: `deploy/gcp/scripts/promote-release.sh:54-85`
- Test: `tests/unit/deploy/test_release_promotion.py` (extend)

**Interfaces:**
- Consumes: `components.console.reference` from Task 3's record.
- Produces: `CONSOLE_IMAGE=<registry>/console@sha256:…` written into the deployment env file.
  Task 8 reads it.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/deploy/test_release_promotion.py`:

```python
CONSOLE_DIGEST = "sha256:" + "e5" * 32


def test_promotion_writes_the_console_image(tmp_path):
    """A record carrying a console component promotes it into the deployment env."""
    rec = make_record()
    rec["components"]["console"] = {
        "dockerfile_target": "console",
        "local_tag": "meshpipeline-console:abc1234",
        "local_image_id": "sha256:" + "f6" * 32,
        "registry_repository": REGISTRY,
        "publication_tag": "v0.0.0",
        "registry_digest": CONSOLE_DIGEST,
        "reference": f"{REGISTRY}/console@{CONSOLE_DIGEST}",
        "workloads": ["console-service"],
    }
    record_path = tmp_path / "release.json"
    record_path.write_text(json.dumps(rec), encoding="utf-8")
    env_file = tmp_path / "generated.env"
    env_file.write_text("DEPLOYMENT_ID=t\n", encoding="utf-8")

    subprocess.run(
        ["bash", str(REPO / "deploy" / "gcp" / "scripts" / "promote-release.sh")],
        check=True, capture_output=True, text=True,
        env={**os.environ, "RELEASE_RECORD": str(record_path),
             "DEPLOY_ENV_FILE": str(env_file)},
    )

    written = env_file.read_text(encoding="utf-8")
    assert f"CONSOLE_IMAGE={REGISTRY}/console@{CONSOLE_DIGEST}" in written


def test_promotion_refuses_a_tag_only_console_reference(tmp_path):
    """Deployment identity is a digest. A movable tag is refused, not resolved."""
    rec = make_record()
    rec["components"]["console"] = {
        "dockerfile_target": "console",
        "local_tag": "meshpipeline-console:abc1234",
        "local_image_id": "sha256:" + "f6" * 32,
        "registry_repository": REGISTRY,
        "publication_tag": "v0.0.0",
        "registry_digest": CONSOLE_DIGEST,
        "reference": f"{REGISTRY}/console:v0.0.0",
        "workloads": ["console-service"],
    }
    record_path = tmp_path / "release.json"
    record_path.write_text(json.dumps(rec), encoding="utf-8")
    env_file = tmp_path / "generated.env"
    env_file.write_text("DEPLOYMENT_ID=t\n", encoding="utf-8")

    done = subprocess.run(
        ["bash", str(REPO / "deploy" / "gcp" / "scripts" / "promote-release.sh")],
        capture_output=True, text=True,
        env={**os.environ, "RELEASE_RECORD": str(record_path),
             "DEPLOY_ENV_FILE": str(env_file)},
    )
    assert done.returncode != 0
    assert "digest" in (done.stderr + done.stdout).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_release_promotion.py -k console -v`
Expected: FAIL — `CONSOLE_IMAGE=` is not in the written env.

- [ ] **Step 3: Resolve and validate the console reference**

In `promote-release.sh`, after the `APP_REF` line:

```bash
CONSOLE_REF="$(_ref console)" || die "the release record names no 'console' component - re-run Gate C"
for ref in "${MESH_REF}" "${APP_REF}" "${CONSOLE_REF}"; do
```

- [ ] **Step 4: Write it into the env file**

Replace the writer block:

```bash
"${PY}" - "${ENV_TARGET}" "${MESH_REF}" "${APP_REF}" "${CONSOLE_REF}" <<'PY'
import pathlib, re, sys
path, mesh, app, console = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
p = pathlib.Path(path)
text = p.read_text() if p.exists() else ""
for key, value in (("MESH_IMAGE", mesh), ("APP_IMAGE", app), ("CONSOLE_IMAGE", console)):
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text += ("" if text.endswith("\n") or not text else "\n") + f"{key}={value}\n"
p.write_text(text)
PY
```

and add to the report at the bottom:

```bash
log "console (console service):  ${CONSOLE_REF}"
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_release_promotion.py -v`
Expected: PASS (all, including the two new)

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/scripts/promote-release.sh tests/unit/deploy/test_release_promotion.py
git commit -m "deploy: promote the validated console digest into the deployment env"
```

---

### Task 5: Declare the console's configuration

**Files:**
- Modify: `deploy/gcp/scripts/bootstrap-env.sh` (naming ~line 75-90; the generated block ~line 277-291)
- Test: `tests/unit/deploy/test_console_config_declaration.py`

**Interfaces:**
- Consumes: `DEPLOYMENT_ID` from discovery.
- Produces, in the generated deployment env: `CLOUDRUN_CONSOLE_SERVICE`, `CONSOLE_CPU`,
  `CONSOLE_MEMORY`, `CONSOLE_CONCURRENCY`, `CONSOLE_MIN_INSTANCES`, `CONSOLE_MAX_INSTANCES`,
  `CONSOLE_INGRESS`, `CONSOLE_ALLOW_UNAUTHENTICATED`, `CONSOLE_SERVICE_ACCOUNT`,
  `AUTH_SECRET_SECRET`, `CONSOLE_AUTH_USERS_SECRET`, `HEXERA_API_BASE_URL`,
  `NEXT_PUBLIC_HEXERA_API_BASE_URL`. Tasks 6, 7, 8, 10 read these.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the deployment env declares the console tier, and declares no secret value.
# Boundaries: it reads the generator's template; it discovers nothing and calls no cloud.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"

DECLARED = (
    "CLOUDRUN_CONSOLE_SERVICE", "CONSOLE_SERVICE_ACCOUNT",
    "CONSOLE_CPU", "CONSOLE_MEMORY", "CONSOLE_CONCURRENCY",
    "CONSOLE_MIN_INSTANCES", "CONSOLE_MAX_INSTANCES",
    "CONSOLE_INGRESS", "CONSOLE_ALLOW_UNAUTHENTICATED",
    "AUTH_SECRET_SECRET", "CONSOLE_AUTH_USERS_SECRET",
    "HEXERA_API_BASE_URL", "NEXT_PUBLIC_HEXERA_API_BASE_URL",
)


def test_every_console_setting_is_declared():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    missing = [name for name in DECLARED if f"{name}=" not in text]
    assert not missing, f"the deployment env declares no {missing}"


def test_the_console_names_secret_containers_never_values():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    # The <SETTING>_SECRET convention: the deployment names a container. A bare AUTH_SECRET=
    # assignment in the generated env would be the literal credential this file exists to avoid.
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("AUTH_SECRET="), (
            f"the generated env assigns AUTH_SECRET directly ({stripped!r}); it must name a "
            f"Secret Manager container via AUTH_SECRET_SECRET instead")
        assert not stripped.startswith("CONSOLE_AUTH_USERS="), (
            f"the generated env assigns CONSOLE_AUTH_USERS directly ({stripped!r}); the "
            f"password hashes are a credential and belong in Secret Manager")


def test_an_absent_console_service_is_a_supported_arrangement():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "CLOUDRUN_CONSOLE_SERVICE=${CLOUDRUN_CONSOLE_SERVICE:-" in text, (
        "the console service name must be overridable and may be empty - a deployment that "
        "serves no console skips the stage, exactly as an API-less one does")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_config_declaration.py -v`
Expected: FAIL — the first test lists all thirteen names.

- [ ] **Step 3: Add the console block to the generated env**

In `bootstrap-env.sh`, in the generated-env heredoc, after the existing
`CLOUDRUN_API_SERVICE=${CLOUDRUN_API_SERVICE:-}` line:

```bash
# THE CONSOLE TIER. Empty CLOUDRUN_CONSOLE_SERVICE means this deployment serves no browser console
# and that stage is skipped - the same arrangement an API-less deployment uses above.
CLOUDRUN_CONSOLE_SERVICE=${CLOUDRUN_CONSOLE_SERVICE:-}
CONSOLE_SERVICE_ACCOUNT=${CONSOLE_SERVICE_ACCOUNT:-${DEPLOY_ID}-console}
# Sizing. The console renders pages and proxies; it runs no model call and holds no mesh, so it is
# deliberately the smallest tier here.
CONSOLE_CPU=${CONSOLE_CPU:-1}
CONSOLE_MEMORY=${CONSOLE_MEMORY:-512Mi}
CONSOLE_CONCURRENCY=${CONSOLE_CONCURRENCY:-80}
CONSOLE_MIN_INSTANCES=${CONSOLE_MIN_INSTANCES:-0}
CONSOLE_MAX_INSTANCES=${CONSOLE_MAX_INSTANCES:-3}
# THE CONSOLE IS THE PUBLIC FRONT DOOR and is invokable by anyone by design: its own Auth.js
# session is the gate, not Cloud Run's IAM. This is a stated choice, not an inherited default.
CONSOLE_INGRESS=${CONSOLE_INGRESS:-all}
CONSOLE_ALLOW_UNAUTHENTICATED=${CONSOLE_ALLOW_UNAUTHENTICATED:-1}
# NO SECRET VALUES: container names only, in the same <SETTING>_SECRET spelling every other
# credential here uses. CONSOLE_AUTH_USERS holds scrypt password hashes, which is a credential.
AUTH_SECRET_SECRET=${AUTH_SECRET_SECRET:-console-auth-secret}
CONSOLE_AUTH_USERS_SECRET=${CONSOLE_AUTH_USERS_SECRET:-console-auth-users}
# Where the console reaches the product API. The server-side value is used by the authenticated
# /api/v1 proxy; the NEXT_PUBLIC_ one is compiled into the browser bundle and is what the
# WebSocket dials, because the stream goes browser->API directly and not through the proxy.
HEXERA_API_BASE_URL=${HEXERA_API_BASE_URL:-}
NEXT_PUBLIC_HEXERA_API_BASE_URL=${NEXT_PUBLIC_HEXERA_API_BASE_URL:-${HEXERA_API_BASE_URL:-}}
```

- [ ] **Step 4: Default the service name during discovery**

In the naming section near `MESH_JOB="${CLOUDRUN_MESH_JOB:-${DEPLOY_ID}-mesh}"`, add:

```bash
  CONSOLE_SERVICE="${CLOUDRUN_CONSOLE_SERVICE:-}"
```

Left empty rather than defaulted to a name: a deployment gets a console because it asked for one,
not because this script invented a service for it.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_config_declaration.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Verify the secret gate still passes**

Run: `python devtools/quality/check_deploy_secrets.py`
Expected: exit 0, no findings.

- [ ] **Step 7: Commit**

```bash
git add deploy/gcp/scripts/bootstrap-env.sh tests/unit/deploy/test_console_config_declaration.py
git commit -m "deploy: declare the console tier in the generated environment"
```

---

### Task 6: Validate the console configuration

**Files:**
- Modify: `deploy/gcp/scripts/validate-config.sh` (after the API block at line 84-94)
- Test: `tests/unit/deploy/test_console_config_validation.py`

**Interfaces:**
- Consumes: the variables Task 5 declares.
- Produces: a typed refusal before any cloud mutation when the console is half-configured.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify a half-declared console is refused before anything is created.
# Boundaries: read-only validation; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"

_BASE = {
    "DEPLOYMENT_ID": "t", "GCP_PROJECT_ID": "fake-proj", "GCP_REGION": "europe-west1",
}


def _run(env_extra: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in {**_BASE, **env_extra}.items()) + "\n",
        encoding="utf-8")
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True,
                          env={**os.environ, "DEPLOY_ENV_FILE": str(env_file)})


def test_a_console_without_an_api_base_url_is_refused(tmp_path):
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console"}, tmp_path)
    assert done.returncode != 0
    assert "HEXERA_API_BASE_URL" in done.stdout + done.stderr


def test_a_console_without_an_auth_secret_container_is_refused(tmp_path):
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console",
                 "HEXERA_API_BASE_URL": "https://api.example",
                 "AUTH_SECRET_SECRET": ""}, tmp_path)
    assert done.returncode != 0
    assert "AUTH_SECRET_SECRET" in done.stdout + done.stderr


def test_inverted_console_scaling_is_refused(tmp_path):
    done = _run({"CLOUDRUN_CONSOLE_SERVICE": "t-console",
                 "HEXERA_API_BASE_URL": "https://api.example",
                 "AUTH_SECRET_SECRET": "console-auth-secret",
                 "CONSOLE_AUTH_USERS_SECRET": "console-auth-users",
                 "CONSOLE_MIN_INSTANCES": "4", "CONSOLE_MAX_INSTANCES": "2"}, tmp_path)
    assert done.returncode != 0
    assert "CONSOLE_MIN_INSTANCES" in done.stdout + done.stderr


def test_no_console_is_not_an_error(tmp_path):
    done = _run({}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_config_validation.py -v`
Expected: FAIL — the three refusal tests, because nothing validates the console yet.

- [ ] **Step 3: Add the console block**

In `validate-config.sh`, immediately after the API-service block:

```bash
# THE CONSOLE. Empty CLOUDRUN_CONSOLE_SERVICE means this deployment serves no browser console and
# the stage is skipped, so nothing below applies. A console that IS declared must be able to reach
# the API and to resolve its two credentials, because both failures present only at runtime: an
# unreachable API is a console that renders and then 503s, and a missing AUTH_SECRET is a revision
# that never becomes ready.
if [ -n "${CLOUDRUN_CONSOLE_SERVICE:-}" ]; then
  [ -n "${HEXERA_API_BASE_URL:-}" ] \
    || add "CLOUDRUN_CONSOLE_SERVICE is set but HEXERA_API_BASE_URL is not - the console's /api/v1 proxy would have no origin to forward to"
  [ -n "${AUTH_SECRET_SECRET:-}" ] \
    || add "the console is configured but AUTH_SECRET_SECRET names no Secret Manager container - Auth.js refuses to start without a secret"
  [ -n "${CONSOLE_AUTH_USERS_SECRET:-}" ] \
    || add "the console is configured but CONSOLE_AUTH_USERS_SECRET names no Secret Manager container - nobody could sign in"
  if [[ "${CONSOLE_MIN_INSTANCES:-}" =~ ^[0-9]+$ ]] && [[ "${CONSOLE_MAX_INSTANCES:-}" =~ ^[0-9]+$ ]]; then
    [ "${CONSOLE_MIN_INSTANCES}" -le "${CONSOLE_MAX_INSTANCES}" ] \
      || add "CONSOLE_MIN_INSTANCES (${CONSOLE_MIN_INSTANCES}) exceeds CONSOLE_MAX_INSTANCES (${CONSOLE_MAX_INSTANCES})"
  fi
fi
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_config_validation.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add deploy/gcp/scripts/validate-config.sh tests/unit/deploy/test_console_config_validation.py
git commit -m "deploy: refuse a half-declared console before any cloud mutation"
```

---

### Task 7: The console's secret containers

**Files:**
- Modify: `deploy/gcp/scripts/create-secrets.sh:43-48` and the `SECRETS` array at `:81-88`
- Test: `tests/unit/deploy/test_console_secret_containers.py`

**Interfaces:**
- Consumes: `AUTH_SECRET_SECRET`, `CONSOLE_AUTH_USERS_SECRET`, `CONSOLE_SERVICE_ACCOUNT` from
  Task 5.
- Produces: two Secret Manager containers with the console service account bound as accessor.
  Task 8 references them.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the console's two credentials get containers and a reader, and no value.
# Boundaries: it reads the script's declarations; it creates nothing.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
SECRETS = REPO / "deploy" / "gcp" / "scripts" / "create-secrets.sh"


def test_the_console_credentials_have_containers():
    text = SECRETS.read_text(encoding="utf-8")
    assert "AUTH_SECRET|" in text, "no container is declared for AUTH_SECRET"
    assert "CONSOLE_AUTH_USERS|" in text, "no container is declared for CONSOLE_AUTH_USERS"


def test_the_console_identity_reads_them():
    text = SECRETS.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith(('"AUTH_SECRET|', '"CONSOLE_AUTH_USERS|')):
            assert "CONSOLE_SERVICE_ACCOUNT" in line, (
                f"the console identity is not a declared reader of this container: {line.strip()!r}")


def test_no_console_credential_value_appears():
    text = SECRETS.read_text(encoding="utf-8")
    assert "scrypt:" not in text, "a password hash literal leaked into the secrets script"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_secret_containers.py -v`
Expected: FAIL — no `AUTH_SECRET|` entry exists.

- [ ] **Step 3: Name the containers**

In `create-secrets.sh`, beside the existing container-name variables:

```bash
AUTH_SECRET_NAME="${AUTH_SECRET_SECRET:-console-auth-secret}"
CONSOLE_AUTH_USERS_NAME="${CONSOLE_AUTH_USERS_SECRET:-console-auth-users}"
```

- [ ] **Step 4: Declare them**

Append to the `SECRETS` array:

```bash
  "AUTH_SECRET|${AUTH_SECRET_NAME}|optional|CONSOLE_SERVICE_ACCOUNT|the key Auth.js signs console session cookies with - generated elsewhere, never here"
  "CONSOLE_AUTH_USERS|${CONSOLE_AUTH_USERS_NAME}|optional|CONSOLE_SERVICE_ACCOUNT|the console's email/password users and their scrypt hashes - replaced by database accounts in sub-project B"
```

Both are `optional` because a deployment that runs no console has neither, and an absent container
is a stated skip rather than a failure — the same treatment `MESH_API_KEY` gets.

- [ ] **Step 5: Grant the console identity on the shared credentials it also needs**

The console's `/api/v1` proxy signs `X-User-Id` and presents `X-API-Key`, so it reads two
containers the API already uses. Add `CONSOLE_SERVICE_ACCOUNT` to the reader list of the existing
`MESH_API_KEY` and `USER_TOKEN_SECRET` entries — change their reader field from
`API_SERVICE_ACCOUNT` to `API_SERVICE_ACCOUNT CONSOLE_SERVICE_ACCOUNT`.

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_secret_containers.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add deploy/gcp/scripts/create-secrets.sh tests/unit/deploy/test_console_secret_containers.py
git commit -m "deploy: create the console's secret containers and bind its reader"
```

---

### Task 8: The console service

The largest task. It follows `create-api-service.sh` deliberately — same skip shape, same
digest refusal, same reference-only credentials, same declarative-env drift refusal.

**Files:**
- Create: `deploy/gcp/scripts/create-console-service.sh`
- Test: `tests/unit/deploy/test_console_service_stage.py`

**Interfaces:**
- Consumes: `CONSOLE_IMAGE` (Task 4), the config from Task 5, the secret containers from Task 7.
- Produces: a Cloud Run service named by `CLOUDRUN_CONSOLE_SERVICE`, and
  `CONSOLE_SERVICE_DISPOSITION` (`created` | `reused` | `rolled`) exported for Task 10.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the console rollout is digest-pinned, credential-free in its spec, and skippable.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-console-service.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "secrets describe"; then exit 0; fi
if has "run services describe"; then exit 1; fi
if has "run services get-iam-policy"; then exit 0; fi
exit 0
"""

_CONSOLE_DIGEST = "us-central1-docker.pkg.dev/fake-proj/hexera/console@sha256:c0ffee"

_ENV = {
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_REGION": "europe-west1",
    "CLOUDRUN_CONSOLE_SERVICE": "t-console",
    "CONSOLE_SERVICE_ACCOUNT": "t-console",
    "CONSOLE_IMAGE": _CONSOLE_DIGEST,
    "HEXERA_API_BASE_URL": "https://api.example",
    "NEXT_PUBLIC_HEXERA_API_BASE_URL": "https://api.example",
    "AUTH_SECRET_SECRET": "console-auth-secret",
    "CONSOLE_AUTH_USERS_SECRET": "console-auth-users",
    "MESH_API_KEY_SECRET": "mesh-api-key",
    "USER_TOKEN_SECRET_SECRET": "user-token-secret",
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    def _run(over: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
        env_vals = {**_ENV, **(over or {})}
        env_file = tmp_path / "generated.env"
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in env_vals.items() if v != "") + "\n",
            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ,
                 "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state),
                 "DEPLOY_ENV_FILE": str(env_file)})
        log_path = state / "calls.log"
        return done, (log_path.read_text(encoding="utf-8") if log_path.exists() else "")

    return _run


def test_no_console_service_is_a_stated_skip(run):
    done, calls = run({"CLOUDRUN_CONSOLE_SERVICE": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "run deploy" not in calls, "it mutated the cloud for a deployment with no console"


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"CONSOLE_IMAGE": "us-central1-docker.pkg.dev/p/r/console:v1"})
    assert done.returncode != 0
    assert "run deploy" not in calls


def test_the_rollout_is_digest_pinned(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert f"--image {_CONSOLE_DIGEST}" in calls


def test_credentials_reach_the_spec_only_as_references(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--set-secrets" in calls
    assert "AUTH_SECRET=console-auth-secret:latest" in calls
    assert "CONSOLE_AUTH_USERS=console-auth-users:latest" in calls
    # The env list is declarative and must never carry the values themselves.
    for forbidden in ("AUTH_SECRET=", "CONSOLE_AUTH_USERS="):
        for chunk in calls.split("--set-env-vars")[1:]:
            assert forbidden not in chunk.split("--set-secrets")[0], (
                f"{forbidden!r} appears in the declarative environment, not as a reference")


def test_the_api_origins_reach_the_container(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "HEXERA_API_BASE_URL=https://api.example" in calls
    assert "NEXT_PUBLIC_HEXERA_API_BASE_URL=https://api.example" in calls


def test_the_console_is_publicly_invokable_when_stated(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "add-iam-policy-binding t-console" in calls
    assert "allUsers" in calls


def test_the_public_binding_is_removed_when_not_stated(run):
    done, calls = run({"CONSOLE_ALLOW_UNAUTHENTICATED": "0"})
    assert done.returncode == 0, done.stderr
    assert "add-iam-policy-binding" not in calls or "allUsers" not in calls.split(
        "add-iam-policy-binding")[-1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_service_stage.py -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the script**

Create `deploy/gcp/scripts/create-console-service.sh`:

```bash
#!/usr/bin/env bash
# Responsibility: Provision the Cloud Run console service - the browser front door of a deployment.
# Owns: the console's identity, its scaling, and whether the public may reach it.
# Boundaries: every credential is a REFERENCE to Secret Manager; the image is promoted, never built.

# Provision the CLOUD RUN CONSOLE SERVICE. Idempotent and RECONCILING: an existing service is
# updated in place - a new revision on the promoted digest - and one already running that digest is
# reported as reused.
#
#   bash deploy/gcp/scripts/create-console-service.sh
#
# WHY IT IS PUBLICLY INVOKABLE. Unlike the API, this service IS the front door: its own Auth.js
# session is the gate, and putting Cloud Run IAM in front of it would mean nobody could reach the
# sign-in page to authenticate at all. That is why CONSOLE_ALLOW_UNAUTHENTICATED defaults to 1
# here and API_ALLOW_UNAUTHENTICATED defaults to 0 there - two different questions, answered
# separately rather than one flag applied to both.
#
# WHY NO VPC EGRESS. The console reaches the product API over its public HTTPS origin, the same
# one the browser's WebSocket dials. It opens no database and no Memorystore, so putting it on the
# VPC would buy nothing and cost a subnet attachment.
#
# INPUTS   the deployment env (CONSOLE_IMAGE, CLOUDRUN_CONSOLE_SERVICE, CONSOLE_*,
#          HEXERA_API_BASE_URL, the *_SECRET container names)
# MUTATES  the console runtime identity, one accessor binding per declared secret, the Cloud Run
#          service, and its invoker policy. It deletes nothing.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

CONSOLE_SERVICE="${CLOUDRUN_CONSOLE_SERVICE:-}"
CONSOLE_SA="${CONSOLE_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-console}"
CONSOLE_SA_EMAIL="${CONSOLE_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# A deployment may genuinely serve no browser console - a mesh-only or API-only arrangement does.
# That is a skip, and it is stated, exactly as the API tier states its own.
if [ -z "${CONSOLE_SERVICE}" ]; then
  info "No Cloud Run console service configured - skipping the console tier"
  log "set CLOUDRUN_CONSOLE_SERVICE to deploy the browser console from this deployment"
  exit 0
fi

# The image is the CONSOLE image, BY DIGEST. promote-release.sh wrote it from the validated
# release record; a tag is refused rather than re-resolved, because a tag can be moved between the
# validation that approved the bytes and the rollout that ships them.
require_digest_reference CONSOLE_IMAGE "${CONSOLE_IMAGE:-}"

CONSOLE_CPU="${CONSOLE_CPU:-1}"
CONSOLE_MEMORY="${CONSOLE_MEMORY:-512Mi}"
CONSOLE_CONCURRENCY="${CONSOLE_CONCURRENCY:-80}"
CONSOLE_TIMEOUT_SECONDS="${CONSOLE_TIMEOUT_SECONDS:-300}"
CONSOLE_MIN_INSTANCES="${CONSOLE_MIN_INSTANCES:-0}"
CONSOLE_MAX_INSTANCES="${CONSOLE_MAX_INSTANCES:-3}"
CONSOLE_INGRESS="${CONSOLE_INGRESS:-all}"
CONSOLE_ALLOW_UNAUTHENTICATED="${CONSOLE_ALLOW_UNAUTHENTICATED:-1}"
APP_ENV="${APP_ENV:-dev}"
CONSOLE_DISPOSITION=created

info "Console service ${CONSOLE_SERVICE} in ${GCP_REGION} (${GCP_PROJECT_ID})"
log "image (validated digest): ${CONSOLE_IMAGE}"

# 1) the console runtime identity - a named account, not the default compute service account.
if sa_exists "${CONSOLE_SA_EMAIL}"; then
  log "console identity ${CONSOLE_SA_EMAIL} (exists)"
else
  info "Creating console runtime identity ${CONSOLE_SA_EMAIL}"
  gc iam service-accounts create "${CONSOLE_SA}" \
    --display-name "Hexera console service" \
    || die "could not create ${CONSOLE_SA_EMAIL}. Creating identities needs
   iam.serviceAccountAdmin, which a DEPLOY identity is deliberately not given. Create it once, as
   an owner:
     gcloud iam service-accounts create ${CONSOLE_SA} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera console service'"
fi

# 2) THE CREDENTIALS, as references. `RUNTIME_VAR:ENV_VAR_HOLDING_THE_SECRET_NAME` - the same shape
#    the API tier and the worker startup script read theirs from. An unset name is skipped, never
#    bound to an empty secret.
SECRET_BINDINGS=()
SECRET_NAMES=()
DECLARED_ENV_NAMES=()
for pair in "AUTH_SECRET:AUTH_SECRET_SECRET" \
            "CONSOLE_AUTH_USERS:CONSOLE_AUTH_USERS_SECRET" \
            "MESH_API_KEY:MESH_API_KEY_SECRET" \
            "USER_TOKEN_SECRET:USER_TOKEN_SECRET_SECRET"; do
  runtime_var="${pair%%:*}"
  holder="${pair##*:}"
  secret_name="${!holder:-}"
  [ -n "${secret_name}" ] || continue
  SECRET_BINDINGS+=("${runtime_var}=${secret_name}:latest")
  SECRET_NAMES+=("${secret_name}")
  DECLARED_ENV_NAMES+=("${runtime_var}")
done

for secret_name in ${SECRET_NAMES[@]+"${SECRET_NAMES[@]}"}; do
  secret_exists "${secret_name}" \
    || warn "cannot confirm secret '${secret_name}' exists in ${GCP_PROJECT_ID} - it may be absent,
       or this identity may not be allowed to read Secret Manager. If it is absent:
         gcloud secrets create ${secret_name} --project ${GCP_PROJECT_ID} --replication-policy=automatic"
  if gc secrets add-iam-policy-binding "${secret_name}" \
       --member "serviceAccount:${CONSOLE_SA_EMAIL}" \
       --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then
    log "secret/${secret_name} += roles/secretmanager.secretAccessor -> ${CONSOLE_SA_EMAIL}"
  else
    warn "could not set IAM on secret ${secret_name}. If the binding is already in place the
       rollout below still succeeds; if it is not, the revision never becomes ready and this is
       the command:
         gcloud secrets add-iam-policy-binding ${secret_name} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${CONSOLE_SA_EMAIL} --role roles/secretmanager.secretAccessor"
  fi
done

# 3) THE NON-SECRET SETTINGS.
#    NEXT_PUBLIC_HEXERA_API_BASE_URL is compiled into the browser bundle at BUILD time by Next, so
#    setting it here changes nothing the browser already downloaded. It is stated anyway because a
#    server component may read it, and because a spec that does not name the API this console
#    talks to is not reviewable. When the two origins must differ per environment, the value has
#    to be a build argument to the image - recorded in the deployment doc as a known limit.
CONSOLE_ENV_PAIRS=(
  "ENV=${APP_ENV}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "NODE_ENV=production"
  "HEXERA_API_BASE_URL=${HEXERA_API_BASE_URL}"
  "NEXT_PUBLIC_HEXERA_API_BASE_URL=${NEXT_PUBLIC_HEXERA_API_BASE_URL:-${HEXERA_API_BASE_URL}}"
)
for pair in "${CONSOLE_ENV_PAIRS[@]}"; do
  case "${pair}" in
    *"|"*) die "the setting '${pair%%=*}' has a '|' in its value, which is the delimiter this
   environment list is passed with. Give it a value without one." ;;
  esac
  DECLARED_ENV_NAMES+=("${pair%%=*}")
done

# 4) WHAT IS ALREADY THERE. --set-env-vars is DECLARATIVE: it replaces the container's environment
#    with exactly what this deployment states. That is what stops drift, and it is also what would
#    silently delete a setting that only ever existed on the live service. The difference is
#    computed, named, and refused unless an operator says to prune it.
if run_svc_exists "${CONSOLE_SERVICE}"; then
  live_image="$(gc run services describe "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  if [ "${live_image}" = "${CONSOLE_IMAGE}" ]; then
    CONSOLE_DISPOSITION=reused
  else
    CONSOLE_DISPOSITION=rolled
  fi

  live_env_names="$(gc run services describe "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].env[].name)' 2>/dev/null | tr ';' '\n' || true)"
  undeclared=()
  while read -r live_name; do
    [ -n "${live_name}" ] || continue
    declared=0
    for declared_name in "${DECLARED_ENV_NAMES[@]}"; do
      [ "${live_name}" != "${declared_name}" ] || { declared=1; break; }
    done
    [ "${declared}" = "1" ] || undeclared+=("${live_name}")
  done <<<"${live_env_names}"

  if [ ${#undeclared[@]} -gt 0 ]; then
    if [ "${CONSOLE_ENV_PRUNE:-0}" = "1" ]; then
      warn "CONSOLE_ENV_PRUNE=1 - dropping ${#undeclared[@]} setting(s) the live service carries and
       this deployment does not declare: ${undeclared[*]}"
    else
      warn "the live ${CONSOLE_SERVICE} carries ${#undeclared[@]} setting(s) this deployment does not declare:"
      printf '    %s\n' "${undeclared[*]}" >&2
      die "refusing to silently drop them. The service's environment is declarative here, so a
   setting that exists only on the live service disappears on the next revision. Either state them
   in this script, or run again with CONSOLE_ENV_PRUNE=1 to say that dropping them is the intent.
   Only NAMES are printed above: this refusal must not republish a value."
    fi
  fi
fi

# 5) the rollout. A new REVISION on an existing service - its URL, IAM policy and revision history
#    all survive.
deploy_args=(
  --region "${GCP_REGION}"
  --image "${CONSOLE_IMAGE}"
  --service-account "${CONSOLE_SA_EMAIL}"
  --port 8080
  --cpu "${CONSOLE_CPU}"
  --memory "${CONSOLE_MEMORY}"
  --concurrency "${CONSOLE_CONCURRENCY}"
  --timeout "${CONSOLE_TIMEOUT_SECONDS}"
  --min-instances "${CONSOLE_MIN_INSTANCES}"
  --max-instances "${CONSOLE_MAX_INSTANCES}"
  --cpu-boost
  --execution-environment gen2
  --ingress "${CONSOLE_INGRESS}"
  --labels "app=hexera,component=console,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${CONSOLE_ENV_PAIRS[*]}")"
)
if [ ${#SECRET_BINDINGS[@]} -gt 0 ]; then
  deploy_args+=(--set-secrets "$(IFS=','; printf '%s' "${SECRET_BINDINGS[*]}")")
else
  warn "this deployment declares no secret container names for the console, so its existing
       secret references are left exactly as they are."
fi
if [ "${CONSOLE_DISPOSITION}" = "created" ]; then
  # Only on CREATE, so an update cannot momentarily revoke a public binding a live service holds.
  deploy_args+=(--no-allow-unauthenticated)
fi

info "Deploying ${CONSOLE_SERVICE} (${CONSOLE_DISPOSITION}: ${#CONSOLE_ENV_PAIRS[@]} env vars, ${#SECRET_BINDINGS[@]} secret reference(s))"
gc run deploy "${CONSOLE_SERVICE}" "${deploy_args[@]}"

# 6) WHO MAY INVOKE IT. Public by default and by design - see the header. Stated by the
#    deployment, never inherited from whatever the service happened to have.
POLICY_MEMBERS="$(gc run services get-iam-policy "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
  --format='value(bindings.members)' 2>/dev/null || true)"
if [ "${CONSOLE_ALLOW_UNAUTHENTICATED}" = "1" ]; then
  gc run services add-iam-policy-binding "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
    --member allUsers --role roles/run.invoker >/dev/null
  INVOKER_STATE="PUBLIC - allUsers may reach the sign-in page; Auth.js is the gate behind it"
else
  case "${POLICY_MEMBERS}" in
    *allUsers*)
      info "Removing the public invoker binding (CONSOLE_ALLOW_UNAUTHENTICATED is not 1)"
      gc run services remove-iam-policy-binding "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
        --member allUsers --role roles/run.invoker >/dev/null
      INVOKER_STATE="private (the public invoker binding it had was removed)" ;;
    *)
      INVOKER_STATE="private - callers must present an identity holding roles/run.invoker" ;;
  esac
fi

CONSOLE_URL="$(gc run services describe "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"
# Read by write-deployment-state.sh, which distinguishes reconciled from not-selected.
export CONSOLE_SERVICE_DISPOSITION="${CONSOLE_DISPOSITION}"
log "console service ${CONSOLE_SERVICE}  (${CONSOLE_DISPOSITION})"
log "  identity      ${CONSOLE_SA_EMAIL}"
log "  image         ${CONSOLE_IMAGE}"
log "  scaling       ${CONSOLE_MIN_INSTANCES}..${CONSOLE_MAX_INSTANCES} instances, concurrency ${CONSOLE_CONCURRENCY}"
log "  api origin    ${HEXERA_API_BASE_URL}"
log "  credentials   ${#SECRET_BINDINGS[@]} Secret Manager reference(s) - no value is in the spec"
log "  invoker       ${INVOKER_STATE}"
log "  url           ${CONSOLE_URL:-<not reported>}"
log "done"
```

- [ ] **Step 4: Make it executable**

```bash
chmod +x deploy/gcp/scripts/create-console-service.sh
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_service_stage.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Lint the shell**

Run: `shellcheck deploy/gcp/scripts/create-console-service.sh`
Expected: clean, or only the same directives the sibling scripts already carry.

- [ ] **Step 7: Verify the secret gate**

Run: `python devtools/quality/check_deploy_secrets.py`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add deploy/gcp/scripts/create-console-service.sh tests/unit/deploy/test_console_service_stage.py
git commit -m "deploy: provision the Cloud Run console service"
```

---

### Task 9: Select and run the console stage

**Files:**
- Modify: `deploy/gcp/scripts/deploy.sh` (`_known` at line 68; a new stage after the API stage at line 245-253; the stage total near line 33)
- Test: `tests/unit/deploy/test_console_component_selection.py`

**Interfaces:**
- Consumes: `create-console-service.sh` from Task 8.
- Produces: `DEPLOY_COMPONENTS=console` selects the console stage and nothing else.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify 'console' is a selectable deploy component and its stage states its skip.
# Boundaries: it reads the driver; it runs no deploy.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"


def test_console_is_a_known_component():
    text = DEPLOY.read_text(encoding="utf-8")
    known = [line for line in text.splitlines() if line.strip().startswith("_known=")]
    assert known, "the component roster moved; this test cannot find it"
    assert "console" in known[0], (
        "'console' is not a known component, so DEPLOY_COMPONENTS=console would be refused "
        "as a typo")


def test_the_console_stage_runs_the_console_script():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "create-console-service.sh" in text


def test_an_unselected_console_states_its_skip():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "skipped console" in text, (
        "a stage that is not selected must say so; silence reads as success")


def test_the_console_rolls_out_after_the_api():
    text = DEPLOY.read_text(encoding="utf-8")
    api_at = text.index("create-api-service.sh")
    console_at = text.index("create-console-service.sh")
    assert api_at < console_at, (
        "the console must roll out after the API it talks to, or its first requests 503")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_component_selection.py -v`
Expected: FAIL — `console` is not in `_known`.

- [ ] **Step 3: Add the component**

In `deploy.sh:68`:

```bash
  _known="images data storage migrate queue workers console"
```

- [ ] **Step 4: Add the stage**

Immediately after the API-service stage block and before the worker-fleet stage:

```bash
stage "Console service (the promoted console digest, in front of the API)"
# AFTER the API: the console's every page load reaches it, so a console that rolls out first
# serves errors until the API catches up. It is its own component rather than part of `images`
# because iterating on the console is exactly the case that wants to deploy it alone.
if want console; then
  bash "${S}/create-console-service.sh"
else
  skipped console "the console service keeps serving whichever digest it already has"
fi
```

- [ ] **Step 5: Correct the stage total**

`deploy.sh:33`'s `stage()` prints `[n/STAGE_TOTAL]`. Find the `STAGE_TOTAL=` assignment and
increment it by one. A counter that says `[12/12]` while a thirteenth stage still runs is a
progress display nobody can trust.

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_component_selection.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Verify the whole deploy suite**

Run: `python -m pytest tests/unit/deploy -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add deploy/gcp/scripts/deploy.sh tests/unit/deploy/test_console_component_selection.py
git commit -m "deploy: make console a selectable component with its own stage"
```

---

### Task 10: Record the console in the deployment state

**Files:**
- Modify: `deploy/gcp/scripts/write-deployment-state.sh` (digest read-back ~line 39-50; dispositions ~line 88; resources ~line 165)
- Test: `tests/unit/deploy/test_console_deployment_state.py`

**Interfaces:**
- Consumes: `CONSOLE_SERVICE_DISPOSITION` exported by Task 8.
- Produces: `deployment.json` carrying `images.console` and `resources.console_service`.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the state manifest names the console service and its digest.
# Boundaries: it reads the writer's declarations; it calls no cloud.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
WRITER = REPO / "deploy" / "gcp" / "scripts" / "write-deployment-state.sh"


def test_the_console_service_is_a_recorded_resource():
    text = WRITER.read_text(encoding="utf-8")
    assert '"console_service"' in text, (
        "the manifest records no console_service, so mesh-destroy.sh could not tell a "
        "tooling-created console from one somebody made by hand")


def test_the_console_digest_is_read_back_for_an_unselected_run():
    text = WRITER.read_text(encoding="utf-8")
    assert "CONSOLE_DIGEST" in text, (
        "a run that did not select 'console' must still record the digest the live service "
        "carries, not an empty value")


def test_the_console_disposition_is_component_aware():
    text = WRITER.read_text(encoding="utf-8")
    assert "_disp console" in text, (
        "the console's disposition must distinguish reconciled from not selected, like every "
        "other resource here")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_deployment_state.py -v`
Expected: FAIL — all three.

- [ ] **Step 3: Read the digest back**

The existing block is gated on `_selected images`. The console is its own component, so it needs
its own gate — a run that selected `images` but not `console` must still record what the live
console is actually serving. Insert after that block:

```bash
# THE CONSOLE'S DIGEST, on the console's own selection. A run that reconciled the API but not the
# console must record what the console is actually running, not the digest this commit validated.
if _selected console; then
  CONSOLE_DIGEST="$(resolve_digest "${CONSOLE_IMAGE:-}" 2>/dev/null || printf '%s' "${CONSOLE_IMAGE:-}")"
elif [ -n "${CLOUDRUN_CONSOLE_SERVICE:-}" ]; then
  CONSOLE_DIGEST="$(gc run services describe "${CLOUDRUN_CONSOLE_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
else
  CONSOLE_DIGEST=""
fi
```

- [ ] **Step 4: Record the disposition and the resource**

Beside the existing `API_DISP` line:

```bash
CONSOLE_DISP="$(_disp console "${CONSOLE_SERVICE_DISPOSITION:-}" console_service)"
```

Beside the `_recon` assignments:

```bash
CONSOLE_RECON="$(_recon console)"
```

In the Python heredoc, extend the images map — note it is `"images": {...}` on one line today:

```python
  "images": {"mesh": "${MESH_DIGEST}", "app": "${APP_DIGEST}", "console": "${CONSOLE_DIGEST}"},
```

And add the resource, beside the `api_service` entry (which lives in the conditional block near
the bottom of the heredoc, not in the top `resources` literal):

```python
doc["resources"]["console_service"] = {
    "name": "${CLOUDRUN_CONSOLE_SERVICE:-}",
    "service_account": "${CONSOLE_SERVICE_ACCOUNT:-}",
    "disposition": "${CONSOLE_DISP}",
    "reconciled": ${CONSOLE_RECON},
}
```

This is a Python heredoc inside bash: `${...}` is expanded by the shell before Python sees it, so
match the surrounding quoting exactly. A mismatched quote here fails at deploy time, not review
time.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_deployment_state.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/scripts/write-deployment-state.sh tests/unit/deploy/test_console_deployment_state.py
git commit -m "deploy: record the console service in the deployment-state manifest"
```

---

### Task 11: Deploy the console on merge

**Files:**
- Modify: `.github/workflows/deploy.yml` (the component picker, ~line 95-160)
- Test: `tests/unit/deploy/test_console_component_selection.py` (extend)

**Interfaces:**
- Consumes: the `console` component from Task 9.
- Produces: a merge to `main` reconciles `images,migrate,console`; a release tag still reconciles
  `all`; a manual run still chooses.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/deploy/test_console_component_selection.py`:

```python
DEPLOY_WF = REPO / ".github" / "workflows" / "deploy.yml"


def test_a_merge_reconciles_the_console():
    text = DEPLOY_WF.read_text(encoding="utf-8")
    assert "components=images,migrate,console" in text, (
        "a merge to main deploys the app but not the console, so the console would silently "
        "stay on an older digest after every merge")


def test_a_release_tag_still_reconciles_everything():
    text = DEPLOY_WF.read_text(encoding="utf-8")
    assert "components=all" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_console_component_selection.py -k merge -v`
Expected: FAIL — the merge default is `images,migrate`.

- [ ] **Step 3: Add console to the merge default**

In `.github/workflows/deploy.yml`, find the `echo "components=images,migrate"` in the picker step
and change it to `echo "components=images,migrate,console"`. Update the surrounding comment to
name the three tiers a merge reconciles.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_console_component_selection.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/deploy.yml tests/unit/deploy/test_console_component_selection.py
git commit -m "deploy: reconcile the console on every merge to main"
```

---

### Task 12: Documentation and the local env example

**Files:**
- Modify: `docs/deployment/environments-and-delivery.md`
- Modify: `apps/console/.env.example`

**Interfaces:**
- Consumes: everything above.
- Produces: no code interface.

- [ ] **Step 1: Document the console tier**

In `docs/deployment/environments-and-delivery.md`, add the console wherever the API service is
described, covering: the service name (`hexera-<env>-console`), that it is publicly invokable by
design while the API is not, its scaling (`0..3` on dev), its two own secrets and the two it
shares with the API, and the `console` component.

Record two limits honestly:
- `NEXT_PUBLIC_HEXERA_API_BASE_URL` is compiled into the browser bundle at image build time, so
  setting it on the service does not change what the browser downloaded. Per-environment public
  origins need it as a build argument.
- The console runs on the generated `run.app` URL. `trustHost: true` in
  `apps/console/src/auth.ts` is what makes that work without an `AUTH_URL`.

- [ ] **Step 2: Match the env example to what is deployed**

Replace `apps/console/.env.example` with:

```bash
# Local development values. In a deployment every credential below arrives as a Secret Manager
# REFERENCE set by deploy/gcp/scripts/create-console-service.sh - never as a literal.

# Console users as JSON, with scrypt password hashes.
# Generate a hash with: printf '%s' 'your-password' | pnpm --filter @hexera/console auth:hash
# Deployed from the container named by CONSOLE_AUTH_USERS_SECRET.
# Replaced by database accounts in sub-project B.
CONSOLE_AUTH_USERS=[{"email":"you@example.com","name":"You","passwordHash":"scrypt:..."}]

# The key Auth.js signs session cookies with. Generate with: openssl rand -base64 32
# Deployed from the container named by AUTH_SECRET_SECRET.
AUTH_SECRET=

# Public browser origin for WebSocket streams: the stream goes browser -> API directly and does
# not pass through the console's proxy, so this must be an origin users can reach.
# NOTE: Next compiles NEXT_PUBLIC_* into the browser bundle at BUILD time, so changing it on a
# deployed service changes nothing the browser already downloaded.
NEXT_PUBLIC_HEXERA_API_BASE_URL=http://localhost:8000

# Private server-side API origin used by the authenticated /api/v1 proxy.
HEXERA_API_BASE_URL=http://localhost:8000

# Presented and signed by the proxy when the API deployment requires them.
# Deployed from the containers named by MESH_API_KEY_SECRET and USER_TOKEN_SECRET_SECRET.
MESH_API_KEY=
USER_TOKEN_SECRET=
```

No `AUTH_URL` entry: `apps/console/src/auth.ts` sets `trustHost: true`, so Auth.js derives its URL
from the request host and works on the generated `run.app` URL unchanged.

- [ ] **Step 3: Verify no credential leaked into either document**

Run: `python devtools/quality/check_deploy_secrets.py && git diff --cached | grep -iE 'scrypt:|AUTH_SECRET=[^$]' || echo CLEAN`
Expected: `CLEAN`.

- [ ] **Step 4: Commit**

```bash
git add docs/deployment/environments-and-delivery.md apps/console/.env.example
git commit -m "docs: describe the console tier and its two known limits"
```

---

## Final verification

Not a task — run these before opening the PR.

- [ ] `python -m pytest tests/unit/deploy -q` — the whole deploy suite passes
- [ ] `make lint && python devtools/quality/mypy_ratchet.py` — Python gates pass
- [ ] `pnpm typecheck && pnpm lint && pnpm test && pnpm build` — workspace gates pass
- [ ] `python devtools/quality/check_deploy_secrets.py` — no credential in any spec or env
- [ ] `make release-validate` — Gate C builds and smokes three components, verdict `passed`
- [ ] Push the branch, then run the Deploy workflow manually against `dev` with
      `components=console`. Confirm the run reports the console stage as reconciled and every
      other stage as *not selected*.
- [ ] Against the dev console URL: unauthenticated `/` redirects to `/sign-in`; the sign-in page
      renders the email/password form; `/readyz` returns 200; signing in with a seeded user
      reaches the console shell.

## What this plan deliberately does not do

- Deploy `apps/admin-console`.
- Retire `ui/` or the `/ui` route — that is sub-project C.
- Add sign-in throttling — that is sub-project B, which replaces this credential path entirely.
- Give the console a custom domain.
