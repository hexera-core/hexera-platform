// Responsibility: Install the live-stream relay before Next's server starts.
// Boundaries: a side effect only - loaded with `node --import` (the Dockerfile, and dev.ts for `pnpm dev`).
import { installStreamRelay } from "./stream-relay.ts";

installStreamRelay();
