import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";
import { proxyProductApiRequest } from "@/lib/product-api/proxy";

type RouteContext = {
  params: Promise<{
    path: string[];
  }>;
};

export const runtime = "nodejs";

async function handle(request: Request, context: RouteContext) {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  const { path } = await context.params;
  return proxyProductApiRequest(request, path, ownerId);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
