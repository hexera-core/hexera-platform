import assert from "node:assert/strict";
import test from "node:test";

import { emptyWords, fileRows, isLive, runIdFrom, sizeWords } from "./run-files";

test("only what the user takes away is listed, the whole bundle first", () => {
  const rows = fileRows([
    { id: "v", artifact_type: "viewer_data", label: "viewer", size_bytes: 10 },
    { id: "m", artifact_type: "mesh", label: "Surface preview (.msh)", size_bytes: 161_500_000 },
    { id: "b", artifact_type: "mesh_bundle", label: "Everything: .inp deck + solver files", size_bytes: 196_800_000 },
    { id: "x", artifact_type: "video", size_bytes: 5 },
    { id: "n", artifact_type: "something_new", size_bytes: 5 },
    { id: "r", artifact_type: "repair_report", label: "", size_bytes: 78_000 },
  ]);
  assert.deepEqual(rows.map((r) => r.id), ["b", "m", "r"]);
  assert.equal(rows[0].main, true);
  assert.equal(rows[2].label, "CAD inspection report");
  assert.equal(rows[2].size, "76.2 KB");
  assert.deepEqual(fileRows(undefined), []);
});

test("sizes read at a glance", () => {
  assert.equal(sizeWords(0), "");
  assert.equal(sizeWords(900), "900 B");
  assert.equal(sizeWords(5 * 1024 * 1024), "5.0 MB");
  assert.equal(sizeWords(212 * 1024 * 1024), "212 MB");
  assert.equal(sizeWords(3 * 1024 * 1024 * 1024), "3.0 GB");
});

test("the run is read from the path, never from /runs/new", () => {
  assert.equal(runIdFrom("/runs/8f14e45f-ceea"), "8f14e45f-ceea");
  assert.equal(runIdFrom("/runs/new"), null);
  assert.equal(runIdFrom("/runs"), null);
  assert.equal(runIdFrom("/settings/billing"), null);
});

test("an empty panel says why", () => {
  assert.equal(emptyWords(null, undefined), "Files show up here once a run starts.");
  assert.equal(emptyWords("a", "running"), "Files show up here as Hexera writes them.");
  assert.equal(emptyWords("a", "failed"), "This run did not deliver any files.");
  assert.equal(emptyWords("a", "succeeded"), "No files for this run yet.");
  assert.equal(isLive("queued"), true);
  assert.equal(isLive("succeeded"), false);
});
