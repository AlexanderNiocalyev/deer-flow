export const EMBED_TOKEN_QUERY_PARAM = "embed_token";
export const EMBED_AUTH_HEADER_NAME = "X-DeerFlow-Embed-Token";
export const ORPHEUS_SESSION_QUERY_PARAM = "orpheus_session_id";
export const ORPHEUS_WORKSPACE_QUERY_PARAM = "orpheus_workspace_id";

const STORAGE_KEY = "deerflow:embed-token";
const ORPHEUS_SESSION_STORAGE_KEY = "deerflow:orpheus-session-id";
const ORPHEUS_WORKSPACE_STORAGE_KEY = "deerflow:orpheus-workspace-id";

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

function readQueryParam(name: string): string | null {
  if (!isBrowser()) return null;
  const value = new URLSearchParams(window.location.search).get(name);
  const trimmed = value?.trim();
  if (!trimmed) return null;
  return trimmed;
}

function rememberOrpheusEmbedContext(): void {
  if (!isBrowser()) return;
  const sessionId = readQueryParam(ORPHEUS_SESSION_QUERY_PARAM);
  const workspaceId = readQueryParam(ORPHEUS_WORKSPACE_QUERY_PARAM);
  try {
    if (sessionId)
      window.sessionStorage.setItem(ORPHEUS_SESSION_STORAGE_KEY, sessionId);
    if (workspaceId)
      window.sessionStorage.setItem(ORPHEUS_WORKSPACE_STORAGE_KEY, workspaceId);
  } catch {
    // Non-fatal: callers can still read the values from the current URL.
  }
}

export function getEmbedToken(): string | null {
  if (!isBrowser()) return null;
  rememberOrpheusEmbedContext();
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

export function getOrpheusEmbedContext(): {
  orpheus_session_id: string;
  orpheus_workspace_id: string;
} | null {
  if (!isBrowser()) return null;
  rememberOrpheusEmbedContext();
  try {
    const sessionId =
      readQueryParam(ORPHEUS_SESSION_QUERY_PARAM) ??
      window.sessionStorage.getItem(ORPHEUS_SESSION_STORAGE_KEY);
    const workspaceId =
      readQueryParam(ORPHEUS_WORKSPACE_QUERY_PARAM) ??
      window.sessionStorage.getItem(ORPHEUS_WORKSPACE_STORAGE_KEY);
    if (!sessionId || !workspaceId) return null;
    return {
      orpheus_session_id: sessionId,
      orpheus_workspace_id: workspaceId,
    };
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
  rememberOrpheusEmbedContext();
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
