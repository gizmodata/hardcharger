// The API page's rules, kept apart from Swagger UI so they can be tested on their own:
// where the OpenAPI document comes from, which requests get the signed-in user's token, and
// which answers are offered as a download instead of being shown as text.
//
// The token goes only to this page's own origin. Swagger UI is never given it (no
// "Authorize" dialog, nothing persisted): the request interceptor adds it to each request
// it sends, at the moment it sends it, and only when the request's URL is on this origin.

/** The server's discovery document, as far as this page reads it (spec §3.1, §3.3). */
export interface Discovery {
  capabilities?: string[];
  links?: unknown;
  [key: string]: unknown;
}

/** Where a server serves its OpenAPI document when it doesn't say (spec §3.3). */
export function defaultOpenApiUrl(origin: string): string {
  return `${origin}/warp/v1/openapi.json`;
}

/**
 * Where this server's OpenAPI document is: undefined when discovery doesn't list the
 * `openapi` capability, or names a place on another origin (the page fetches from its own
 * origin only).
 */
export function openApiUrl(discovery: Discovery, origin: string): string | undefined {
  if (!discovery.capabilities?.includes("openapi")) return undefined;
  const links = discovery.links;
  const named = typeof links === "object" && links !== null ? (links as Record<string, unknown>)["openapi"] : undefined;
  if (typeof named !== "string" || named === "") return defaultOpenApiUrl(origin);
  return isSameOrigin(named, origin) ? new URL(named, origin).href : undefined;
}

/** An OpenAPI document, as far as this page looks into it. */
export interface OpenApiDocument {
  openapi: string;
  info?: { title?: string; version?: string };
  servers?: unknown;
  paths?: Record<string, Record<string, unknown> | undefined>;
  [key: string]: unknown;
}

/** The server doesn't publish an OpenAPI document (or not one this page can read). */
export class NoOpenApiError extends Error {
  override readonly name = "NoOpenApiError";
}

/** Fetches this server's OpenAPI document; a server that has none is a NoOpenApiError. */
export async function loadOpenApi(
  discovery: Discovery,
  origin: string,
  signal?: AbortSignal,
): Promise<OpenApiDocument> {
  const url = openApiUrl(discovery, origin);
  if (url === undefined) throw new NoOpenApiError("its discovery document (GET /warp) doesn't list one");
  const resp = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal: signal ?? null });
  if (resp.status === 404) throw new NoOpenApiError(`${new URL(url).pathname} answered 404`);
  if (!resp.ok) throw new Error(`${new URL(url).pathname} answered ${resp.status}`);
  let doc: unknown;
  try {
    doc = await resp.json();
  } catch {
    throw new NoOpenApiError(`${new URL(url).pathname} is not JSON`);
  }
  if (typeof doc !== "object" || doc === null || typeof (doc as { openapi?: unknown }).openapi !== "string") {
    throw new NoOpenApiError(`${new URL(url).pathname} is not an OpenAPI document`);
  }
  return doc as OpenApiDocument;
}

const methods = ["get", "put", "post", "delete", "options", "head", "patch", "trace"];

/**
 * The document as the page shows it: every request goes to this page's own origin. The
 * document's `servers` (anywhere: top level, path items, operations) are replaced by this
 * origin, so Swagger UI has nothing to switch to and builds every URL here.
 */
export function sameOriginSpec(doc: OpenApiDocument, origin: string): OpenApiDocument {
  const copy = structuredClone(doc);
  copy.servers = [{ url: origin, description: "This server" }];
  for (const item of Object.values(copy.paths ?? {})) {
    if (!item) continue;
    delete item["servers"];
    for (const m of methods) {
      const op = item[m];
      if (op && typeof op === "object") delete (op as Record<string, unknown>)["servers"];
    }
  }
  return copy;
}

/** Whether `url` (absolute, or relative to the page) is on `origin`. */
export function isSameOrigin(url: string, origin: string): boolean {
  try {
    const u = new URL(url, origin);
    return (u.protocol === "http:" || u.protocol === "https:") && u.origin === origin;
  } catch {
    return false;
  }
}

/** A request as Swagger UI hands it to its requestInterceptor. */
export interface SwaggerRequest {
  url: string;
  headers?: Record<string, string>;
  [key: string]: unknown;
}

function withoutAuthorization(headers: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(headers).filter(([k]) => k.toLowerCase() !== "authorization"));
}

/**
 * Swagger UI's requestInterceptor: a request to `origin` carries `Authorization: Bearer
 * <token>` (the token as it is when the request is sent); a request anywhere else carries no
 * Authorization header at all.
 */
export function authorizeRequests(
  token: () => string | undefined,
  origin: string,
): (request: SwaggerRequest) => SwaggerRequest {
  return (request) => {
    const headers = withoutAuthorization(request.headers ?? {});
    const t = token();
    if (t && isSameOrigin(request.url, origin)) headers["Authorization"] = `Bearer ${t}`;
    request.headers = headers;
    return request;
  };
}

/** A response as Swagger UI hands it to its responseInterceptor. */
export interface SwaggerResponse {
  headers?: Record<string, string | undefined>;
  [key: string]: unknown;
}

// Answers Swagger UI shows as text (or as an image or audio player); anything else is bytes.
const shownTypes = /json|xml|yaml|^text\/|javascript|^image\/|^audio\//i;

function header(headers: Record<string, string | undefined>, name: string): string | undefined {
  const k = Object.keys(headers).find((h) => h.toLowerCase() === name);
  return k === undefined ? undefined : headers[k];
}

/** The file name a binary answer of `contentType` is downloaded as. */
export function downloadName(contentType: string): string {
  const type = contentType.split(";")[0]!.trim().toLowerCase();
  if (type === "application/vnd.apache.arrow.stream") return "result.arrows";
  if (type === "application/vnd.apache.arrow.file") return "result.arrow";
  if (type === "application/vnd.apache.parquet") return "result.parquet";
  return "response.bin";
}

/**
 * Swagger UI's responseInterceptor: a binary answer (Arrow IPC above all) is marked as an
 * attachment, so Swagger UI offers "Download file" instead of printing its bytes as text.
 */
export function downloadBinaryResponses(response: SwaggerResponse): SwaggerResponse {
  const headers = response.headers ?? {};
  const type = header(headers, "content-type");
  if (!type || shownTypes.test(type) || header(headers, "content-disposition") !== undefined) return response;
  response.headers = { ...headers, "content-disposition": `attachment; filename="${downloadName(type)}"` };
  return response;
}
