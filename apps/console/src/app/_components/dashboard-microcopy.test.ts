import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function dashboardSource(relativePath: string): string {
  return readFileSync(new URL(`../(dashboard)/${relativePath}`, import.meta.url), "utf8");
}

test("the runs list keeps the pitch and leaves the money to billing", () => {
  const source = dashboardSource("runs/page.tsx");

  assert.match(source, /Upload a part, answer a few questions/);
  assert.doesNotMatch(source, /metric-card/);
});

test("API key summary omits the self-evident account scope card", () => {
  const source = dashboardSource("settings/api-keys/page.tsx");

  assert.match(source, /className="summary-grid summary-grid--compact"/);
  assert.doesNotMatch(source, /<p className="label">Scope<\/p>/);
});

test("billing summary omits payment-provider implementation detail", () => {
  const source = dashboardSource("settings/billing/page.tsx");

  assert.doesNotMatch(source, /<p className="label">Payment system<\/p>/);
});

test("billing keeps the charging consequence where the ledger now lives", () => {
  const source = dashboardSource("settings/billing/page.tsx");

  assert.match(source, /Credits are charged only for completed runs\./);
  assert.match(source, /credits\/history/);
});

test("organisation names the workspace boundary", () => {
  const source = dashboardSource("settings/organization/page.tsx");

  assert.match(source, /Members, billing, credits and runs belong to this workspace\./);
});
