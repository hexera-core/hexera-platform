import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

const here = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  transpilePackages: ["@hexera/api-client"],
  // The console has two surfaces: /runs for the work and /settings/billing for the money. These
  // retired routes all showed a slice of one or the other; query strings ride along, so a saved
  // /usage?cursor= link still lands on the same ledger page. 307, not 308 - the map may move again.
  redirects() {
    return [
      { source: "/", destination: "/runs", permanent: false },
      { source: "/usage", destination: "/settings/billing", permanent: false },
      { source: "/conversations/:path*", destination: "/runs", permanent: false },
      { source: "/repair", destination: "/runs", permanent: false },
    ];
  },
  // The container copies one self-contained tree rather than installing at image build time.
  output: "standalone",
  // In a pnpm workspace, tracing must start at the repository root: the console imports
  // @hexera/api-client from packages/, and a trace rooted at apps/console silently omits it -
  // which fails at runtime, not at build time.
  outputFileTracingRoot: path.join(here, "..", ".."),
};

export default nextConfig;
