import { readFile } from "node:fs/promises";
import { test } from "node:test";
import assert from "node:assert/strict";

const source = await readFile(new URL("./api-proxy.js", import.meta.url), "utf8");
const workerModule = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const worker = workerModule.default;

test("API proxy forwards only required request headers and removes cookies", async () => {
  const originalFetch = globalThis.fetch;
  let upstreamUrl;
  let upstreamOptions;
  globalThis.fetch = async (url, options) => {
    upstreamUrl = String(url);
    upstreamOptions = options;
    return new Response("ok", {
      status: 200,
      headers: { "Content-Type": "application/json", "Set-Cookie": "session=secret" },
    });
  };

  try {
    const request = new Request("https://api.example.test/auth/login?next=%2Fdrive", {
      method: "POST",
      headers: {
        Accept: "application/json",
        Authorization: "Bearer test-token",
        "Content-Type": "application/json",
        Origin: "https://studymw.netlibrary.dpdns.org",
        Cookie: "session=browser-cookie",
        "X-Forwarded-For": "203.0.113.99",
        "X-Forwarded-Host": "attacker.example",
      },
      body: JSON.stringify({ email: "student@gmail.com" }),
    });

    const response = await worker.fetch(request);
    const headers = new Headers(upstreamOptions.headers);
    assert.equal(upstreamUrl, "https://bloody-brigitta-ahzafar-de9b1709.koyeb.app/auth/login?next=%2Fdrive");
    assert.equal(upstreamOptions.method, "POST");
    assert.equal(headers.get("Authorization"), "Bearer test-token");
    assert.equal(headers.get("Origin"), "https://studymw.netlibrary.dpdns.org");
    assert.equal(headers.get("Cookie"), null);
    assert.equal(headers.get("X-Forwarded-For"), null);
    assert.equal(headers.get("X-Forwarded-Host"), null);
    assert.equal(response.headers.get("Set-Cookie"), null);
    assert.equal(await response.text(), "ok");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
