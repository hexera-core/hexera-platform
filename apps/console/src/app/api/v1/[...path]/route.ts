import { auth } from "@/auth";
import { ownerIdFromSession, proxyRefusal } from "@/lib/auth/session";
import { proxyProductApiRequest } from "@/lib/product-api/proxy";

type RouteContext = {
  params: Promise<{
    path: string[];
  }>;
};

export const runtime = "nodejs";

async function handle(request: Request, context: RouteContext) {
  const session = await auth();
  // DECISION 4, ENFORCED. The (dashboard) layout's emailVerified check decides what is rendered;
  // this one decides what may be CALLED. See proxyRefusal for why the render-time check alone is
  // not a boundary.
  const refusal = proxyRefusal(session);
  if (refusal) {
    return refusal;
  }
  const ownerId = ownerIdFromSession(session);
  const { path } = await context.params;
  return proxyProductApiRequest(request, path, ownerId);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
