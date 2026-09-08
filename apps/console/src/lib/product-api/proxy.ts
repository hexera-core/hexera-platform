import { createHmac } from "node:crypto";

type BuildProductApiRequestOptions = {
  baseUrl: string;
  ownerId: string | null;
  request: Request;
  routePath: string[];
  userTokenSecret?: string;
};

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "content-length",
  "cookie",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

export async function buildProductApiRequest({
  baseUrl,
  ownerId,
  request,
  routePath,
  userTokenSecret,
}: BuildProductApiRequestOptions): Promise<Request> {
  if (!ownerId) {
    throw new Error("Authentication required");
  }

  const sourceUrl = new URL(request.url);
  const targetUrl = new URL(`/api/v1/${routePath.map(encodeURIComponent).join("/")}`, normalizedBaseUrl(baseUrl));
  targetUrl.search = sourceUrl.search;

  const headers = forwardedHeaders(request.headers);
  headers.set("x-user-id", ownerId);

  if (userTokenSecret) {
    headers.set("x-user-sig", signOwnerId(ownerId, userTokenSecret));
  }

  if (process.env.MESH_API_KEY) {
    headers.set("x-api-key", process.env.MESH_API_KEY);
  }

  return new Request(targetUrl, {
    body: await requestBody(request),
    headers,
    method: request.method,
  });
}

export async function proxyProductApiRequest(
  request: Request,
  routePath: string[],
  ownerId: string | null,
): Promise<Response> {
  if (!ownerId) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const baseUrl = process.env.HEXERA_API_BASE_URL;
  if (!baseUrl) {
    return Response.json(
      { error: "HEXERA_API_BASE_URL is not configured" },
      { status: 503 },
    );
  }

  const productRequest = await buildProductApiRequest({
    baseUrl,
    ownerId,
    request,
    routePath,
    userTokenSecret: process.env.USER_TOKEN_SECRET,
  });

  return fetch(productRequest);
}

function forwardedHeaders(source: Headers): Headers {
  const headers = new Headers();
  source.forEach((value, key) => {
    if (!HOP_BY_HOP_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });
  return headers;
}

function normalizedBaseUrl(baseUrl: string): string {
  return baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
}

function requestBody(request: Request): Promise<ArrayBuffer | null> {
  if (request.method === "GET" || request.method === "HEAD") {
    return Promise.resolve(null);
  }

  return request.arrayBuffer();
}

function signOwnerId(ownerId: string, secret: string): string {
  return createHmac("sha256", secret).update(ownerId).digest("hex");
}
