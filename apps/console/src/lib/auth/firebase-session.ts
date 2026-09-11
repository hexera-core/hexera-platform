type ConsoleAuthEnv = Record<string, string | undefined>;

export type ConsoleUser = {
  id: string;
  email: string;
  name: string;
  organizationId: string;
  emailVerified: boolean;
};

/** Thrown, not returned, because it is the one refusal the sign-up page must explain rather
 *  than reporting as bad credentials. Every other failure is an indistinguishable null. */
export class SignupDisabled extends Error {
  constructor() {
    super("SignupDisabled");
    this.name = "SignupDisabled";
  }
}

export async function authorizeFirebaseSession(
  credentials: Partial<Record<"idToken" | "organizationName", unknown>>,
  env: ConsoleAuthEnv = process.env,
): Promise<ConsoleUser | null> {
  const idToken = credentials.idToken;
  if (typeof idToken !== "string" || !idToken) {
    return null;
  }
  const organizationName =
    typeof credentials.organizationName === "string" ? credentials.organizationName : "";

  const baseUrl = env.HEXERA_API_BASE_URL;
  if (!baseUrl) {
    // Refuse rather than guessing an origin. A console pointed at the wrong API would sign
    // people in against a database that is not this deployment's.
    return null;
  }

  const headers = new Headers({ "content-type": "application/json" });
  if (env.MESH_API_KEY) {
    headers.set("x-api-key", env.MESH_API_KEY);
  }

  const response = await fetch(new URL("/auth/session", normalizedBaseUrl(baseUrl)), {
    method: "POST",
    headers,
    body: JSON.stringify({ id_token: idToken, organization_name: organizationName }),
  });

  if (response.status === 403) {
    throw new SignupDisabled();
  }

  if (!response.ok) {
    // One null for every other cause, matching the API's own single refusal.
    return null;
  }

  const payload = (await response.json()) as Record<string, unknown>;
  const ownerId = typeof payload.owner_id === "string" ? payload.owner_id : "";
  if (!ownerId) {
    return null;
  }

  return {
    // `id` IS the owner_id the proxy signs into every API call. Keeping them the same value is
    // what lets ownerIdFromSession and proxy.ts stay exactly as they are.
    id: ownerId,
    email: ownerId,
    name: typeof payload.name === "string" && payload.name ? payload.name : ownerId,
    organizationId:
      typeof payload.organization_id === "string" ? payload.organization_id : "",
    emailVerified: payload.email_verified === true,
  };
}

function normalizedBaseUrl(baseUrl: string): string {
  return baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
}
