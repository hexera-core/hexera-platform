# Admin console behind IAP (Admin-1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy `apps/admin-console` as a Cloud Run service that only named Google identities can
reach, with a navigation shell the later admin sub-projects hang pages off.

**Architecture:** The admin console becomes a fourth release component beside `mesh`, `app` and
`console` — built once by Gate C, published by digest, promoted, and rolled out by a new `admin`
deploy component. Its service is the inverse of the public console's: no `allUsers` binding, IAP
enabled directly on the service, and the IAP service agent granted the invoker role.

**Tech Stack:** Next 16 standalone output on Node, pnpm workspaces, Docker multi-stage, Cloud Run,
Identity-Aware Proxy, GitHub Actions, bash + pytest for the deploy tooling.

**Spec:** `docs/superpowers/specs/2026-09-08-admin-console-design.md` (§4 is this plan's scope)

**Access runbook:** `docs/deployment/admin-console-access.md` — §6 is this plan's acceptance test.

## A deliberate departure from the spec

Spec §4 puts **VPC egress and a dedicated Postgres role** in Admin-1. This plan **defers both to
Admin-2**.

Admin-1 ships a navigation shell with no data pages, so it issues no queries. Giving it a database
credential and a route to the private data tier before anything reads from them means shipping an
unused credential and an unused network path — exactly what a reviewer should object to. Admin-2 is
the first sub-project that reads product tables, and it adds both alongside the first page that
needs them.

Everything else in §4 is implemented here.

## Global Constraints

- **Every image reference in a deployment is digest-qualified.** A tag-only reference is refused,
  never re-resolved. `require_digest_reference` in `deploy/gcp/scripts/lib.sh` is the check.
- **No credential value in a Cloud Run spec, a generated env file, or a log line.** Deployments name
  Secret Manager containers using the `<SETTING>_SECRET` convention.
  `devtools/quality/check_deploy_secrets.py` is the gate.
- **The admin service must never carry an `allUsers` invoker binding.** IAP and the Cloud Run IAM
  policy are separate gates; a public binding admits requests regardless of what IAP decides. This
  is the single most important constraint in this plan.
- **Gate C builds; nothing else does.** `deploy.sh` promotes an already-validated artifact.
- **New deploy stages state their skip**, using the existing `want` / `skipped` helpers.
- **Pinned base images.** Every `FROM` is digest-pinned. Reuse the Node digest the console stages
  already pin: `node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e`.
- **Existing action pins are reused verbatim:** `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`.
- **Product version authority** is `src/meshpipeline/__init__.py`'s `__version__`, passed as
  `--build-arg APP_VERSION`.
- **Scripts must run on bash 3.2** (what macOS ships), like their siblings.

---

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `apps/admin-console/src/app/_components/nav.tsx` | The sidebar: the five sections and which is current |
| `apps/admin-console/src/app/_components/nav.test.ts` | Its unit tests |
| `apps/admin-console/src/app/(admin)/layout.tsx` | The shell that wraps every admin page in the nav |
| `deploy/gcp/scripts/create-admin-service.sh` | Provision the IAP-protected Cloud Run service |
| `tests/unit/deploy/test_admin_service_stage.py` | Drive it against a fake gcloud |
| `tests/unit/deploy/test_admin_release_component.py` | Gate C builds, smokes and records `admin` |
| `tests/unit/deploy/test_admin_deploy_wiring.py` | Workflow outputs, env mapping, dispatch options |

**Modified**

| Path | Change |
|---|---|
| `apps/admin-console/next.config.ts` | `output: "standalone"` + `outputFileTracingRoot` |
| `apps/admin-console/package.json` | a `test` script, so the CI `web` lane runs its tests |
| `apps/admin-console/src/app/(admin)/page.tsx` | the stub becomes the Fleet landing page |
| `Dockerfile` | two new stages: `admin-build`, `admin` |
| `devtools/release/validate.sh` | build, smoke and record the `admin` component |
| `deploy/gcp/scripts/promote-release.sh` | resolve `admin`, write `ADMIN_IMAGE` |
| `deploy/gcp/scripts/bootstrap-env.sh` | declare the admin tier |
| `deploy/gcp/scripts/validate-config.sh` | validate it |
| `deploy/gcp/scripts/enable-apis.sh` | enable `iap.googleapis.com` |
| `deploy/gcp/scripts/deploy.sh` | `admin` in `_known`; a stage after the API |
| `deploy/gcp/scripts/write-deployment-state.sh` | record `admin_service` |
| `.github/workflows/deploy.yml` | `admin_service` output, env mapping, dispatch options, post-deploy IAP check |
| `docs/deployment/admin-console-access.md` | §9's prerequisite is satisfied — say so |
| `docs/deployment/environments-and-delivery.md` | document the admin tier |

---

### Task 1: The navigation shell

**Files:**
- Create: `apps/admin-console/src/app/_components/nav.tsx`
- Create: `apps/admin-console/src/app/_components/nav.test.ts`
- Create: `apps/admin-console/src/app/(admin)/layout.tsx`
- Modify: `apps/admin-console/src/app/(admin)/page.tsx`
- Modify: `apps/admin-console/package.json`

**Interfaces:**
- Consumes: nothing.
- Produces: `ADMIN_SECTIONS`, an ordered readonly array of
  `{ href: string; label: string; available: boolean }`, exported from
  `src/app/_components/nav.tsx`. Later admin sub-projects flip `available` and add pages at those
  hrefs.

- [ ] **Step 1: Add a test script so CI runs these tests**

The CI `web` lane runs `pnpm test`, which is `pnpm -r --if-present test`. `admin-console` has no
`test` script, so its tests would silently never run. In `apps/admin-console/package.json`, add to
`"scripts"` (matching `@hexera/console`'s exactly):

```json
    "test": "node --import tsx --test src/**/*.test.ts",
```

and add to `"devDependencies"`, matching the version `@hexera/console` already pins:

```json
    "tsx": "4.23.13",
```

Then run `pnpm install` from the repo root so the lockfile is updated.

- [ ] **Step 2: Write the failing test**

```typescript
import assert from "node:assert/strict";
import { test } from "node:test";

import { ADMIN_SECTIONS } from "./nav.ts";

test("the shell offers the five admin sections in a fixed order", () => {
  assert.deepEqual(
    ADMIN_SECTIONS.map((s) => s.label),
    ["Fleet", "Costs", "Activity", "Customers", "Outreach"],
  );
});

test("every section has a route under /", () => {
  for (const section of ADMIN_SECTIONS) {
    assert.match(section.href, /^\/[a-z-]*$/, `bad href for ${section.label}`);
  }
});

test("sections whose data does not exist yet are marked unavailable", () => {
  // Customers reads accounts and metering, which do not exist; Outreach lives only in prod.
  // Marking them unavailable is what stops the shell linking to a page that cannot render.
  const unavailable = ADMIN_SECTIONS.filter((s) => !s.available).map((s) => s.label);
  assert.deepEqual(unavailable, ["Customers", "Outreach"]);
});

test("hrefs are unique", () => {
  const hrefs = ADMIN_SECTIONS.map((s) => s.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pnpm --filter @hexera/admin-console test`
Expected: FAIL — cannot resolve `./nav.ts`.

- [ ] **Step 4: Write the nav**

Create `apps/admin-console/src/app/_components/nav.tsx`:

```tsx
// The admin console's five sections, in the order they are shown.
//
// `available` is the honest state of the DATA, not a feature flag: Customers reads accounts and
// token metering, which do not exist yet, and Outreach is a single prod-only instance. A section
// that is unavailable renders as text rather than a link, because linking to a page that cannot
// render is worse than saying why it is not there.
export type AdminSection = {
  href: string;
  label: string;
  available: boolean;
};

export const ADMIN_SECTIONS: readonly AdminSection[] = [
  { href: "/", label: "Fleet", available: true },
  { href: "/costs", label: "Costs", available: true },
  { href: "/activity", label: "Activity", available: true },
  { href: "/customers", label: "Customers", available: false },
  { href: "/outreach", label: "Outreach", available: false },
] as const;

export function AdminNav({ current }: { current: string }) {
  return (
    <nav aria-label="Admin sections" className="admin-nav">
      <ul>
        {ADMIN_SECTIONS.map((section) => (
          <li key={section.href}>
            {section.available ? (
              <a aria-current={section.href === current ? "page" : undefined} href={section.href}>
                {section.label}
              </a>
            ) : (
              <span className="admin-nav-unavailable" title="Not available yet">
                {section.label}
              </span>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pnpm --filter @hexera/admin-console test`
Expected: PASS (4 tests)

- [ ] **Step 6: Wrap the pages in the shell**

Create `apps/admin-console/src/app/(admin)/layout.tsx`:

```tsx
import { AdminNav } from "@/app/_components/nav";

// Every admin page renders inside this. There is no authentication here on purpose: IAP decides
// who reaches this container at all, and everyone it admits is an admin. See
// docs/deployment/admin-console-access.md.
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <p className="admin-brand">Hexera Admin</p>
        <AdminNav current="/" />
      </aside>
      <main className="admin-main">{children}</main>
    </div>
  );
}
```

- [ ] **Step 7: Make the landing page the Fleet placeholder**

Replace `apps/admin-console/src/app/(admin)/page.tsx` entirely. The existing "Boundaries" panel was
scaffolding and goes; this page is replaced wholesale by Admin-2, so anything elaborate is wasted:

```tsx
export default function FleetPage() {
  return (
    <>
      <h1>Fleet</h1>
      <p>
        Worker fleet, warm-start counts and Cloud Run revisions for both environments land here
        with the platform-ops pages. This deployment exists to prove the access boundary first.
      </p>
    </>
  );
}
```

- [ ] **Step 8: Verify the whole workspace gate**

Run: `pnpm install --frozen-lockfile && pnpm typecheck && pnpm lint && pnpm test && pnpm build`
Expected: all five succeed.

- [ ] **Step 9: Commit**

```bash
git add apps/admin-console pnpm-lock.yaml
git commit -m "feat(admin): add the navigation shell and put its tests under CI"
```

---

### Task 2: The admin image target

**Files:**
- Modify: `apps/admin-console/next.config.ts`
- Modify: `Dockerfile`
- Test: `tests/unit/deploy/test_admin_image_target.py`

**Interfaces:**
- Consumes: the shell from Task 1.
- Produces: a Dockerfile target named `admin`, listening on `$PORT` (default 8080), running as the
  base image's `node` user, entry command `node apps/admin-console/server.js`. Task 3 builds it.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the admin image target is pinned, unprivileged and serves on Cloud Run's port.
# Boundaries: it reads the Dockerfile; it builds nothing.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
DOCKERFILE = REPO / "Dockerfile"
NEXT_CONFIG = REPO / "apps" / "admin-console" / "next.config.ts"

# The console stages pin this; the admin stages must pin the SAME one. Two Node bases in one
# Dockerfile is two things to keep current, and a silent drift between them.
NODE_DIGEST = "sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e"


def _stage(name: str) -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"^FROM ", text, flags=re.M)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[s:end]
        if re.match(rf"^FROM \S+ AS {re.escape(name)}\s*$", block.splitlines()[0]):
            return block
    raise AssertionError(f"no Dockerfile stage named {name!r}")


def test_admin_stages_pin_the_same_node_digest_as_the_console():
    for name in ("admin-build", "admin"):
        first = _stage(name).splitlines()[0]
        assert NODE_DIGEST in first, (
            f"stage {name} does not pin {NODE_DIGEST}: {first!r}. Both Node images in this "
            f"Dockerfile must be the same base or they drift apart silently.")


def test_admin_runs_unprivileged():
    assert re.search(r"^USER (?!root)", _stage("admin"), flags=re.M)


def test_admin_honours_the_cloud_run_port():
    block = _stage("admin")
    assert "PORT=8080" in block
    assert "HOSTNAME=0.0.0.0" in block, (
        "Next's standalone server binds localhost without this, so the container starts, answers "
        "nothing, and the revision never becomes ready")
    assert 'CMD ["node", "apps/admin-console/server.js"]' in block


def test_next_emits_standalone_output_rooted_at_the_workspace():
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    assert 'output: "standalone"' in text
    assert "outputFileTracingRoot" in text, (
        "in a pnpm workspace Next traces from the app directory unless told otherwise, and the "
        "standalone bundle then omits the workspace packages the app imports")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_admin_image_target.py -v`
Expected: FAIL — `no Dockerfile stage named 'admin-build'`.

- [ ] **Step 3: Configure standalone output**

Replace `apps/admin-console/next.config.ts` with:

```typescript
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

const here = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  transpilePackages: ["@hexera/api-client"],
  // The container copies one self-contained tree rather than installing at image build time.
  output: "standalone",
  // In a pnpm workspace, tracing must start at the repository root: this app imports
  // @hexera/api-client from packages/, and a trace rooted here silently omits it - which fails at
  // runtime, not at build time.
  outputFileTracingRoot: path.join(here, "..", ".."),
};

