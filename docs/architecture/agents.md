# Agents

The pipeline is a LangGraph state machine with eight nodes. Three of them call a model (intake,
builder, reviewer); the rest are deterministic system stages that decide, measure or terminalize.
This document describes what each owns and how control moves between them.

The graph is assembled in `src/meshpipeline/pipeline/graph.py`. Every node reads and writes the
single state object declared in `contracts/pipeline_state.py`, and every field in it has a
declared meaning and consumer in `pipeline/data_contract.py`, a node may not invent state keys.

## The graph

```text
START
  └─► node_intake ─────────────► node_engine_select ──┬─► node_geometry_admission ─┬─► node_builder
         │ (api failure)                              │                            │
         ▼                                            │ (already meshed)           │ (nothing to build)
   node_failure_handler                               ▼                            ▼
         │                                       node_reviewer  ◄──────────── node_executor
         ▼                                            │                            │
        END                                           ├─► END (pass)                ├─► END
                                                      ├─► node_classifier ──► node_builder
                                                      └─► node_failure_handler
```

Every conditional edge is a named routing function, not an inline lambda over free text, so the
reachable transitions are enumerable and tested.

## node_intake: the conversation

**Responsibility.** Turn what the user says into a typed, approved job.

**Inputs.** The user's messages, the uploaded geometry's identity and interpretation, and the
capability catalog.
**Outputs.** Purpose, dimensionality, engine choice, engine parameters, and an approval snapshot.

**Owns.** The conversation state, the requirements it reads back for approval, and the
approved-intent fingerprint. That fingerprint is the reason a run cannot quietly change meaning
after approval: the intent is recomputed before execution and the run is refused if it no longer
matches. The two policy version fields that participate are described in
[architecture/overview.md](overview.md#policy-versions).

**Boundaries.** It never meshes, never chooses parameters an engine did not declare, and never
proceeds past an ambiguous unit. A geometry whose physical scale is unconfirmed is asked about,
because STL and VTK carry no unit and a mis-scaled run is silently wrong rather than loudly broken.

**Failure.** A provider failure routes straight to the failure handler; the conversation is not
left half-committed.

## node_engine_select: matching capability to purpose

**Responsibility.** Resolve the engine the run will use.

Engines advertise capabilities (input kind, output kind, topologies); a purpose declares what it
requires. Compatibility is derived from both, so an engine serves any workflow its capabilities
can satisfy rather than being bound to one domain.

**Boundaries.** Deterministic, no model call. It selects; it does not validate the geometry.

**Routing.** Normally to geometry admission. A run that already carries a delivered mesh, a
resumed or re-reviewed job, goes straight to the reviewer.

## node_geometry_admission: is this geometry meshable

**Responsibility.** Decide whether the submitted geometry satisfies the chosen engine's declared
input contract, before any builder or mesher runs.

**Owns.** The admission verdict and its evidence: dimensionality, input kind, thickness ratios,
self-intersection checks, and the declared-extent gate where the engine uses one.

**Boundaries.** It admits or rejects. It never repairs geometry, and it never lowers a bar to let
something through.

**Routing.** To the builder normally; directly to the executor when the workspace already holds
everything the engine needs.

## node_builder: authoring the mesh configuration

**Responsibility.** Write the engine's configuration and drive it to a submitted mesh.

**Tools.** `write_file`, `read_file`, `list_directory`, `geometry_report`, `measure_scales`,
`run_python`, `run_mesh`, `web_search`, `submit_mesh`.

**Owns.** The attempt: its workspace, the files it authors, and the decision to submit. Attempt
directories are namespaced by execution generation, so a superseded run can never overwrite the
current one's work.

**Boundaries.** It authors configuration; it does not decide whether the mesh is good. Its
`run_python` executes under an OS sandbox with no network, and the OpenFOAM dictionaries it writes
are rejected before meshing if they contain parse-time code directives. Model-authored content is
treated as untrusted input throughout, see
[security-and-privacy.md](../deployment/security-and-privacy.md#model-authored-code-and-configuration).

**Budgets.** `BUILDER_MAX_ROUNDS` for a first attempt, `BUILDER_RETRY_MAX_ROUNDS` for a rebuild -
shorter, because a retry starts from a reviewed failure rather than from nothing.

**Failure.** A builder that cannot produce a submission routes to the failure handler rather than
handing an unusable workspace onward.

## node_executor: running the real mesher

**Responsibility.** Run the engine and evaluate its declared gates.

**Owns.** Native execution, the gate outcomes, and the deliverable check: an engine's bundle must
contain every member its deliverable declares required, or the run has not delivered. A partial
case is not success.

**Boundaries.** It executes and measures. It forms no opinion about mesh quality beyond the
declared numerical bars, and it never renders anything.

**Routing.** To the reviewer when the gates passed, to the classifier when they did not and a
retry is sensible, or to END when the run is terminal.

## node_classifier: why did that fail, and what should change

**Responsibility.** Turn a failure into a specific, actionable instruction for the next builder
attempt.

**Owns.** The failure category, the failing gate or axis, and the builder mode for the retry.

**Boundaries.** Deterministic. It does not re-run the mesher, and it does not decide whether
another attempt is allowed. That is the routing function's decision, taken from the attempt
budget.

**Routing.** Always back to the builder. The retry loop is bounded; when attempts are exhausted
the reviewer's routing sends the run to END rather than looping.

## node_reviewer: looking at the mesh

**Responsibility.** Decide whether the mesh is fit for the declared purpose, from evidence.

**Owns.** The verdict, the per-axis findings, and the evidence ledger backing them. The reviewer
inspects rendered images of the actual mesh: it is a vision model, against criteria composed
from the engine and the purpose together.

**Boundaries.** It judges an already-validated mesh: the executor's gates run first, and a mesh
that failed them never reaches review. It cannot resolve its own artifact paths, the review
session resolves and confines every artifact and hands over the resolved set, so a path the fence
refused can never reach a loader.

**Failure separation.** What the review *decided* and what *happened to* the review are different
outcomes. A renderer that could not produce evidence is reported as missing evidence, never as a
quality rejection: the two must not collapse, or a broken renderer would look like a bad mesh.

**Budget.** `REVIEWER_MAX_ROUNDS` per invocation, capped by the run's remaining top-level deadline.

**Routing.** END on a pass, the classifier when a retry is warranted, or the failure handler.

## node_failure_handler: terminalizing truthfully

**Responsibility.** Give a run one final, truthful outcome.

**Owns.** The terminal status, the public failure category, and the terminal event written in the
same transaction as the result, so the durable record and the announcement cannot disagree.

**Boundaries.** It reports; it never retries and never rewrites what happened. Cancellation is not
a supported terminal state in the current product, a run that exhausts its top-level deadline is
reported as timed out, which is a real and reachable outcome.

## Shared agent machinery

All three model-calling agents run through one canonical loop
(`agents/loop/`), which owns:

- **Round budgets and accounting**: what a round is, when the budget is spent, and the tally an
  invocation reports.
- **Tool execution**: dispatch, result capture, and the fence that runs before any
  side-effecting tool.
- **Provider rounds**: the single path to a model call, so retries, capacity admission and
  telemetry are not reimplemented per agent.
- **Ownership checks**: a long-running agent stops when it no longer owns the run, before it can
  produce a side effect a newer generation would have to undo.

Because the loop is shared, an agent's own module contains only what is genuinely specific to that
role: its prompt, its tools, and its interpretation of the result.
