# Mesh Profile Shadow Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit internal profile recommendations and basic actual execution measurements from the unchanged Cloud Run path.

**Architecture:** A focused application observer derives bounded workload evidence from the declared native payload and wraps one provider call. `CloudRunMeshExecutor.run` invokes the observer, while submission claims, collection, dispatch, and provider arguments remain unchanged.

**Tech Stack:** Python 3.11, pathlib, JSON, logging, pytest

**Spec:** `docs/superpowers/specs/2026-10-22-dynamic-mesh-workers-design.md`

## Global Constraints

- Cloud Run remains the only runtime executor.
- Observation must fail open and must not alter provider results or exceptions.
- Only declared payload files may be inspected; no recursive workspace scan.
- Input JSON reads are bounded to 2 MiB per file.
- No database, schema, setting, dependency, or customer-facing change.

## Review Focus

- Missing or malformed payload evidence cannot block a mesh submission.
- Binary STL triangle counts are accepted only when the file length matches its header.
- Nested JSON evidence chooses the largest relevant numeric value without treating booleans as integers.
- The log preserves policy version, reasons, recommendation, and actual result measurements.
- A provider exception is re-raised unchanged after observation.

---

### Task 1: Shadow workload observation

**Files:**
- Create: `src/meshpipeline/application/mesh_execution_shadow.py`
- Create: `tests/unit/application/test_mesh_execution_shadow.py`
- Modify: `src/meshpipeline/adapters/mesh_execution/cloud_run_client.py`

**Interfaces:**
- Consumes: `submission_payload_files(workspace)`, `MeshWorkload`, `select_profile`, and a zero-argument provider call.
- Produces: `workload_from_workspace(workspace, engine)` and `run_with_shadow_profile(workspace, engine, run)`.

- [x] **Step 1: Write failing evidence, logging, failure-isolation, and Cloud Run path tests**
- [x] **Step 2: Run the focused tests and verify RED because the observer module is absent**
- [x] **Step 3: Implement bounded evidence extraction and fail-open structured observation**
- [x] **Step 4: Call the observer from `CloudRunMeshExecutor.run` without changing arguments or collection**
- [x] **Step 5: Run focused tests, `make lint`, `make typecheck`, and `make check-fast`**
