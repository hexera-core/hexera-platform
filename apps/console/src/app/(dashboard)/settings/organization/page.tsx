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
        <div>
          <p className="page__eyebrow">Workspace</p>
          <h1 className="page__title">Organisation</h1>
          <p className="page__intro">
            The tenant boundary for members, billing, credits and submitted mesh studies.
          </p>
        </div>
      </div>

      {view === null ? (
        <p className="empty card">Could not reach the API just now. Reload to try again.</p>
      ) : (
        <>
          <div className="summary-grid">
            <div className="metric-card">
              <p className="label">Name</p>
              {/* The route degrades to an owner-derived view rather than erroring when the
                organisation cannot be resolved, so a null here is a degraded read, not an
                account without a tenant. */}
              <b>{view.organization?.name ?? "Not resolved"}</b>
              <span>Resolved from the current console session.</span>
            </div>
            <div className="metric-card">
              <p className="label">Slug</p>
              <b style={{ fontSize: "1rem" }}>
                <code>{view.organization?.slug ?? "--"}</code>
              </b>
              <span>Used as the stable workspace handle.</span>
            </div>
            <div className="metric-card">
              <p className="label">Members</p>
              <b>{view.members.length.toLocaleString()}</b>
              <span>Inviting people is not available yet.</span>
            </div>
          </div>

          <section className="panel">
            <div className="panel__head">
              <div>
                <p className="label">Members</p>
                <p className="page__sub">People currently attached to this organisation.</p>
              </div>
            </div>
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
          </section>
        </>
      )}
    </div>
  );
}
