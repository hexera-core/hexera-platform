// The shared frame for every block on an admin page: a titled section, the empty state that says
// why a block has nothing to show, and a row of labelled figures.
//
// EMPTY IS A SENTENCE, NOT A BLANK. A chart with no series must say which metric it looked for,
// because "we ran zero workers" and "this metric is not published" look identical when both
// render as a flat line at zero.

export function Panel({
  action,
  children,
  heading,
  title,
}: {
  action?: React.ReactNode;
  children: React.ReactNode;
  heading?: string;
  title: string;
}) {
  return (
    <section className="admin-panel">
      <header className="admin-panel-head">
        <h2 className="admin-panel-title">{title}</h2>
        {heading ? <p className="admin-panel-heading">{heading}</p> : null}
        {action}
      </header>
      {children}
    </section>
  );
}

export function EmptyState({ note }: { note: string }) {
  return <p className="admin-empty">{note}</p>;
}

export function Alert({ children, tone = "warn" }: { children: React.ReactNode; tone?: "info" | "warn" }) {
  return <p className={tone === "warn" ? "admin-alert" : "admin-notice"}>{children}</p>;
}

export function StatRow({ stats }: { stats: readonly { label: string; value: string }[] }) {
  return (
    <dl className="admin-stats">
      {stats.map((stat) => (
        <div className="admin-stat" key={stat.label}>
          <dt>{stat.label}</dt>
          <dd>{stat.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function FieldList({ fields }: { fields: readonly { label: string; value: string }[] }) {
  return (
    <dl className="admin-fields">
      {fields.map((field) => (
        <div className="admin-field" key={field.label}>
          <dt>{field.label}</dt>
          <dd>{field.value}</dd>
        </div>
      ))}
    </dl>
  );
}
