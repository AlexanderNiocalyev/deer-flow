import { afterEach, beforeEach, expect, test } from "@rstest/core";

import {
  EMBED_AUTH_HEADER_NAME,
  consumeEmbedTokenFromUrl,
  getEmbedToken,
  withEmbedAuthHeader,
} from "@/core/embed-auth";

type FakeWindow = {
  location: URL;
  history: {
    state: unknown;
    replaceState: (
      state: unknown,
      title: string,
      url?: string | URL | null,
    ) => void;
  };
  sessionStorage: {
    clear: () => void;
    getItem: (key: string) => string | null;
    setItem: (key: string, value: string) => void;
  };
};

function installFakeWindow() {
  const storage = new Map<string, string>();
  const fakeWindow = {
    location: new URL("https://deerflow.example/"),
    history: {
      state: null,
      replaceState(state: unknown, _title: string, url?: string | URL | null) {
        this.state = state;
        if (url) {
          fakeWindow.location = new URL(url, fakeWindow.location.origin);
        }
      },
    },
    sessionStorage: {
      clear: () => storage.clear(),
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
    },
  } satisfies FakeWindow;

  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: fakeWindow,
  });
}

beforeEach(() => {
  installFakeWindow();
});

afterEach(() => {
  Reflect.deleteProperty(globalThis, "window");
});

test("getEmbedToken reads URL token and stores it for later requests", () => {
  window.history.replaceState(
    null,
    "",
    "/embed/chats/thread-1?embed=1&embed_token=signed-token",
  );

  expect(getEmbedToken()).toBe("signed-token");

  window.history.replaceState(null, "", "/embed/chats/thread-1?embed=1");

  expect(getEmbedToken()).toBe("signed-token");
});

test("withEmbedAuthHeader injects the embed auth header", () => {
  window.history.replaceState(
    null,
    "",
    "/embed/chats/thread-1?embed_token=signed-token",
  );

  const headers = withEmbedAuthHeader({ Accept: "application/json" });

  expect(headers.get("Accept")).toBe("application/json");
  expect(headers.get(EMBED_AUTH_HEADER_NAME)).toBe("signed-token");
});

test("consumeEmbedTokenFromUrl stores token and removes it from the address bar", () => {
  window.history.replaceState(
    null,
    "",
    "/embed/chats/thread-1?embed=1&embed_token=signed-token&x=1",
  );

  consumeEmbedTokenFromUrl();

  expect(window.location.search).toBe("?embed=1&x=1");
  expect(getEmbedToken()).toBe("signed-token");
});
