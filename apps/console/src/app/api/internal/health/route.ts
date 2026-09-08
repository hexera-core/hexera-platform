import { HEXERA_API_PREFIX } from "@hexera/api-client";

export const runtime = "nodejs";

export async function GET() {
  return Response.json({
    nextApi: "/api/internal",
    productApi: HEXERA_API_PREFIX,
    service: "console",
    status: "ok",
  });
}
