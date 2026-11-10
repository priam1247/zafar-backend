import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";
import { test } from "node:test";
import assert from "node:assert/strict";

// Exercise the actual module with the Worker runtime globals/fetch stubbed.
if (!globalThis.crypto) Object.defineProperty(globalThis, "crypto", { value: webcrypto });
const source = await readFile(new URL("./worker.js", import.meta.url), "utf8");
const workerModule = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const worker = workerModule.default;
const testOrigin = "https://library.example.test";
const env = {
  ALLOWED_ORIGINS: testOrigin,
  DL_SECRET: "unit-test-signing-secret",
};

async function signedRequest(headers = {}) {
  const fileId = "drive-file-123";
  const exp = String(Math.floor(Date.now() / 1000) + 60);
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(env.DL_SECRET),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const signature = await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`${fileId}.${exp}`)
  );
  const sig = Buffer.from(signature).toString("base64url");
  return new Request(
    `https://worker.example.test/dl/${fileId}?exp=${exp}&sig=${sig}&name=paper.pdf`,
    { headers: { Origin: testOrigin, ...headers } }
  );
}

test("only the new production domain receives CORS permission", async () => {
  const workerEnv = { ...env, ALLOWED_ORIGINS: "https://studymw.netlibrary.dpdns.org" };
  const newDomainRequest = new Request("https://worker.example.test/missing", {
    headers: { Origin: "https://studymw.netlibrary.dpdns.org" },
  });
  const allowed = await worker.fetch(newDomainRequest, workerEnv);
  assert.equal(allowed.status, 404);
  assert.equal(allowed.headers.get("Access-Control-Allow-Origin"), "https://studymw.netlibrary.dpdns.org");

  const oldDomainRequest = new Request("https://worker.example.test/missing", {
    headers: { Origin: "https://library.zafarh.dpdns.org" },
  });
  const denied = await worker.fetch(oldDomainRequest, workerEnv);
  assert.equal(denied.headers.get("Access-Control-Allow-Origin"), null);
});

test("HTML Drive interstitial returns an uncached 502", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("<html>scan page</html>", {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
  try {
    const response = await worker.fetch(await signedRequest(), env);
    assert.equal(response.status, 502);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    assert.equal(await response.text(), "Upstream returned a non-PDF page");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Range is forwarded and 206 response headers/body pass through", async () => {
  const originalFetch = globalThis.fetch;
  let upstreamRange;
  globalThis.fetch = async (_url, options) => {
    upstreamRange = options.headers.get("Range");
    return new Response("partial bytes", {
      status: 206,
      headers: {
        "Content-Type": "application/pdf",
        "Content-Range": "bytes 0-12/100",
        "Accept-Ranges": "bytes",
      },
    });
  };
  try {
    const response = await worker.fetch(await signedRequest({ Range: "bytes=0-12" }), env);
    assert.equal(upstreamRange, "bytes=0-12");
    assert.equal(response.status, 206);
    assert.equal(response.headers.get("Content-Range"), "bytes 0-12/100");
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    assert.equal(await response.text(), "partial bytes");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
