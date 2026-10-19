import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

// A "use server" MODULE MAY EXPORT ONLY ASYNC FUNCTIONS. Next refuses to build one that exports a
// value - an object, a constant - and the client component importing it is where the build dies.
// Types are erased, so `export type` is fine; anything else that is not `export async function`
// is the build failure, found here in a second instead of by `next build`.

function serverModules(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...serverModules(path));
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".test.ts")) {
      if (/^\s*["']use server["']/.test(readFileSync(path, "utf8"))) out.push(path);
    }
  }
  return out;
}

test("every 'use server' module exports only async functions", () => {
  const modules = serverModules(join(process.cwd(), "src"));
  assert.ok(modules.length > 0, "no 'use server' modules found - is the scan rooted correctly?");
  const offenders: string[] = [];
  for (const path of modules) {
    for (const line of readFileSync(path, "utf8").split("\n")) {
      if (!/^export\s/.test(line)) continue;
      if (/^export\s+(type|interface)\s/.test(line)) continue;
      if (/^export\s+(default\s+)?async\s+function\s/.test(line)) continue;
      offenders.push(`${path}: ${line.trim()}`);
    }
  }
  assert.deepEqual(offenders, []);
});
