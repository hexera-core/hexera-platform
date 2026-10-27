import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dashboardCss = readFileSync(
  new URL("../../../public/static/css/dashboard.css", import.meta.url),
  "utf8",
);

test("dashboard shell keeps the enterprise console theme restrained", () => {
  assert.equal(
    /\bradial-gradient\(/.test(dashboardCss),
    false,
    "dashboard background should not use decorative radial glows",
  );
  assert.equal(
    /\bmix-blend-mode\s*:/.test(dashboardCss),
    false,
    "dashboard background should not depend on blended texture effects",
  );
});
