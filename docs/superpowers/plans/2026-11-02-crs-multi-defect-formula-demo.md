# CRS Multi-Defect Formula Demo Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce a deployed-service recording of one recognizable Formula car with the maximum independently proven, repairable defects, compared against the identical unrepaired CAD under the same mesh settings.

**Architecture:** Build a deterministic STEP fixture by modifying localized exterior regions of the existing Formula body. Qualify each induced defect alone through inspect, repair, re-inspect, and mesh before combining them. The final recording uses only service-generated evidence and labels every defect with its actual CRS code, severity, operation, and verdict.

**Tech Stack:** Python, OpenCASCADE/OCP, pytest native tier, Hexera API/workers, Playwright, existing console and PDF workflow.

---

### Task 1: Establish the defect matrix

- [ ] Inventory current operations and native fixtures for `open_shell`, `self_intersection`, `wire_gap`, `curve_inconsistency`, `degenerate_edge`, and `small_face`.
- [ ] Record which defects remain detectable after STEP serialization and which operations produce a real kept verdict without mocks.
- [ ] Exclude any defect whose only successful test relies on monkeypatched kernel analysis.
- [ ] Define the maximum honest combined set, including slivers as mesh-blocking warnings rather than relabeling their severity.

### Task 2: Build one deterministic Formula fixture

**Files:**
- Create: `tools/demo/build_crs_formula_fixture.py`
- Test: `tests/native/test_native_crs_formula_fixture.py`

- [ ] Write failing native tests that require a recognizable car-scale STEP and the selected localized defects after round trip.
- [ ] Implement the fixture builder using the existing Formula source and deterministic face selection by geometry, not transient indices.
- [ ] Emit a manifest with defect type, intended location, and geometric measurement for video overlays.
- [ ] Verify each defect independently before enabling the combined output.
- [ ] Commit the fixture builder and tests.

### Task 3: Prove repair and control outcomes locally

**Files:**
- Modify: `tests/native/test_native_crs_formula_fixture.py`

- [ ] Add a failing end-to-end assertion for kept operations, removed error defects, and bounded geometry changes.
- [ ] Run CRS on the combined STEP and re-inspect the output.
- [ ] Mesh the unrepaired and repaired STEP with identical engine/settings.
- [ ] Require the control to fail or show a measured, material degradation; do not use a merely cosmetic difference.
- [ ] Require the repaired path to promote repaired CAD and produce a valid mesh with preserved planar/sharp features.
- [ ] Commit only changes needed for the proven path.

### Task 4: Run the real deployed service

- [ ] Run `make lint`, `make typecheck`, `make test-fast`, `make check-fast`, and focused native tests.
- [ ] Deploy only changed tiers to personal slug `pranav`.
- [ ] Upload the same combined STEP twice: CRS disabled control and CRS enabled repair.
- [ ] Capture API/job evidence for inspection defects, operation verdicts, promotion, and mesh result.
- [ ] Reject the take if deployed evidence differs from local qualification.

### Task 5: Record the client demo and update the paper

**Files:**
- Modify: `docs/evidence/crs-automotive-component/`
- Modify: `docs/whitepapers/hexera-crs-technical-overview.*`
- Create: `output/video/crs-formula-control-vs-repair.mp4`
- Create: `output/pdf/hexera-crs-technical-overview.pdf`

- [ ] Record the actual deployed console with Playwright from upload through mesh result.
- [ ] Show the recognizable full car, then zoom to every induced defect and identify its CRS code and severity.
- [ ] Compare identical mesh settings side by side and state the measured control failure/degradation.
- [ ] Show each kept repair and the final repaired-CAD promotion without exposing reproducible implementation detail.
- [ ] Update the white paper with only verified defect coverage and this run's measured evidence.
- [ ] Open and visually inspect the final MP4 and PDF before delivery.

### Stop Conditions

- [ ] Do not claim a self-intersection repair unless the real analyzer finds it and the real operation keeps it after STEP round trip.
- [ ] Do not proceed to recording unless the control is demonstrably worse under identical settings.
- [ ] Do not alter production deployment behavior solely to make the demo pass.
