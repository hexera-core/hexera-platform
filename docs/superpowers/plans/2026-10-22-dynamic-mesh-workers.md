# Dynamic Mesh Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pure, shadow-mode policy for evaluating preflight mesh sizing signals.

**Architecture:** A provider-free application policy accepts immutable workload evidence and returns an explainable, versioned profile recommendation. `contracts.mesh_machine` remains the sole runtime routing and escalation policy; no executor composition or dispatch behavior changes.

**Tech Stack:** Python 3.11, dataclasses, enums, pytest

**Spec:** `docs/superpowers/specs/2026-10-22-dynamic-mesh-workers-design.md`

## Global Constraints

- Worker sizing remains internal.
- Runtime dispatch does not change in this slice.
- The shadow recommendation never changes requested mesh fidelity.
- Runtime escalation remains owned by `contracts.mesh_machine`.
- No new dependency is introduced.

## Review Focus

- A signal exactly on a profile ceiling remains in that profile.
- One oversized signal cannot be hidden by several small signals.
- Missing measurements remain routable and explain why the default was chosen.
- Negative measured values are rejected instead of producing a smaller worker.

---

### Task 1: Pure execution-profile policy

**Files:**
- Create: `src/meshpipeline/application/mesh_execution_profiles.py`
- Create: `tests/unit/application/test_mesh_execution_profiles.py`

**Interfaces:**
- Consumes: primitive workload measurements available before native execution.
- Produces: `MeshWorkload`, `MeshExecutionProfile`, `ProfileDecision`, and `select_profile(workload)`.

- [x] **Step 1: Write failing selection tests**

  Cover the review-focus cases plus standard, high-memory, and ultramem recommendations.

- [x] **Step 2: Run the focused tests and verify RED**

  Run: `.venv/bin/python -m pytest tests/unit/application/test_mesh_execution_profiles.py -q`

  Expected: collection fails because `meshpipeline.application.mesh_execution_profiles` does not exist.

- [x] **Step 3: Implement the minimal pure policy**

  Use ordered per-signal ceilings, explicit engine and boundary-layer pressure, and stable reason
  strings. Keep provider selection, escalation, and persistence out of this module.

- [x] **Step 4: Run the focused tests and verify GREEN**

  Run: `.venv/bin/python -m pytest tests/unit/application/test_mesh_execution_profiles.py -q`

  Expected: all tests pass.

- [x] **Step 5: Run host gates**

  Run: `make lint`, `make typecheck`, and `make check-fast`.

  Expected: all gates pass. Run `make test-fast` and report any host-tooling or platform-dependent
  failures separately.