export default nextConfig;
```

- [ ] **Step 4: Add the two stages**

Append to `Dockerfile`, after the `console` stage:

```dockerfile
# ---------------------------------------------------------------------------
# THE ADMIN CONSOLE. The same shape as the console stages above and pinned to the same Node base,
# deliberately: two Node images in one Dockerfile would be two things to keep current.
FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS admin-build
WORKDIR /build
ENV CI=1
RUN corepack enable
COPY pnpm-workspace.yaml pnpm-lock.yaml package.json tsconfig.base.json ./
COPY packages/ ./packages/
COPY apps/admin-console/package.json ./apps/admin-console/
RUN pnpm install --frozen-lockfile --filter @hexera/admin-console...
COPY apps/admin-console/ ./apps/admin-console/
RUN pnpm --filter @hexera/admin-console build

FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS admin
ARG APP_VERSION
LABEL org.opencontainers.image.title="Hexera Admin Console" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="The Hexera operations console" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"
WORKDIR /srv
# HOSTNAME is load-bearing: Next's standalone server binds localhost without it, so on Cloud Run
# the container would start, answer nothing, and the revision would never become ready.
ENV NODE_ENV=production \
    PORT=8080 \
    HOSTNAME=0.0.0.0
COPY --from=admin-build /build/apps/admin-console/.next/standalone/ ./
COPY --from=admin-build /build/apps/admin-console/.next/static/ ./apps/admin-console/.next/static/
COPY --from=admin-build /build/apps/admin-console/public/ ./apps/admin-console/public/
# node:24-slim already ships a `node` user at uid 1000; creating another at that uid fails.
RUN chown -R node:node /srv
USER node
EXPOSE 8080
CMD ["node", "apps/admin-console/server.js"]
```

**If `apps/admin-console/public/` does not exist**, the third `COPY` fails the build. Create the
directory with a `.gitkeep` rather than deleting the line — Next serves `public/` and a later task
will put a favicon there.

- [ ] **Step 5: Build it and prove it serves**

```bash
APP_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' src/meshpipeline/__init__.py | head -1)"
docker build --target admin --build-arg APP_VERSION="$APP_VERSION" -t hexera-admin:local .
docker run --rm -d --name admin-smoke -p 8081:8080 hexera-admin:local
sleep 5
curl -fsS http://localhost:8081/api/internal/health && echo OK
curl -fsS http://localhost:8081/ | grep -q "Fleet" && echo "shell renders"
docker rm -f admin-smoke
```

Expected: `OK` and `shell renders`.

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/unit/deploy/test_admin_image_target.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add Dockerfile apps/admin-console tests/unit/deploy/test_admin_image_target.py
git commit -m "build: add the admin console image target"
```

