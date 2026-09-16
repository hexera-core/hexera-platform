import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Member = { email: string; name: string; role: string };

type OrganizationView = {
  organization: { id: string; name: string; slug: string } | null;
  members: Member[];
};

export default async function OrganizationPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const view = await consoleFetch<OrganizationView>("organization", ownerId);

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Organisation</h1>
      </div>

      {view === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : (
        <>
          <div className="card">
            <p className="label">Name</p>
            {/* The route degrades to an owner-derived view rather than erroring when the
                organisation cannot be resolved, so a null here is a degraded read, not an
                account without a tenant. */}
            <p>{view.organization?.name ?? "Not resolved"}</p>
            <p className="label" style={{ marginTop: ".8rem" }}>
              Slug
            </p>
            <p>
              <code>{view.organization?.slug ?? "—"}</code>
            </p>
          </div>

          <p className="page__sub">Inviting people is not available yet.</p>

          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {view.members.map((member) => (
                <tr key={member.email}>
                  <td>{member.name}</td>
                  <td>{member.email}</td>
                  <td>
                    <span className="label">{member.role}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
