import { OAuth2Client } from "google-auth-library";

// WHO IS MAKING THIS CHANGE, established from the assertion IAP signs rather than from the header
// it sets alongside it.
//
// IAP sets two things on every request it admits: X-Goog-Authenticated-User-Email, which is plain
// text, and X-Goog-IAP-JWT-Assertion, which is signed. The plain header is trustworthy only while
// the service carries no allUsers invoker binding - create-admin-service.sh works hard to keep
// that true, and actively removes such a binding when it finds one. But an audit trail that
// attributes a VM deletion to a named person should not rest on a second script's invariant
// holding, so writes verify the signature and reads do not.
//
// THERE IS NO BYPASS. A deployment that cannot compute its audience cannot verify an actor, and
// this refuses rather than falling back to the header. That means writes stop when the console is
// misconfigured, which is the correct direction to fail: the alternative is accepting an
// unverified actor, which is the entire risk this exists to close.

const IAP_ISSUER = "https://cloud.google.com/iap";

export type IapActor = {
  email: string;
  subject: string;
};

// The verifier, narrowed so tests can supply one without a network round trip to Google's key set.
export type IapVerifier = Pick<
  OAuth2Client,
  "getIapPublicKeys" | "verifySignedJwtWithCertsAsync"
>;

let client: OAuth2Client | null = null;

export function getIapVerifier(): IapVerifier {
  client ??= new OAuth2Client();
  return client;
}

// IAP enabled DIRECTLY on a Cloud Run service signs for the service. The
// /projects/<n>/global/backendServices/<id> form belongs to the load-balancer arrangement, which
// this deployment deliberately does not use - see the admin console design, Decision 1.
export function iapAudience(args: {
  projectNumber: string | null;
  region: string;
  service: string | null;
}): string | null {
  if (!args.projectNumber || !args.service) return null;
  return `/projects/${args.projectNumber}/locations/${args.region}/services/${args.service}`;
}

export async function verifyIapActor(args: {
  assertion: string | null;
  audience: string | null;
  verifier: IapVerifier;
}): Promise<IapActor> {
  if (!args.assertion) {
    throw new Error(
      "This request carries no IAP assertion, so the person making it cannot be established. " +
        "Changes to infrastructure are refused rather than attributed to an unverified identity.",
    );
  }
  if (!args.audience) {
    throw new Error(
      "The IAP audience cannot be computed: this deployment is missing GCP_PROJECT_NUMBER or " +
        "CLOUDRUN_ADMIN_SERVICE. Without it the assertion cannot be verified, so writes are refused.",
    );
  }

  const keys = await args.verifier.getIapPublicKeys();
  const ticket = await args.verifier.verifySignedJwtWithCertsAsync(
    args.assertion,
    keys.pubkeys,
    args.audience,
    [IAP_ISSUER],
  );

  const payload = ticket.getPayload();
  const email = payload?.email;
  if (!email) {
    throw new Error("The IAP assertion verified but carries no email claim to attribute this to.");
  }

  return { email, subject: payload.sub ?? email };
}