---

### Task 3: The admin as a fourth release component

**Files:**
- Modify: `devtools/release/validate.sh`
- Test: `tests/unit/deploy/test_admin_release_component.py`

**Interfaces:**
- Consumes: the `admin` Dockerfile target from Task 2.
- Produces: `deploy/output/release.json` carrying an `admin` key under `components`, with
  `dockerfile_target`, `local_tag`, `local_image_id`, `workloads: ["admin-service"]`.
  `publish.sh` iterates `${REC_COMPONENTS}` generically and needs no change.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify Gate C builds, smokes and records the admin console alongside the others.
# Boundaries: it reads the validation script's declarations; it runs no docker build.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "devtools" / "release" / "validate.sh"
PUBLISH = REPO / "devtools" / "release" / "publish.sh"


def test_admin_is_a_built_component():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "[admin]=admin" in text, "the component-to-target map does not name the admin console"
    assert "for comp in app mesh console admin;" in text, (
        "the build loop does not cover the admin console, so no admin image is ever validated")


def test_admin_image_is_smoked_before_it_is_recorded():
    assert "admin image: serves its health route" in VALIDATE.read_text(encoding="utf-8"), (
        "an image that builds and cannot serve is a green gate and a broken deploy")


def test_admin_is_written_into_the_record():
    text = VALIDATE.read_text(encoding="utf-8")
    assert '"admin":' in text
    assert '"admin-service"' in text, "the admin component does not name the workload it becomes"


def test_publication_needs_no_per_component_change():
    assert "for comp in ${REC_COMPONENTS}" in PUBLISH.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_admin_release_component.py -v`
Expected: FAIL on the first three.

- [ ] **Step 3: Build the admin component**

In `devtools/release/validate.sh`, extend the target map and the build loop:

```bash
declare -A TARGET=( [app]=pipeline [mesh]=mesh [console]=console [admin]=admin )
declare -A IMAGE_ID=() IMAGE_TAG=()
for comp in app mesh console admin; do
```

- [ ] **Step 4: Smoke the admin image**

After the existing console smoke block, add:

```bash
if [ -n "${IMAGE_ID[admin]}" ]; then
  # An image that builds and cannot serve is a green gate and a broken deploy. The admin console
  # needs no credentials to boot - IAP is its gate, and it holds no session of its own.
  _acid="$(docker run --rm -d --label "amp-release=${STAMP}" -P "${IMAGE_TAG[admin]}" 2>/dev/null || true)"
  if [ -n "${_acid}" ]; then
    _aport="$(docker port "${_acid}" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')"
    _aok=1
    for _try in 1 2 3 4 5 6 7 8 9 10; do
      curl -fsS --max-time 5 "http://127.0.0.1:${_aport}/api/internal/health" >/dev/null 2>&1 \
        && { _aok=0; break; }
      sleep 2
    done
    docker rm -f -v "${_acid}" >/dev/null 2>&1 || true
    if [ "${_aok}" = "0" ]; then
      record "admin image: serves its health route" passed 1
    else
      record "admin image: serves its health route" failed 1 \
        "the container started but never answered /api/internal/health"
    fi
  else
    record "admin image: serves its health route" failed 1 "the container did not start"
  fi
else
  record "admin image: serves its health route" not_run 1 "no admin image was built"
