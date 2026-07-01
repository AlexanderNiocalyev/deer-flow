export const EMBED_TOKEN_QUERY_PARAM = "embed_token";
export const EMBED_AUTH_HEADER_NAME = "X-DeerFlow-Embed-Token";

const STORAGE_KEY = "deerflow:embed-token";

function isBrowser(): boolean {
  return typeof window !== "undefined";
}

function readTokenFromLocation(): string | null {
  if (!isBrowser()) return null;
  const token = new URLSearchParams(window.location.search).get(
    EMBED_TOKEN_QUERY_PARAM,
  );
  const trimmed = token?.trim();
  if (!trimmed) return null;
  return trimmed;
}

export function getEmbedToken(): string | null {
  if (!isBrowser()) return null;
  const urlToken = readTokenFromLocation();
  if (urlToken) {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, urlToken);
    } catch {
      // Ignore storage failures; the URL token is still usable this request.
    }
    return urlToken;
  }
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function withEmbedAuthHeader(headers: HeadersInit | undefined): Headers {
  const merged = new Headers(headers);
  const token = getEmbedToken();
  if (token && !merged.has(EMBED_AUTH_HEADER_NAME)) {
    merged.set(EMBED_AUTH_HEADER_NAME, token);
  }
  return merged;
}

export function consumeEmbedTokenFromUrl(): void {
  if (!isBrowser()) return;
  const url = new URL(window.location.href);
  const token = url.searchParams.get(EMBED_TOKEN_QUERY_PARAM)?.trim();
  if (!token) return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Non-fatal: API wrappers can still read the token before it is stripped.
  }
  url.searchParams.delete(EMBED_TOKEN_QUERY_PARAM);
  window.history.replaceState(window.history.state, "", url.toString());
}

export function hasEmbedToken(): boolean {
  return Boolean(getEmbedToken());
}
