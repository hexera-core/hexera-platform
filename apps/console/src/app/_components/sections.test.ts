import assert from "node:assert/strict";
import test from "node:test";

import { CONSOLE_SECTIONS, currentSection, visibleSections } from "./sections";

test("the CAD repair proof of concept stays out of the sidebar unless switched on", () => {
  const hidden = visibleSections(false).map((section) => section.href);
  assert.ok(!hidden.includes("/repair"));
  const shown = visibleSections(true).map((section) => section.href);
  assert.ok(shown.includes("/repair"));
});

test("visibleSections hides nothing else, in either state", () => {
  const others = CONSOLE_SECTIONS.filter((section) => section.href !== "/repair");
  for (const repairPoc of [false, true]) {
    const hrefs = visibleSections(repairPoc).map((section) => section.href);
    for (const section of others) {
      assert.ok(hrefs.includes(section.href), `${section.href} missing at repairPoc=${repairPoc}`);
    }
  }
});

test("the repair route resolves as its own section, not Overview", () => {
  assert.equal(currentSection("/repair"), "/repair");
  assert.equal(currentSection("/"), "/");
});
