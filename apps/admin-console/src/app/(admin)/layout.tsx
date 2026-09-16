import { AdminNav } from "@/app/_components/nav";

// Every admin page renders inside this. There is no authentication here on purpose: IAP decides
// who reaches this container at all, and everyone it admits is an admin. See
// docs/deployment/admin-console-access.md.
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <p className="admin-brand">Hexera Admin</p>
        <AdminNav />
      </aside>
      <main className="admin-main">{children}</main>
    </div>
  );
}
