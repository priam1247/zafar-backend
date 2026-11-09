/**
 * Zafar signed download proxy.
 * Set ALLOWED_ORIGIN to the exact frontend origin, e.g.
 * https://library.zafarh.dpdns.org, and DL_SECRET to the backend signing secret.
 */
function b64urlToBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function verifySignature(secret, fileId, exp, sigB64) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false,
    ["verify"]
  );
  return crypto.subtle.verify(
    "HMAC", key, b64urlToBytes(sigB64),
    new TextEncoder().encode(`${fileId}.${exp}`)
  );
}

function withCors(response, request, env) {
  const headers = new Headers(response.headers);
  headers.append("Vary", "Origin");
  if (request.headers.get("Origin") === env.ALLOWED_ORIGIN) {
    headers.set("Access-Control-Allow-Origin", env.ALLOWED_ORIGIN);
    headers.set("Access-Control-Allow-Methods", "GET, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization");
    headers.set("Access-Control-Max-Age", "86400");
  }
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/dl\/([A-Za-z0-9-_]+)$/);
    if (!match) return withCors(new Response("Not found", { status: 404 }), request, env);

    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }), request, env);
    }
    if (request.method !== "GET") {
      return withCors(new Response("Method not allowed", { status: 405 }), request, env);
    }

    const fileId = match[1];
    const exp = url.searchParams.get("exp");
    const sig = url.searchParams.get("sig");
    const name = url.searchParams.get("name") || `${fileId}.pdf`;

    if (!exp || !sig) return withCors(new Response("Missing signature", { status: 403 }), request, env);
    if (Date.now() / 1000 > Number(exp)) {
      return withCors(new Response("Link expired — request a fresh one", { status: 403 }), request, env);
    }
    if (!(await verifySignature(env.DL_SECRET, fileId, exp, sig))) {
      return withCors(new Response("Invalid signature", { status: 403 }), request, env);
    }

    const driveResp = await fetch(
      `https://drive.google.com/uc?export=download&id=${fileId}`,
      { cf: { cacheEverything: true, cacheTtl: 86400 }, redirect: "follow" }
    );
    if (!driveResp.ok) {
      return withCors(new Response("Upstream error fetching file", { status: 502 }), request, env);
    }

    const safeName = name.replace(/[^\w.\-()[\] ]/g, "_");
    const headers = new Headers(driveResp.headers);
    headers.set(
      "Content-Disposition",
      `attachment; filename="${safeName}"; filename*=UTF-8''${encodeURIComponent(name)}`
    );
    headers.set("X-Content-Type-Options", "nosniff");
    headers.delete("set-cookie");
    return withCors(new Response(driveResp.body, { status: 200, headers }), request, env);
  },
};
