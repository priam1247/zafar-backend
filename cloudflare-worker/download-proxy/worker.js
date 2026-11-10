/**
 * Zafar signed download proxy.
 * Set ALLOWED_ORIGINS to comma-separated exact frontend origins and DL_SECRET to the backend signing secret.
 * ALLOWED_ORIGIN remains supported as a legacy single-origin setting.
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
  const allowedOrigins = (env.ALLOWED_ORIGINS || env.ALLOWED_ORIGIN || "")
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean);
  const requestOrigin = request.headers.get("Origin");
  if (allowedOrigins.includes(requestOrigin)) {
    headers.set("Access-Control-Allow-Origin", requestOrigin);
    headers.set("Access-Control-Allow-Methods", "GET, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization, Range");
    headers.set("Access-Control-Max-Age", "86400");
  }
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

function noStoreError(message, status) {
  return new Response(message, { status, headers: { "Cache-Control": "no-store" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/dl\/([A-Za-z0-9-_]+)$/);
    if (!match) return withCors(noStoreError("Not found", 404), request, env);

    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }), request, env);
    }
    if (request.method !== "GET") {
      return withCors(noStoreError("Method not allowed", 405), request, env);
    }

    const fileId = match[1];
    const exp = url.searchParams.get("exp");
    const sig = url.searchParams.get("sig");
    const name = url.searchParams.get("name") || `${fileId}.pdf`;

    if (!exp || !sig) return withCors(noStoreError("Missing signature", 403), request, env);
    if (Date.now() / 1000 > Number(exp)) {
      return withCors(noStoreError("Link expired — request a fresh one", 403), request, env);
    }
    if (!(await verifySignature(env.DL_SECRET, fileId, exp, sig))) {
      return withCors(noStoreError("Invalid signature", 403), request, env);
    }

    const upstreamHeaders = new Headers();
    const range = request.headers.get("Range");
    if (range) upstreamHeaders.set("Range", range);
    // Deliberately do not set edge cache directives: Drive may return an HTML
    // virus-scan interstitial, which must never be cached as a PDF.
    const driveResp = await fetch(
      `https://drive.google.com/uc?export=download&id=${fileId}`,
      { headers: upstreamHeaders, redirect: "follow" }
    );
    const contentType = driveResp.headers.get("Content-Type") || "";
    if (/^text\/html(?:\s*;|$)/i.test(contentType)) {
      return withCors(noStoreError("Upstream returned a non-PDF page", 502), request, env);
    }
    if (!driveResp.ok && driveResp.status !== 206) {
      return withCors(noStoreError("Upstream error fetching file", 502), request, env);
    }

    const safeName = name.replace(/[^\w.\-()[\] ]/g, "_");
    const headers = new Headers(driveResp.headers);
    headers.set(
      "Content-Disposition",
      `attachment; filename="${safeName}"; filename*=UTF-8''${encodeURIComponent(name)}`
    );
    headers.set("X-Content-Type-Options", "nosniff");
    headers.set("Cache-Control", "no-store");
    headers.delete("set-cookie");
    return withCors(new Response(driveResp.body, { status: driveResp.status, headers }), request, env);
  },
};
