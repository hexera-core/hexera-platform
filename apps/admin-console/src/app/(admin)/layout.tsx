import { AdminNav } from "@/app/_components/nav";
import { buildAdminSections } from "@/app/_components/sections";

// Every admin page renders inside this. There is no authentication here on purpose: IAP decides
// who reaches this container at all, and everyone it admits is an admin. See
// docs/deployment/admin-console-access.md.
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const sections = buildAdminSections(process.env);

  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <p className="admin-brand">Hexera Admin</p>
        <AdminNav sections={sections} />
      </aside>
      <main className="admin-main">{children}</main>
    </div>
  );
}
