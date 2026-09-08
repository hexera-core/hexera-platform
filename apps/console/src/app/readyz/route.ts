import { auth } from "@/auth";

export const runtime = "nodejs";

export async function GET() {
  const session = await auth();
  if (!session) {
    return Response.json({ status: "unauthorized" }, { status: 401 });
  }

  if (!process.env.HEXERA_API_BASE_URL) {
    return Response.json({ status: "unconfigured", checks: {} }, { status: 503 });
  }

  const response = await fetch(new URL("/readyz", process.env.HEXERA_API_BASE_URL), {
    headers: process.env.MESH_API_KEY ? { "x-api-key": process.env.MESH_API_KEY } : {},
  });

  return new Response(response.body, {
    headers: response.headers,
    status: response.status,
    statusText: response.statusText,
  });
}
