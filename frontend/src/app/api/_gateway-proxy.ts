const DEFAULT_GATEWAY_BASE_URL = "http://127.0.0.1:8001";

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "content-length",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function getGatewayBaseURL() {
  const raw =
    process.env.DEER_FLOW_INTERNAL_GATEWAY_BASE_URL ??
    process.env.NEXT_PUBLIC_BACKEND_BASE_URL ??
    DEFAULT_GATEWAY_BASE_URL;
  const trimmed = raw.trim();
  return (trimmed.length > 0 ? trimmed : DEFAULT_GATEWAY_BASE_URL).replace(
    /\/+$/,
    "",
  );
}

function normalizeGatewayPath(pathname: string) {
  return pathname.startsWith("/") ? pathname : `/${pathname}`;
}

export function buildGatewayURL(requestURL: string, gatewayPathname: string) {
  const incoming = new URL(requestURL);
  const target = new URL(normalizeGatewayPath(gatewayPathname), getGatewayBaseURL());
  target.search = incoming.search;
  return target;
}

function copyProxyHeaders(headers: Headers) {
  const copied = new Headers(headers);
  for (const header of HOP_BY_HOP_HEADERS) {
    copied.delete(header);
  }
  copied.delete("host");
  return copied;
}

export async function proxyGatewayRequest(
  request: Request,
  gatewayPathname: string,
) {
  const hasBody = !["GET", "HEAD"].includes(request.method);
  const response = await fetch(buildGatewayURL(request.url, gatewayPathname), {
    method: request.method,
    headers: copyProxyHeaders(request.headers),
    body: hasBody ? await request.arrayBuffer() : undefined,
    redirect: "manual",
  });

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: copyProxyHeaders(response.headers),
  });
}
