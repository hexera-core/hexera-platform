// Responsibility: Start `next dev` with the live-stream relay loaded, so a local console streams the
// same way a deployed one does - through its own origin.
// Boundaries: a launcher only. `next dev` serves from a child process it forks, and a preload given
// to this process would not reach it; NODE_OPTIONS does, because Next passes it on to that child.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const nextBin = require.resolve("next/dist/bin/next");
const preload = new URL("./preload.ts", import.meta.url).href;
const nodeOptions = [process.env.NODE_OPTIONS, `--import=${preload}`].filter(Boolean).join(" ");

const child = spawn(process.execPath, [nextBin, "dev", ...process.argv.slice(2)], {
  env: { ...process.env, NODE_OPTIONS: nodeOptions },
  stdio: "inherit",
});
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => child.kill(signal));
}
child.on("exit", (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
