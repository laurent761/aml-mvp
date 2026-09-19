const REQUEST_HEADER_ALLOWLIST = ["accept", "content-type", "idempotency-key", "x-aml-api-version"] as const;
const RESPONSE_HEADER_ALLOWLIST = [
  "cache-control",
  "content-disposition",
  "content-length",
  "content-type",
  "etag",
  "x-artifact-sha256",
  "x-content-sha256",
] as const;

export function isBackendPath(pathname: string): boolean {
  return pathname === "/healthz" || pathname === "/readyz" || pathname.startsWith("/v1/");
}

export function resolveBackendApiUrl(
  workerBinding: string | undefined,
  processEnvironment: string | undefined,
): string | undefined {
  return workerBinding?.trim() ? workerBinding : processEnvironment;
}

function errorResponse(status: number, detail: string): Response {
  return Response.json({ detail }, { status });
}

export async function proxyBackendRequest(
  request: Request,
  backendApiUrl: string | undefined,
  upstreamFetch: typeof fetch = fetch,
): Promise<Response> {
  const configuredOrigin = backendApiUrl?.trim();
  if (!configuredOrigin) {
    return errorResponse(503, "BACKEND_API_URL is not configured for this UI deployment");
  }

  let backendOrigin: URL;
  try {
    backendOrigin = new URL(configuredOrigin);
  } catch {
    return errorResponse(503, "BACKEND_API_URL is invalid");
  }
  if (!(["http:", "https:"] as string[]).includes(backendOrigin.protocol)) {
    return errorResponse(503, "BACKEND_API_URL must use HTTP or HTTPS");
  }

  const incomingUrl = new URL(request.url);
  const backendUrl = new URL(`${incomingUrl.pathname}${incomingUrl.search}`, backendOrigin);
  const headers = new Headers();
  for (const name of REQUEST_HEADER_ALLOWLIST) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  headers.set("x-forwarded-host", incomingUrl.host);
  headers.set("x-forwarded-proto", incomingUrl.protocol.replace(":", ""));

  const hasBody = !["GET", "HEAD"].includes(request.method.toUpperCase());
  let upstream: Response;
  try {
    const body = hasBody ? await request.arrayBuffer() : undefined;
    upstream = await upstreamFetch(
      new Request(backendUrl, {
        method: request.method,
        headers,
        body,
        redirect: "manual",
      }),
    );
  } catch {
    return errorResponse(502, "The configured backend could not be reached");
  }

  if (upstream.status >= 300 && upstream.status < 400) {
    return errorResponse(502, "Backend redirects are not followed by the UI proxy");
  }

  const responseHeaders = new Headers();
  for (const name of RESPONSE_HEADER_ALLOWLIST) {
    const value = upstream.headers.get(name);
    if (value !== null) responseHeaders.set(name, value);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}
