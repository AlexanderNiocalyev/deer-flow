import { afterEach, beforeEach, describe, expect, test, rs } from "@rstest/core";

import { buildGatewayURL, proxyGatewayRequest } from "@/app/api/_gateway-proxy";

const ENV_KEYS = [
  "DEER_FLOW_INTERNAL_GATEWAY_BASE_URL",
  "NEXT_PUBLIC_BACKEND_BASE_URL",
] as const;

type EnvSnapshot = Partial<
  Record<(typeof ENV_KEYS)[number], string | undefined>
>;

function snapshotEnv(): EnvSnapshot {
  const snapshot: EnvSnapshot = {};
  for (const key of ENV_KEYS) {
    snapshot[key] = process.env[key];
  }
  return snapshot;
}

function setEnv(key: (typeof ENV_KEYS)[number], value: string | undefined) {
  const env = process.env as Record<string, string | undefined>;
  if (value === undefined) {
    delete env[key];
  } else {
    env[key] = value;
  }
}

function restoreEnv(snapshot: EnvSnapshot) {
  for (const key of ENV_KEYS) {
    setEnv(key, snapshot[key]);
  }
}

describe("gateway proxy", () => {
  let saved: EnvSnapshot;

  beforeEach(() => {
    saved = snapshotEnv();
    setEnv("DEER_FLOW_INTERNAL_GATEWAY_BASE_URL", undefined);
    setEnv("NEXT_PUBLIC_BACKEND_BASE_URL", undefined);
  });

  afterEach(() => {
    restoreEnv(saved);
    rs.unstubAllGlobals();
  });

  test("uses runtime internal gateway URL and preserves the query string", () => {
    setEnv("DEER_FLOW_INTERNAL_GATEWAY_BASE_URL", "https://gw.example.com/");

    const url = buildGatewayURL(
      "https://frontend.example.com/api/langgraph/threads/t1?embed=1",
      "/api/threads/t1",
    );

    expect(url.toString()).toBe("https://gw.example.com/api/threads/t1?embed=1");
  });

  test("falls back to NEXT_PUBLIC_BACKEND_BASE_URL for direct deployments", () => {
    setEnv("NEXT_PUBLIC_BACKEND_BASE_URL", "https://public-gw.example.com");

    const url = buildGatewayURL(
      "https://frontend.example.com/api/models",
      "/api/models",
    );

    expect(url.toString()).toBe("https://public-gw.example.com/api/models");
  });

  test("proxies method, body, and non-hop-by-hop headers", async () => {
    setEnv("DEER_FLOW_INTERNAL_GATEWAY_BASE_URL", "https://gw.example.com");
    const fetchMock = rs.fn(async () => {
      return new Response(JSON.stringify({ ok: true }), {
        status: 201,
        headers: {
          "content-type": "application/json",
          connection: "close",
        },
      });
    });
    rs.stubGlobal("fetch", fetchMock);

    const response = await proxyGatewayRequest(
      new Request("https://frontend.example.com/api/langgraph/runs?stream=1", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-deerflow-embed-token": "token",
          connection: "close",
        },
        body: JSON.stringify({ input: "hello" }),
      }),
      "/api/runs",
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls.at(0) as
      | [URL, RequestInit & { headers: Headers }]
      | undefined;
    expect(call).toBeDefined();
    const [url, init] = call!;
    expect(url.toString()).toBe("https://gw.example.com/api/runs?stream=1");
    expect(init.method).toBe("POST");
    expect(init.headers.get("x-deerflow-embed-token")).toBe("token");
    expect(init.headers.has("connection")).toBe(false);
    expect(await new Response(init.body).text()).toBe('{"input":"hello"}');
    expect(response.status).toBe(201);
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(response.headers.has("connection")).toBe(false);
  });
});
