import { proxyProductApiRequest } from "@/lib/product-api/proxy";

//: How long a dashboard page waits for the product API before rendering the degraded row. A
//: page that hangs is worse than a page that says it could not load: the render is a server
//: render, so the whole route stalls behind it.
const TIMEOUT_MS = 3_000;

/** The parsed body, or null for every failure.
 *
 * BOTH the catch and the timeout matter, and neither alone is enough -- the same reasoning the
 * old console page's creditBalance() carried. fetch() REJECTS rather than resolving with a
 * non-ok Response on a connection failure (refused, DNS), and an uncaught rejection propagates
 * out of a server component and takes the entire page down rather than one panel. A
 * slow-but-not-failing API is just as damaging without a bound on time.
 */
export async function readJson<T>(response: Promise<Response>): Promise<T | null> {
  try {
    const resolved = await response;
    if (!resolved.ok) {
      return null;
    }
    return (await resolved.json()) as T;
  } catch {
    return null;
  }
}

/** Read a product API path as the signed-in owner. Fails soft, always.
 *
 * `path` may carry a query string ("simulation?limit=5"). THE TWO HALVES TRAVEL SEPARATELY:
 * proxy.ts builds its target from `routePath.map(encodeURIComponent).join("/")` and then copies
 * the search from the REQUEST's own url. Passing "simulation?limit=5" as one segment would
 * percent-encode the "?" into the path and the query would silently vanish.
 */
export function splitPath(path: string): { segments: string[]; search: string } {
  const [pathname, search = ""] = path.replace(/^\/+/, "").split("?");
  return { segments: pathname.split("/").filter(Boolean), search };
}

export async function consoleFetch<T>(path: string, ownerId: string): Promise<T | null> {
  const { segments, search } = splitPath(path);
  const url = `http://console.internal/api/v1/${segments.join("/")}${search ? `?${search}` : ""}`;
  return readJson<T>(
    Promise.race([
      proxyProductApiRequest(new Request(url), segments, ownerId),
      rejectOnAbort(AbortSignal.timeout(TIMEOUT_MS)),
    ]),
  );
}

function rejectOnAbort(signal: AbortSignal): Promise<never> {
  return new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason ?? new Error("timed out")));
  });
}