fi
```

- [ ] **Step 5: Record the component**

Extend the `COMPONENTS="$(python3 - …)"` block to pass and unpack a fourth triple. The Python
becomes:

```python
import json, sys
at, atag, aid, mt, mtag, mid, ct, ctag, cid, dt, dtag, did = sys.argv[1:13]
print(json.dumps({
  "app":     {"dockerfile_target": at, "local_tag": atag, "local_image_id": aid,
              "workloads": ["api-service", "pipeline-job"]},
  "mesh":    {"dockerfile_target": mt, "local_tag": mtag, "local_image_id": mid,
              "workloads": ["mesh-job"]},
  "console": {"dockerfile_target": ct, "local_tag": ctag, "local_image_id": cid,
              "workloads": ["console-service"]},
  "admin":   {"dockerfile_target": dt, "local_tag": dtag, "local_image_id": did,
              "workloads": ["admin-service"]},
}))
```

and the shell must pass `"${TARGET[admin]}" "${IMAGE_TAG[admin]:-}" "${IMAGE_ID[admin]:-}"` as the
last three arguments, in that order. A mismatch between the argument order and the unpacking
silently records the wrong target against the wrong component.

- [ ] **Step 6: Update the header comment**

`validate.sh`'s header says "Three images are deployable". Change it to four and add the admin line.
A comment that contradicts the code is a defect.

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/unit/deploy/test_admin_release_component.py tests/unit/deploy/test_release_promotion.py -v`
Expected: PASS. If `test_release_promotion.py` asserts a closed component set, extend its fixtures
with an `admin` component rather than weakening the assertion.

Do **not** run `make release-validate` — it builds the multi-gigabyte mesh image. Gate C runs once
in final verification.

- [ ] **Step 8: Commit**

```bash
git add devtools/release/validate.sh tests/unit/deploy/
git commit -m "release: build, smoke and record the admin console as a fourth component"
```

---

### Task 4: Promote the admin image

**Files:**
- Modify: `deploy/gcp/scripts/promote-release.sh`
- Test: `tests/unit/deploy/test_release_promotion.py` (extend)

**Interfaces:**
- Consumes: `components.admin.reference` from Task 3's record.
- Produces: `ADMIN_IMAGE=<registry>/admin@sha256:…` in the deployment env file. Task 7 reads it.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/deploy/test_release_promotion.py`. **Build the record with the repository's
real HEAD commit and tree** using the helpers already in that file — `promote-release.sh` refuses a
record whose commit is not HEAD, so a fabricated commit fails the test for an unrelated reason:

```python
ADMIN_DIGEST = "sha256:" + "a7" * 32


def test_promotion_writes_the_admin_image(tmp_path):
    """A record carrying an admin component promotes it into the deployment env."""
    sha, tree = _head()
    rec = make_record(commit=sha, tree=tree)
    rec["components"]["admin"] = {
        "dockerfile_target": "admin",
        "local_tag": "meshpipeline-admin:abc1234",
        "local_image_id": "sha256:" + "b8" * 32,
        "registry_repository": REGISTRY,
        "publication_tag": "v0.0.0",
        "registry_digest": ADMIN_DIGEST,
        "reference": f"{REGISTRY}/admin@{ADMIN_DIGEST}",
        "workloads": ["admin-service"],
    }
    env_file = _promote(tmp_path, rec)
    assert f"ADMIN_IMAGE={REGISTRY}/admin@{ADMIN_DIGEST}" in env_file.read_text(encoding="utf-8")
```

Reuse whatever helper that file already has for resolving HEAD and for invoking the script — read it
first and match its idiom rather than writing a new `subprocess.run`. If it has no `_head()` or
`_promote()` helper under those names, use the ones it does have and adjust the test accordingly.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_release_promotion.py -k admin -v`
Expected: FAIL — `ADMIN_IMAGE=` is not written.

- [ ] **Step 3: Resolve and validate the admin reference**

In `promote-release.sh`, after the `CONSOLE_REF` line:

```bash
ADMIN_REF="$(_ref admin)" || die "the release record names no 'admin' component - re-run Gate C"
```

and add `"${ADMIN_REF}"` to the existing `for ref in …` digest-qualification loop.

- [ ] **Step 4: Write it into the env file**

Extend the embedded Python writer to take a fourth positional argument and add
`("ADMIN_IMAGE", admin)` to its `for key, value in (…)` tuple. Do not add a second writer — one
file, one writer. Add a report line beside the others:

```bash
log "admin (admin service):      ${ADMIN_REF}"
```

- [ ] **Step 5: Update the header comment**

The component table near the top of `promote-release.sh` lists `mesh`, `app` and `console`, and the
`OUTPUT` line names three variables. Add `admin -> ADMIN_IMAGE   the Cloud Run admin service` and
correct the OUTPUT line to name all four.

- [ ] **Step 6: Run the test**

Run: `python -m pytest tests/unit/deploy/test_release_promotion.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add deploy/gcp/scripts/promote-release.sh tests/unit/deploy/test_release_promotion.py
git commit -m "deploy: promote the validated admin digest into the deployment env"
```

---

### Task 5: Declare and validate the admin tier

**Files:**
- Modify: `deploy/gcp/scripts/bootstrap-env.sh`
- Modify: `deploy/gcp/scripts/validate-config.sh`
- Modify: `deploy/gcp/scripts/enable-apis.sh`
- Test: `tests/unit/deploy/test_admin_config_declaration.py`

**Interfaces:**
- Consumes: nothing.
- Produces, in the generated deployment env: `CLOUDRUN_ADMIN_SERVICE`, `ADMIN_SERVICE_ACCOUNT`,
  `ADMIN_CPU`, `ADMIN_MEMORY`, `ADMIN_CONCURRENCY`, `ADMIN_MIN_INSTANCES`, `ADMIN_MAX_INSTANCES`,
  `ADMIN_INGRESS`, `ADMIN_IMAGE`. Tasks 6-8 read these.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the deployment env declares the admin tier and never a public invoker.
# Boundaries: it reads the generator and the validator; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"

DECLARED = (
    "CLOUDRUN_ADMIN_SERVICE", "ADMIN_SERVICE_ACCOUNT", "ADMIN_CPU", "ADMIN_MEMORY",
    "ADMIN_CONCURRENCY", "ADMIN_MIN_INSTANCES", "ADMIN_MAX_INSTANCES", "ADMIN_INGRESS",
    "ADMIN_IMAGE",
)

