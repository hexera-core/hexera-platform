// Responsibility: Build the inline script that hands main.js its session facts - the API origin
// the WebSocket dials, the routing mode, the job to boot and the owner id - as source text that is
// safe to embed in an HTML <script> element.
// Boundaries: pure. It renders nothing and reads no environment; the caller supplies every value.

/** A JSON literal that is safe INSIDE a <script>. `JSON.stringify` alone is not: a `<` in any
 *  value lets "</script>" end the element early, and the job id here comes straight from the URL.
 *  U+2028/U+2029 are legal in JSON strings and were illegal in JavaScript source before ES2019. */
const ESCAPED = new Map<string, string>([
  ["<", "\\u003c"],
  [String.fromCharCode(0x2028), "\\u2028"],
  [String.fromCharCode(0x2029), "\\u2029"],
]);

function jsLiteral(value: unknown): string {
  return Array.from(JSON.stringify(value), (ch) => ESCAPED.get(ch) ?? ch).join("");
}

export function sessionScript({
  bootJobId,
  ownerId,
  publicApiBaseUrl,
}: {
  bootJobId: string | null;
  ownerId: string;
  publicApiBaseUrl: string;
}): string {
  return [
    `globalThis.__HEXERA_API_WS_BASE_URL__ = ${jsLiteral(publicApiBaseUrl)};`,
    "globalThis.__HEXERA_ROUTED__ = true;",
    `globalThis.__HEXERA_BOOT_JOB__ = ${jsLiteral(bootJobId)};`,
    `try { localStorage.setItem("mg_uid", ${jsLiteral(ownerId)}); } catch {}`,
  ].join("\n");
}
