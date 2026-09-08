import { HEXERA_API_PREFIX } from "./routes";

export type HexeraApiPath = `${typeof HEXERA_API_PREFIX}${string}`;

export type HexeraApiClientOptions = {
  baseUrl?: string;
  defaultHeaders?: HeadersInit;
  fetchImpl?: typeof fetch;
};

export type HexeraApiClient = {
  request<T>(path: HexeraApiPath, init?: RequestInit): Promise<T>;
};

export class HexeraApiError extends Error {
  readonly body: string;
  readonly status: number;

  constructor(status: number, statusText: string, body: string) {
    super(`Hexera API request failed: ${status} ${statusText}`);
    this.body = body;
    this.name = "HexeraApiError";
    this.status = status;
  }
}

export function resolveHexeraApiUrl(path: HexeraApiPath, baseUrl = ""): string {
  if (!path.startsWith(HEXERA_API_PREFIX)) {
    throw new Error(`Route ${path} is outside the Hexera product API`);
  }

  const normalizedBaseUrl = baseUrl.trim().replace(/\/+$/, "");
  return `${normalizedBaseUrl}${path}`;
}

export function createHexeraApiClient({
  baseUrl = "",
  defaultHeaders,
  fetchImpl = fetch,
}: HexeraApiClientOptions = {}): HexeraApiClient {
  return {
    async request<T>(path: HexeraApiPath, init: RequestInit = {}): Promise<T> {
      const response = await fetchImpl(resolveHexeraApiUrl(path, baseUrl), {
        ...init,
        headers: mergeHeaders(defaultHeaders, init.headers),
      });

      if (!response.ok) {
        throw new HexeraApiError(
          response.status,
          response.statusText,
          await response.text(),
        );
      }

      if (response.status === 204) {
        return undefined as T;
      }

      const contentType = response.headers.get("content-type") ?? "";
      if (contentType.includes("application/json")) {
        return (await response.json()) as T;
      }

      return (await response.text()) as T;
    },
  };
}

function mergeHeaders(defaultHeaders?: HeadersInit, requestHeaders?: HeadersInit): Headers {
  const headers = new Headers(defaultHeaders);
  new Headers(requestHeaders).forEach((value, key) => {
    headers.set(key, value);
  });
  return headers;
}