_BASE = {
    "DEPLOYMENT_ID": "t", "GCP_PROJECT_ID": "fake-proj", "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
}


def test_every_admin_setting_is_declared():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    missing = [n for n in DECLARED if f"{n}=" not in text]
    assert not missing, f"the deployment env declares no {missing}"


def test_the_admin_image_slot_survives_regeneration():
    assert "ADMIN_IMAGE=${ADMIN_IMAGE:-}" in BOOTSTRAP.read_text(encoding="utf-8"), (
        "without the ${VAR:-} slot every bootstrap run erases the promoted admin digest, "
        "exactly as it once did for CONSOLE_IMAGE")


def test_no_admin_public_invoker_setting_exists():
    # The admin console must never be allUsers-invokable. Not configurable, not defaulted off -
    # absent, so no deployment can turn it on by setting a variable.
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "ADMIN_ALLOW_UNAUTHENTICATED" not in text, (
        "a public-invoker knob exists for the admin tier; IAP over an allUsers binding protects "
        "nothing, so this must not be settable")


def _validate(env_extra: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in {**_BASE, **env_extra}.items()) + "\n",
                        encoding="utf-8")
    scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("MINIO_")}
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True,
                          env={**scrubbed, "DEPLOY_ENV_FILE": str(env_file)})


def test_inverted_admin_scaling_is_refused(tmp_path):
    done = _validate({"CLOUDRUN_ADMIN_SERVICE": "t-admin",
                      "ADMIN_MIN_INSTANCES": "4", "ADMIN_MAX_INSTANCES": "2"}, tmp_path)
    assert done.returncode != 0
    assert "ADMIN_MIN_INSTANCES" in done.stdout + done.stderr


