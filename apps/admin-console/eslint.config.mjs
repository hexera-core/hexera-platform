import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const eslintConfig = [
  // The outreach sender's bundle is generated output, not source. Linting it reports findings
  // about code nobody wrote and nobody can fix in place.
  { ignores: ["outreach-worker.js", "import-outreach.js"] },
  ...nextVitals,
  ...nextTypescript,
];

export default eslintConfig;
