import { ADMIN_SECTIONS } from "./sections";

export function AdminNav({ current }: { current: string }) {
  return (
    <nav aria-label="Admin sections" className="admin-nav">
      <ul>
        {ADMIN_SECTIONS.map((section) => (
          <li key={section.href}>
            {section.available ? (
              <a aria-current={section.href === current ? "page" : undefined} href={section.href}>
                {section.label}
              </a>
            ) : (
              <span className="admin-nav-unavailable" title="Not available yet">
                {section.label}
              </span>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