def test_no_admin_is_not_an_error(tmp_path):
    done = _validate({}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_admin_config_declaration.py -v`
Expected: FAIL on the declaration and scaling tests.

- [ ] **Step 3: Declare the tier**

In `bootstrap-env.sh`, in the generated-env heredoc after the console block:

```bash
# THE ADMIN TIER. Empty CLOUDRUN_ADMIN_SERVICE means this deployment serves no admin console and
# that stage is skipped, the same arrangement the API and console tiers use.
#
# THERE IS NO ADMIN_ALLOW_UNAUTHENTICATED, deliberately. The console has one because its sign-in
# page must be publicly reachable; the admin console's gate is IAP, and IAP in front of a service
# that still carries an allUsers binding protects nothing - the two are separate checks. Making
# that a setting would make the protection optional, so it is not one.
CLOUDRUN_ADMIN_SERVICE=${CLOUDRUN_ADMIN_SERVICE:-}
ADMIN_SERVICE_ACCOUNT=${ADMIN_SERVICE_ACCOUNT:-${DEPLOY_ID}-admin}
# The smallest tier here: it renders pages for a handful of people and runs no model call.
ADMIN_CPU=${ADMIN_CPU:-1}
ADMIN_MEMORY=${ADMIN_MEMORY:-512Mi}
ADMIN_CONCURRENCY=${ADMIN_CONCURRENCY:-80}
ADMIN_MIN_INSTANCES=${ADMIN_MIN_INSTANCES:-0}
ADMIN_MAX_INSTANCES=${ADMIN_MAX_INSTANCES:-2}
ADMIN_INGRESS=${ADMIN_INGRESS:-all}
# The promoted digest, kept across regeneration exactly as MESH_IMAGE and APP_IMAGE are.
ADMIN_IMAGE=${ADMIN_IMAGE:-}
```

Also add `ADMIN_SERVICE="${CLOUDRUN_ADMIN_SERVICE:-}"` in `discover()` **and read it back** in the
heredoc as `CLOUDRUN_ADMIN_SERVICE=${ADMIN_SERVICE}` — matching how `CLOUDRUN_MESH_JOB=${MESH_JOB}`
and `CLOUDRUN_CONSOLE_SERVICE=${CONSOLE_SERVICE}` work. Setting a variable in `discover()` that
`emit_env` never reads is an unused-variable warning and a trap for the next change.

- [ ] **Step 4: Validate it**

In `validate-config.sh`, after the console block:

```bash
# THE ADMIN CONSOLE. Empty CLOUDRUN_ADMIN_SERVICE means this deployment serves none.
if [ -n "${CLOUDRUN_ADMIN_SERVICE:-}" ]; then
  if [[ "${ADMIN_MIN_INSTANCES:-}" =~ ^[0-9]+$ ]] && [[ "${ADMIN_MAX_INSTANCES:-}" =~ ^[0-9]+$ ]]; then
    [ "${ADMIN_MIN_INSTANCES}" -le "${ADMIN_MAX_INSTANCES}" ] \
      || add "ADMIN_MIN_INSTANCES (${ADMIN_MIN_INSTANCES}) exceeds ADMIN_MAX_INSTANCES (${ADMIN_MAX_INSTANCES})"
  fi
fi
```

- [ ] **Step 5: Enable the IAP API**

`create-admin-service.sh` cannot enable IAP on a project where `iap.googleapis.com` is off. Add it
to the API list in `deploy/gcp/scripts/enable-apis.sh`, following that file's existing style and its
comment convention for why each API is needed.

- [ ] **Step 6: Run the tests and the secret gate**

Run: `python -m pytest tests/unit/deploy/test_admin_config_declaration.py tests/unit/deploy/test_discovery.py -v`
Run: `python devtools/quality/check_deploy_secrets.py`
Run: `shellcheck deploy/gcp/scripts/bootstrap-env.sh deploy/gcp/scripts/validate-config.sh deploy/gcp/scripts/enable-apis.sh`
Expected: tests pass, gate exits 0, shellcheck shows only the pre-existing `SC1091` info.

- [ ] **Step 7: Commit**

```bash
git add deploy/gcp/scripts/ tests/unit/deploy/test_admin_config_declaration.py
git commit -m "deploy: declare the admin tier and enable the IAP API"
```

---

### Task 6: The admin service

The largest task. Model it on `deploy/gcp/scripts/create-console-service.sh` — **read that file
first** — and invert the parts named below.

**Files:**
- Create: `deploy/gcp/scripts/create-admin-service.sh`
- Test: `tests/unit/deploy/test_admin_service_stage.py`

**Interfaces:**
- Consumes: `ADMIN_IMAGE` (Task 4), the config from Task 5.
- Produces: a Cloud Run service named by `CLOUDRUN_ADMIN_SERVICE`, IAP-enabled, and an
  `admin_url=<url>` line appended to `$GITHUB_OUTPUT` when that variable is set — Task 8's
  post-deploy check reads it.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the admin rollout is IAP-gated, never public, and digest-pinned.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-admin-service.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "run services describe"; then
  if has "status.url"; then printf '%s\n' "https://t-admin.run.app"; exit 0; fi
  exit "${FAKE_SVC_EXISTS_RC:-1}"
fi
if has "run services get-iam-policy"; then printf '%s\n' "${FAKE_POLICY_MEMBERS:-}"; exit 0; fi
exit 0
"""

_ADMIN_DIGEST = "us-central1-docker.pkg.dev/fake-proj/hexera/admin@sha256:c0ffee"

_ENV = {
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "CLOUDRUN_ADMIN_SERVICE": "t-admin",
    "ADMIN_SERVICE_ACCOUNT": "t-admin",
    "ADMIN_IMAGE": _ADMIN_DIGEST,
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    def _run(over: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
        vals = {**_ENV, **(over or {})}
        env_file = tmp_path / "generated.env"
        env_file.write_text("\n".join(f"{k}={v}" for k, v in vals.items() if v != "") + "\n",
                            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file)})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


def test_no_admin_service_is_a_stated_skip(run):
    done, calls = run({"CLOUDRUN_ADMIN_SERVICE": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "run deploy" not in calls


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"ADMIN_IMAGE": "us-central1-docker.pkg.dev/p/r/admin:v1"})
    assert done.returncode != 0
    assert "run deploy" not in calls


def test_the_rollout_is_digest_pinned_and_iap_enabled(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert f"--image {_ADMIN_DIGEST}" in calls
    assert "--iap" in calls, "IAP was not enabled on the service"


def test_the_service_is_never_publicly_invokable(run):
    """The single most important assertion in this file."""
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--no-allow-unauthenticated" in calls
    assert "add-iam-policy-binding" not in calls or "allUsers" not in calls, (
        "the admin service was granted a public invoker binding; IAP in front of that protects "
        "nothing, because the two are separate gates")


def test_an_existing_public_binding_is_removed(run):
    done, calls = run({"FAKE_POLICY_MEMBERS": "allUsers;serviceAccount:x@y"})
    assert done.returncode == 0, done.stderr
    assert "remove-iam-policy-binding" in calls and "allUsers" in calls


def test_the_iap_service_agent_is_granted_invoker(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "service-224734058693@gcp-sa-iap.iam.gserviceaccount.com" in calls, (
        "IAP terminates the request and calls Cloud Run itself; without this grant every request "
        "403s in a way that looks like the IAP policy is wrong when it is not")
    assert "roles/run.invoker" in calls


def test_the_url_is_published_for_the_post_deploy_check(run, tmp_path):
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    env_file = tmp_path / "generated.env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in _ENV.items()) + "\n", encoding="utf-8")
    subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                   env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                        "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file),
                        "GITHUB_OUTPUT": str(out)})
    assert "admin_url=https://t-admin.run.app" in out.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_admin_service_stage.py -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the script**

Create `deploy/gcp/scripts/create-admin-service.sh`, modelled on `create-console-service.sh`. Keep
these unchanged from that file: the stated-skip-and-exit-0 when the service name is empty;
`require_vars` and `require_digest_reference` before any mutation; the service-account creation with
its actionable failure message; the declarative `--set-env-vars` with the drift refusal that prints
names only; and the `$GITHUB_OUTPUT` publication at the end (as `admin_url`).

Invert or add exactly these:

```bash
# WHY THIS SERVICE IS THE INVERSE OF THE CONSOLE'S. The public console is allUsers-invokable
# because its sign-in page must be reachable before anyone can authenticate. The admin console has
# no public entry point: its gate is IAP, which authenticates a Google identity before the request
# reaches this container.
#
# IAP AND THE INVOKER POLICY ARE SEPARATE GATES. A service with IAP enabled AND an allUsers
# invoker binding is not protected - the binding admits the request regardless. That is why this
# script never grants allUsers, actively removes one it finds, and has no setting to turn the
# behaviour off. See docs/deployment/admin-console-access.md.
```

The rollout adds `--iap` and always `--no-allow-unauthenticated`:

```bash
deploy_args+=(--no-allow-unauthenticated --iap)
```

Note this differs from the console, where `--no-allow-unauthenticated` is added only on create.
Here it is unconditional, because there is no state in which this service should be public.

The IAP service agent grant, after the rollout:

```bash
# IAP terminates the request and calls Cloud Run itself, so the IAP service agent - not the end
# user - is what needs the invoker role. Without it every request returns 403 and the failure
# reads as a broken IAP policy rather than a missing binding.
IAP_AGENT="service-${GCP_PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"
gc run services add-iam-policy-binding "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
  --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker >/dev/null \
  || warn "could not grant ${IAP_AGENT} roles/run.invoker on ${ADMIN_SERVICE}. Requests will
       return 403 until it holds that role:
         gcloud run services add-iam-policy-binding ${ADMIN_SERVICE} --project ${GCP_PROJECT_ID} \\
           --region ${GCP_REGION} --member serviceAccount:${IAP_AGENT} --role roles/run.invoker"
```

And the public-binding removal, which replaces the console's conditional grant entirely:

```bash
# NOT a choice, unlike the console's step 6. A public binding on this service is always wrong, so
# it is removed whenever it is found rather than being governed by a variable.
POLICY_MEMBERS="$(gc run services get-iam-policy "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
  --format='value(bindings.members)' 2>/dev/null || true)"
case "${POLICY_MEMBERS}" in
  *allUsers*)
    warn "${ADMIN_SERVICE} carried a public invoker binding - removing it. IAP does not protect a
       service that allUsers may invoke; the two are separate gates."
    gc run services remove-iam-policy-binding "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
      --member allUsers --role roles/run.invoker >/dev/null ;;
