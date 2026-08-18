# Security and privacy

What the code enforces today. Nothing here is a certification or a compliance claim; each
statement names a mechanism you can find in the source.

## Credentials

- Local credentials live in `.env`, which is gitignored and generated with every secret blank.
- The developer's Google credential stays in their gcloud config and is bind-mounted
  read-only into the one service that dispatches meshes. It is never copied into the
  repository, an image or `.env`.
- Provisioning creates no credential of its own. `make mesh-deploy` grants roles to the
  account you are already signed in as and to the mesh job's service account; it stores no
  secret and issues no key. Running the application elsewhere means supplying the same
  settings as environment, by whatever your platform provides.
- `ENV=production` refuses to start without `MESH_API_KEY`, a non-wildcard `CORS_ORIGINS` and a
  set `POSTGRES_PASSWORD`.

Diagnostics never print a credential. Where an error must identify a database, it prints host and
database name only, the password, the username and every query parameter are dropped rather than
masked, because query parameters carry tokens and endpoint keys on hosted providers.

## Identity and tenant isolation

Every durable record carries an owner. Queries are scoped by owner, and an object key is derived
from content and owner rather than being guessable by sequence.

`USER_TOKEN_SECRET` HMAC-signs the caller's identity header. Without it, identity is
self-asserted, acceptable on a laptop, not on anything reachable by someone you do not control.
Set it wherever the deployment is exposed.

## Object access

Downloads are issued as time-limited signed URLs, bounded by `MINIO_SIGNED_URL_TTL`. A bundle is
not served from a path a client can construct.

Uploaded geometry is addressed by the SHA-256 of its contents, so the same bytes uploaded twice
resolve to one object and no filename from a user reaches storage as a path.

## Model-authored code and configuration

Model output is treated as untrusted input, not as a trusted program.

- **`run_python`** executes under an OS sandbox: seccomp blocks the `socket` syscall, so the code
  has no network and cannot exfiltrate anything; Landlock confines it to its own workspace;
  rlimits and a container process cap bound its resources. `RUN_PYTHON_REQUIRE_SANDBOX` fails
  closed, if the jail cannot be applied, the code does not run.
- **OpenFOAM dictionaries** are inspected before any mesher starts. A case embedding OpenFOAM's
  parse-time code directives (`#codeStream`, `#calc`, `#system`) is rejected, because those
  execute during dictionary parsing.
- **Every foam subprocess** runs with a secret-scrubbed environment.
- **Model-authored code is never the deliverable.** It may compute a value inside the jail; the
  engine's configuration is emitted by a deterministic renderer from a declarative spec.

## Render artifact confinement

Every artifact a reviewer might open passes one fence before a loader sees it. The fence resolves
the declared artifact key against the job workspace and refuses anything that escapes it -
absolute paths outside the workspace, `..` traversals, and symlinks that resolve outward. A
symlink that stays inside is allowed.

Content is checked, not the extension: a file whose bytes do not match its declared format is
refused before gmsh or PyVista is asked to read it. Diagnostics name the manifest key, never the
filesystem path, so a refusal cannot leak where something was.

There is exactly one implementation of this fence, and a test fails if a second appears.

## Ownership fencing

A run has a durable owner and an execution generation. A worker that lost ownership, because a
newer generation took over after a crash or redelivery, is fenced before any terminal side
effect: it stops rather than racing the current generation. Side effects namespace on the
generation, so a superseded worker cannot overwrite the current one's workspace or artifacts.

## Public error redaction

What a user sees is a classified failure category, not an exception. The classification decides
both the persisted reason and the public message, so internal detail cannot reach a browser
through an error path.

## What a live page may show

`PUBLIC_TRACE_MODE` is a publication policy, independent of data collection:

- `safe` (default) publishes reasoning and tool activity without content.
- `raw` additionally publishes provider reasoning, real tool names, arguments, results and the
  reviewer's inspection images, and requires `ALLOW_PUBLIC_RAW_TRACE=true` as a second
  acknowledgement.

Secrets and model or provider identity are redacted in **both** modes.

## Data collection and retention

Collection is **on by default**. See
[operating-modes.md](../architecture/operating-modes.md#data-collection) for the full comparison.

- With collection off, nothing optional is stored: no capture rows, no corpus, no intake training
  metadata. The chat session, its messages, the job and its artifacts are still stored, a
  conversation has to survive a page reload, and a delivered mesh has to be downloadable.
- With collection enabled, finished runs are exported to `CORPUS_DIR` on this machine. Environment
  secrets and key-shaped strings are redacted on the way in; payloads are otherwise stored whole.
  Treat the corpus as sensitive.
- The corpus is operator-managed: nothing expires it. Remove samples you no longer want.
- Visible messages and the prompts actually sent to models are captured. Provider-internal
  reasoning is not.
- Capture is fail-open: a capture failure logs a warning and never fails a mesh job.

## Deletion and retention

- Uploaded geometry bytes are purged on a retention schedule. The **row** is kept as lineage -
  sessions, jobs and interpretations reference it, and a terminal result stays readable through it
  long after the bytes are gone. The row records that the object was purged and when.
- A purge is claimed before it runs, so two workers cannot both delete, and a claim that expires
  is retried rather than wedging the row.
- Failed jobs are retained for `FAILED_JOB_RETENTION_HOURS`, then their workspaces are cleaned.
- `make clean-workspaces` removes local job workspaces on demand.

## Reporting a vulnerability

Privately, to the repository owner. Not in an issue.
