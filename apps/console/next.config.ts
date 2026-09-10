import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

const here = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  transpilePackages: ["@hexera/api-client"],
  // The container copies one self-contained tree rather than installing at image build time.
  output: "standalone",
  // In a pnpm workspace, tracing must start at the repository root: the console imports
  // @hexera/api-client from packages/, and a trace rooted at apps/console silently omits it -
  // which fails at runtime, not at build time.
  outputFileTracingRoot: path.join(here, "..", ".."),
};

export default nextConfig;