esac
```

`require_vars` for this script is `GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID GCP_PROJECT_NUMBER` —
the project number is needed for the IAP service agent's address, and an unbound-variable crash
after the service account has been created is worse than a stated refusal before it.

The admin console needs **no secret bindings** in this sub-project: IAP is its gate and it holds no
session of its own. Omit the secret loop entirely rather than carrying an empty one.

- [ ] **Step 4: Make it executable, then run the tests**

```bash
chmod +x deploy/gcp/scripts/create-admin-service.sh
python -m pytest tests/unit/deploy/test_admin_service_stage.py -v
shellcheck deploy/gcp/scripts/create-admin-service.sh
/bin/bash -n deploy/gcp/scripts/create-admin-service.sh
python devtools/quality/check_deploy_secrets.py
```

Expected: 7 tests pass; shellcheck shows only `SC1091`; `bash -n` is silent (this is bash 3.2 on
macOS — the script must parse there); the secret gate exits 0.

- [ ] **Step 5: Commit**

```bash
git add deploy/gcp/scripts/create-admin-service.sh tests/unit/deploy/test_admin_service_stage.py
git commit -m "deploy: provision the admin console behind IAP"
```

---

### Task 7: Select and record the admin stage

**Files:**
- Modify: `deploy/gcp/scripts/deploy.sh`
- Modify: `deploy/gcp/scripts/write-deployment-state.sh`
- Test: `tests/unit/deploy/test_admin_component_selection.py`

**Interfaces:**
- Consumes: `create-admin-service.sh` from Task 6.
- Produces: `DEPLOY_COMPONENTS=admin` selects the admin stage and nothing else;
  `deployment.json` carries `images.admin` and `resources.admin_service`.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify 'admin' is a selectable component, ordered after the API, and recorded.
# Boundaries: it reads the driver and the state writer; it runs no deploy.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"
WRITER = REPO / "deploy" / "gcp" / "scripts" / "write-deployment-state.sh"


def test_admin_is_a_known_component():
    known = [l for l in DEPLOY.read_text(encoding="utf-8").splitlines()
             if l.strip().startswith("_known=")]
    assert known and "admin" in known[0], (
        "'admin' is not a known component, so DEPLOY_COMPONENTS=admin would be refused as a typo")


def test_the_admin_stage_runs_the_admin_script():
    assert "create-admin-service.sh" in DEPLOY.read_text(encoding="utf-8")


def test_an_unselected_admin_states_its_skip():
    assert "skipped admin" in DEPLOY.read_text(encoding="utf-8")


def test_the_admin_rolls_out_after_the_api():
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("create-api-service.sh") < text.index("create-admin-service.sh")


def test_the_admin_service_is_a_recorded_resource():
    text = WRITER.read_text(encoding="utf-8")
    assert '"admin_service"' in text
    assert "ADMIN_DIGEST" in text, (
        "a run that did not select 'admin' must still record the digest the live service carries")
    assert "_disp admin" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_admin_component_selection.py -v`
Expected: FAIL on all five.

- [ ] **Step 3: Add the component and the stage**

In `deploy.sh`, extend `_known`:

```bash
  _known="images data storage migrate queue workers console admin"
```

and add a stage after the console stage:

```bash
stage "Admin console (the promoted admin digest, behind IAP)"
# AFTER the API, like the console: its pages read the API and the database, so an admin console
# that rolls out first shows errors until the rest catches up.
if want admin; then
  bash "${S}/create-admin-service.sh"
else
  skipped admin "the admin console keeps serving whichever digest it already has"
fi
```

`STAGE_TOTAL` is computed by `grep -c '^stage "'`, so it needs no edit — confirm that is still true
rather than assuming it.

- [ ] **Step 4: Record it in the manifest**

In `write-deployment-state.sh`, beside the console's digest read-back:

```bash
if _selected admin; then
  ADMIN_DIGEST="$(resolve_digest "${ADMIN_IMAGE:-}" 2>/dev/null || printf '%s' "${ADMIN_IMAGE:-}")"
elif [ -n "${CLOUDRUN_ADMIN_SERVICE:-}" ]; then
  ADMIN_DIGEST="$(gc run services describe "${CLOUDRUN_ADMIN_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
else
  ADMIN_DIGEST=""
fi
```

Add `ADMIN_DISP="$(_disp admin "${ADMIN_SERVICE_DISPOSITION:-}" admin_service)"` and
`ADMIN_RECON="$(_recon admin)"` beside their console equivalents, add `"admin": "${ADMIN_DIGEST}"`
to the one-line `images` map, and add the resource beside `console_service`:

```python
doc["resources"]["admin_service"] = {
    "name": "${CLOUDRUN_ADMIN_SERVICE:-}",
    "service_account": "${ADMIN_SERVICE_ACCOUNT:-}",
    "disposition": "${ADMIN_DISP}",
    "reconciled": ${ADMIN_RECON},
}
```

This is a Python heredoc inside bash — `${...}` is shell-expanded before Python sees it. Match the
surrounding quoting exactly; a mismatch fails after the deploy has finished.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/deploy/test_admin_component_selection.py tests/unit/deploy/test_fresh_install_contract.py -v`
Expected: PASS. `test_fresh_install_contract.py` asserts an exact set of image keys; extend it to
include `admin` rather than weakening it.

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/scripts/ tests/unit/deploy/
git commit -m "deploy: make admin a selectable component and record it in the manifest"
```

---

### Task 8: Deploy it, and prove it is not public

**Files:**
- Modify: `.github/workflows/deploy.yml`
- Test: `tests/unit/deploy/test_admin_deploy_wiring.py`

**Interfaces:**
- Consumes: the `admin` component (Task 7) and `admin_url` (Task 6).
- Produces: dev deploys the admin console; a post-deploy step that fails the run if it is reachable
  anonymously.

- [ ] **Step 1: Write the failing test**

```python
# Responsibility: Verify the workflow pins an admin service for dev, not prod, and proves it is not public.
# Boundaries: it reads the workflow document; it runs no deploy.
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
WF = REPO / ".github" / "workflows" / "deploy.yml"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def test_the_target_publishes_an_admin_service_output():
    assert "admin_service" in _doc()["jobs"]["target"]["outputs"]


def test_dev_pins_an_admin_service_and_prod_does_not():
    text = WF.read_text(encoding="utf-8")
    assert 'echo "admin_service=dev-admin"' in text
    assert 'echo "admin_service="' in text, (
        "prod must be left deliberately empty, like the console, so a release tag does not "
        "provision an unasked-for billed service")


def test_the_deploy_job_maps_the_admin_service():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "CLOUDRUN_ADMIN_SERVICE" in body


def test_a_manual_run_can_select_the_admin_console():
    options = _doc()[True]["workflow_dispatch"]["inputs"]["components"]["options"]
    assert [o for o in options if "admin" in o], (
        f"no components option contains 'admin', so a manual run cannot deploy it. Offered: {options}")


def test_the_deployed_admin_is_proved_not_public():
    body = yaml.dump(_doc()["jobs"]["deploy"])
    assert "admin_url" in body, "nothing verifies the deployed admin console"
    assert "allUsers" in body, (
        "the post-deploy check does not assert the absence of a public invoker binding - the one "
        "failure that makes IAP pointless")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/deploy/test_admin_deploy_wiring.py -v`
