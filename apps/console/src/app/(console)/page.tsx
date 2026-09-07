import { HEXERA_API_PREFIX, hexeraApiRoutes } from "@hexera/api-client";

const boundaryRows = [
  ["Next app", "/"],
  ["Next API", "/api/internal/*"],
  ["Product API", HEXERA_API_PREFIX],
  ["First backend call", hexeraApiRoutes.clientConfig],
] as const;

export default function ConsolePage() {
  return (
    <main className="app-shell">
      <section className="hero">
        <p className="eyebrow">console.hexera.ai</p>
        <h1>Hexera Console</h1>
        <p className="lede">Simulation jobs, files and review artifacts.</p>
      </section>

      <section className="panel" aria-labelledby="boundaries-title">
        <h2 id="boundaries-title">Boundaries</h2>
        <dl className="boundary-list">
          {boundaryRows.map(([label, value]) => (
            <div className="boundary-row" key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </section>
    </main>
  );
}
