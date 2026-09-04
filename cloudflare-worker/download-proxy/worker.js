/**
 * Zafar download proxy.
 * Route: GET /dl/:fileId?exp=<unix>&sig=<b64url-hmac>&name=<filename>
 * Verifies HMAC-SHA256(DL_SECRET, `${fileId}.${exp}`), then streams the
 * file from Google Drive with a correct Content-Disposition header.
 * Streaming = no buffering; Worker memory stays flat regardless of file size.
 *
 * Set the secret (same value as the backend's DOWNLOAD_SIGNING_SECRET):
 *   npx wrangler secret put DL_SECRET
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
  // crypto.subtle.verify is constant-time — resistant to timing attacks.
  return crypto.subtle.verify(
    "HMAC", key, b64urlToBytes(sigB64),
    new TextEncoder().encode(`${fileId}.${exp}`)
  );
}

const ALLOWED_ORIGIN = "https://library.zafarh.dpdns.org";

function withCors(response) {
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
  return new Response(response.body, { status: response.status, headers });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/dl\/([A-Za-z0-9-_]+)$/);
    if (!match) return new Response("Not found", { status: 404 });

    const fileId = match[1];
    const exp = url.searchParams.get("exp");
    const sig = url.searchParams.get("sig");
    const name = url.searchParams.get("name") || `${fileId}.pdf`;

    if (!exp || !sig) return withCors(new Response("Missing signature", { status: 403 }));
    if (Date.now() / 1000 > Number(exp))
      return withCors(new Response("Link expired — request a fresh one", { status: 403 }));
    if (!(await verifySignature(env.DL_SECRET, fileId, exp, sig)))
      return withCors(new Response("Invalid signature", { status: 403 }));

    // cf.cacheEverything lets Cloudflare's edge cache the file body, so
    // repeat downloads of popular papers never even hit Drive again.
    const driveResp = await fetch(
      `https://drive.google.com/uc?export=download&id=${fileId}`,
      { cf: { cacheEverything: true, cacheTtl: 86400 }, redirect: "follow" }
    );
    if (!driveResp.ok)
      return withCors(new Response("Upstream error fetching file", { status: 502 }));

    // Sanitize filename for the header (strip quotes/control chars).
    const safeName = name.replace(/[^\w.\-()[\] ]/g, "_");

    const headers = new Headers(driveResp.headers);
    headers.set(
      "Content-Disposition",
      `attachment; filename="${safeName}"; filename*=UTF-8''${encodeURIComponent(name)}`
    );
    headers.set("X-Content-Type-Options", "nosniff");
    headers.set("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
    headers.delete("set-cookie");

    // Pass the body stream straight through — zero buffering.
    return new Response(driveResp.body, { status: 200, headers });
  },
};