Expected: FAIL on all five.

- [ ] **Step 3: Pin the service, dev only**

In the `target` job, add an `admin_service` output beside `console_service`, emit
`admin_service=dev-admin` on the dev branch and `admin_service=` (empty) on the prod branch. Comment
the prod line in the file's own voice, for the same reason the console's is empty: a release tag
reconciles every tier it is told about, so naming one here provisions a billed production service
nobody asked for.

Map it in the `deploy` job's env block:

```yaml
      CLOUDRUN_ADMIN_SERVICE: ${{ needs.target.outputs.admin_service }}
```

- [ ] **Step 4: Offer it to a manual run**

Add to the `workflow_dispatch` `components` choice list, keeping the existing entries and their
comment style:

```yaml
          - admin                       # the admin console alone - the fast loop while iterating on it
```

- [ ] **Step 5: Prove it is not public, after deploy**

Add a step after the console verification, guarded on `steps.deploy.outputs.admin_url != ''`:

```yaml
      # THE ONE CHECK THAT MATTERS. IAP over a service that still carries an allUsers invoker
      # binding protects nothing - the two are separate gates - and the symptom is silence,
      # because the page loads and looks fine. Asserted here rather than left to a runbook.
      - name: Verify the admin console is not publicly reachable
        if: steps.deploy.outputs.admin_url != ''
        env:
          ADMIN_URL: ${{ steps.deploy.outputs.admin_url }}
          ADMIN_SERVICE: ${{ needs.target.outputs.admin_service }}
          REGION: ${{ needs.target.outputs.region }}
          PROJECT: ${{ needs.target.outputs.project }}
        run: |
          set -euo pipefail
          code="$(curl -s -o /dev/null --max-time 15 -w '%{http_code}' "${ADMIN_URL}/")"
          case "${code}" in
            200)
              echo "::error::an anonymous request to ${ADMIN_URL}/ returned 200 - the admin console is publicly reachable"
              exit 1 ;;
            *)
              echo "OK: anonymous request refused (${code})" ;;
          esac
          members="$(gcloud run services get-iam-policy "${ADMIN_SERVICE}" \
            --project "${PROJECT}" --region "${REGION}" \
            --format='value(bindings.members)' 2>/dev/null | tr ';' '\n' || true)"
          if printf '%s\n' "${members}" | grep -qx allUsers; then
            echo "::error::${ADMIN_SERVICE} carries an allUsers invoker binding - IAP is not protecting it"
            exit 1
          fi
          echo "OK: no public invoker binding"
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/deploy/test_admin_deploy_wiring.py tests/unit/deploy/test_console_component_selection.py -v`
Expected: PASS

- [ ] **Step 7: Confirm the workflow's shell parses**

Extract the new step's `run:` script to a temp file and run `bash -n` on it. A YAML-valid workflow
with a broken script fails only at deploy time.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/deploy.yml tests/unit/deploy/test_admin_deploy_wiring.py
git commit -m "deploy: deploy the admin console to dev and prove it is not public"
```

---

### Task 9: Documentation

**Files:**
- Modify: `docs/deployment/admin-console-access.md`
- Modify: `docs/deployment/environments-and-delivery.md`

- [ ] **Step 1: Retire the prerequisite**

`docs/deployment/admin-console-access.md` §9 says the admin console has no Cloud Run service and
that this is the step before §4. That is now false. Replace §9 with what is true: the service is
deployed by the `admin` component, its name is `dev-admin`, prod is deliberately unpinned, and the
`--iap` / `--no-allow-unauthenticated` / service-agent grant that §4 describes are applied by
`create-admin-service.sh` on every run rather than by hand. Keep §3's one-time OAuth brand setup —
that genuinely is still manual.

Re-read the whole document afterwards and confirm no other sentence was falsified by this plan.

- [ ] **Step 2: Document the tier**

In `docs/deployment/environments-and-delivery.md`, add the admin console to the "What runs in each"
table (`dev-admin` / deliberately unset) and a short section describing it beside the console's:
that it is the only tier not publicly invokable, that IAP is its gate, that it carries no secrets in
this sub-project, and that VPC egress and a database role arrive with Admin-2.

- [ ] **Step 3: Verify no credential leaked**

Run: `python devtools/quality/check_deploy_secrets.py`
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs: record the admin tier and retire the access guide's prerequisite"
```

---

## Final verification

Not a task — run before opening the PR.

- [ ] `python -m pytest tests/unit/deploy -q` — the deploy suite passes (5-7 failures are
      pre-existing on macOS: docker-compose flags, missing pyenv 3.12, and a bash-3.2 parse error in
      `deploy-preflight.sh`; confirm any failure is one of those)
- [ ] `pnpm install --frozen-lockfile && pnpm typecheck && pnpm lint && pnpm test && pnpm build`
- [ ] `make lint && python devtools/quality/mypy_ratchet.py`
- [ ] `python devtools/quality/check_deploy_secrets.py`
- [ ] `shellcheck` and `/bin/bash -n` on every changed shell script
- [ ] `make release-validate` — Gate C builds and smokes four components, verdict `passed`
- [ ] Deploy to dev with `components=admin`, then follow
      `docs/deployment/admin-console-access.md` §3 (one-time OAuth brand) and §5 (grant yourself
      access)
- [ ] Confirm §6's three checks: anonymous request is not 200, a granted identity gets in, no
      `allUsers` binding

## What this plan deliberately does not do

- VPC egress and the Postgres role — deferred to Admin-2, which is the first thing that queries.
- Any admin page with real data. Fleet, Costs and Activity are Admin-2; Customers is Admin-3;
  Outreach is Admin-4.
- Reading `X-Goog-IAP-JWT-Assertion`. Everyone IAP admits is an admin until there is a reason to
  tell them apart.
- Prod. `admin_service` is empty for prod deliberately; turning it on is a reviewed diff plus a
  repeat of the access guide's §3-§6 against `hexera-prod`.
