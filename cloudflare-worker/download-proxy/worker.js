/**
 * Zafar download proxy.
 * Route: GET /dl/:fileId?exp=<unix>&sig=<b64url-hmac>&name=<filename>
 *
 * 1. Verifies HMAC-SHA256(DL_SECRET, `${fileId}.${exp}`) — the same message
 *    the backend signs in auth.py:make_signed_download_url.
 * 2. Authenticates to Google as the service account and streams the file
 *    from the Drive API with a correct Content-Disposition header.
 *
 * Streaming = no buffering; Worker memory stays flat regardless of file size.
 *
 * Secrets (see DEPLOYMENT.md step 4):
 *   npx wrangler secret put DL_SECRET                    # == backend DOWNLOAD_SIGNING_SECRET
 *   npx wrangler secret put GOOGLE_SERVICE_ACCOUNT_JSON  # == backend's same-named var
 *
 * WHY the Drive API and not `drive.google.com/uc?export=download`:
 * the backend reads Drive through a service account with drive.readonly,
 * so the material folders are shared *with that service account*, not
 * published to "anyone with the link". An unauthenticated uc?export
 * request therefore never returns the PDF — Google answers 200 with an
 * HTML sign-in/permission page (or, for big files, a virus-scan
 * interstitial), so users got a broken file or an error page. Signing
 * requests as the service account is the only thing that actually works,
 * and it keeps the material private.
 */

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";

// ---------------------------------------------------------------------------
// base64 helpers
// ---------------------------------------------------------------------------

function b64urlToBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  // atob throws on any non-base64 character; a tampered or truncated ?sig=
  // must become a 403, not an uncaught exception (Cloudflare Error 1101).
  let bin;
  try {
    bin = atob(s);
  } catch {
    return null;
  }
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function bytesToB64url(bytes) {
  let bin = "";
  for (const b of new Uint8Array(bytes)) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ---------------------------------------------------------------------------
// Signed-link verification
// ---------------------------------------------------------------------------

async function verifySignature(secret, fileId, exp, sigB64) {
  const sigBytes = b64urlToBytes(sigB64);
  // HMAC-SHA256 is always 32 bytes; bail before importKey on junk input.
  if (!sigBytes || sigBytes.length !== 32) return false;

  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false,
    ["verify"]
  );
  // crypto.subtle.verify is constant-time — resistant to timing attacks.
  return crypto.subtle.verify(
    "HMAC", key, sigBytes,
    new TextEncoder().encode(`${fileId}.${exp}`)
  );
}

// ---------------------------------------------------------------------------
// Google service-account auth (JWT-bearer flow, RS256 via WebCrypto)
// ---------------------------------------------------------------------------

// Access tokens live 1h. Cache in module scope so warm isolates reuse one
// token across many downloads instead of doing a token round-trip per file.
let tokenCache = { token: null, expiresAt: 0 };

function pemToPkcs8(pem) {
  const body = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s+/g, "");
  const bin = atob(body);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function getAccessToken(serviceAccountJson) {
  const now = Math.floor(Date.now() / 1000);
  // 60s of slack so a token can't expire mid-flight on a slow download start.
  if (tokenCache.token && now < tokenCache.expiresAt - 60) return tokenCache.token;

  let sa;
  try {
    sa = JSON.parse(serviceAccountJson);
  } catch (err) {
    throw new WorkerError(503, "Download proxy is misconfigured",
      `GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: ${err.message}`);
  }
  if (!sa.client_email || !sa.private_key) {
    throw new WorkerError(503, "Download proxy is misconfigured",
      "GOOGLE_SERVICE_ACCOUNT_JSON is missing client_email or private_key");
  }

  const header = { alg: "RS256", typ: "JWT" };
  const claims = {
    iss: sa.client_email,
    scope: DRIVE_SCOPE,
    aud: sa.token_uri || TOKEN_URL,
    iat: now,
    exp: now + 3600,
  };
  const enc = new TextEncoder();
  const unsigned =
    bytesToB64url(enc.encode(JSON.stringify(header))) + "." +
    bytesToB64url(enc.encode(JSON.stringify(claims)));

  // \n arrives escaped when the key is pasted into an env var / secret.
  const pem = sa.private_key.replace(/\\n/g, "\n");
  const key = await crypto.subtle.importKey(
    "pkcs8", pemToPkcs8(pem),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc.encode(unsigned));
  const assertion = `${unsigned}.${bytesToB64url(sig)}`;

  const resp = await fetch(sa.token_uri || TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion,
    }),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok || !data?.access_token) {
    throw new WorkerError(502, "Could not authenticate with Google Drive",
      `token endpoint ${resp.status}: ${data ? JSON.stringify(data) : "<non-JSON body>"}`);
  }

  tokenCache = {
    token: data.access_token,
    expiresAt: now + (Number(data.expires_in) || 3600),
  };
  return tokenCache.token;
}

// ---------------------------------------------------------------------------

/** An error carrying the status + user-facing text we want to return. */
class WorkerError extends Error {
  constructor(status, publicMessage, logMessage) {
    super(logMessage || publicMessage);
    this.status = status;
    this.publicMessage = publicMessage;
  }
}

function text(body, status) {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
  });
}

export default {
  async fetch(request, env) {
    try {
      return await handle(request, env);
    } catch (err) {
      // Nothing may escape this handler: an uncaught throw makes Cloudflare
      // serve its own "Error 1101 — Worker threw exception" page, which the
      // browser displays in place of the download. Log the detail, return
      // a plain response. Watch it live with: npx wrangler tail zafar-dl
      if (err instanceof WorkerError) {
        console.error(`download-proxy ${err.status}: ${err.message}`);
        return text(err.publicMessage, err.status);
      }
      console.error("download-proxy unhandled:", err?.stack || err?.message || String(err));
      return text("Download failed — please try again", 500);
    }
  },
};

async function handle(request, env) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return text("Method not allowed", 405);
  }

  const url = new URL(request.url);
  const match = url.pathname.match(/^\/dl\/([A-Za-z0-9-_]+)$/);
  if (!match) return text("Not found", 404);

  // An unset/empty secret makes crypto.subtle.importKey throw, so check it
  // up front: a misconfigured deploy should be an obvious 503.
  if (!env.DL_SECRET) {
    throw new WorkerError(503, "Download proxy is misconfigured",
      "DL_SECRET is not set — run: npx wrangler secret put DL_SECRET");
  }
  if (!env.GOOGLE_SERVICE_ACCOUNT_JSON) {
    throw new WorkerError(503, "Download proxy is misconfigured",
      "GOOGLE_SERVICE_ACCOUNT_JSON is not set — run: npx wrangler secret put GOOGLE_SERVICE_ACCOUNT_JSON");
  }

  const fileId = match[1];
  const exp = url.searchParams.get("exp");
  const sig = url.searchParams.get("sig");
  const name = url.searchParams.get("name") || `${fileId}.pdf`;

  if (!exp || !sig) return text("Missing signature", 403);
  // Guard Number(exp): a non-numeric exp yields NaN, and `NaN > x` is false,
  // which would have let an unparseable expiry sail past the expiry check.
  if (!/^\d{1,15}$/.test(exp)) return text("Invalid signature", 403);
  if (Date.now() / 1000 > Number(exp)) {
    return text("This download link has expired — go back and tap download again.", 403);
  }
  if (!(await verifySignature(env.DL_SECRET, fileId, exp, sig))) {
    return text("Invalid signature", 403);
  }

  // Serve popular papers from Cloudflare's edge cache. The cache key is the
  // file id ALONE — deliberately not the request URL, because every signed
  // URL is unique (fresh exp+sig each time) so a URL-keyed cache would never
  // hit. Access is already gated by the HMAC check above, which has passed.
  const cache = caches.default;
  const cacheKey = new Request(`https://zafar-dl.internal/file/${fileId}`, { method: "GET" });
  const range = request.headers.get("Range");

  if (!range) {
    const cached = await cache.match(cacheKey);
    if (cached) return withDownloadHeaders(cached, name, request.method === "HEAD");
  }

  const token = await getAccessToken(env.GOOGLE_SERVICE_ACCOUNT_JSON);
  const driveUrl =
    `https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}` +
    `?alt=media&acknowledgeAbuse=true&supportsAllDrives=true`;

  const upstreamHeaders = { Authorization: `Bearer ${token}` };
  // Forward Range so mobile browsers and download managers can resume.
  if (range) upstreamHeaders.Range = range;

  const driveResp = await fetch(driveUrl, { headers: upstreamHeaders });

  if (driveResp.status === 401 || driveResp.status === 403) {
    // Token invalid, or the folder isn't shared with the service account.
    tokenCache = { token: null, expiresAt: 0 }; // don't reuse a rejected token
    const detail = await driveResp.text().catch(() => "");
    throw new WorkerError(502, "This file isn't available right now",
      `Drive denied access to ${fileId} (${driveResp.status}): ${detail.slice(0, 300)}`);
  }
  if (driveResp.status === 404) {
    throw new WorkerError(404, "That file no longer exists on Drive",
      `Drive 404 for ${fileId}`);
  }
  if (!driveResp.ok && driveResp.status !== 206) {
    const detail = await driveResp.text().catch(() => "");
    throw new WorkerError(502, "Couldn't fetch the file — please try again",
      `Drive ${driveResp.status} for ${fileId}: ${detail.slice(0, 300)}`);
  }

  let response = driveResp;
  if (!range && driveResp.status === 200) {
    // Cache a clone; the original stream still goes to the user untouched.
    const cacheable = new Response(driveResp.body, driveResp);
    cacheable.headers.set("Cache-Control", "public, max-age=86400");
    cacheable.headers.delete("set-cookie");
    response = cacheable.clone();
    // waitUntil isn't available here, but cache.put consumes its own clone
    // and Cloudflare keeps the Worker alive for the pending write.
    cache.put(cacheKey, cacheable).catch((err) =>
      console.error("cache.put failed (non-fatal):", err?.message)
    );
  }

  return withDownloadHeaders(response, name, request.method === "HEAD");
}

/**
 * Re-headers an upstream response so the browser saves it as a PDF with the
 * right filename, without leaking Google's own hop-by-hop headers.
 */
function withDownloadHeaders(upstream, name, headOnly) {
  // Strip quotes/control chars so the filename can't break out of the header.
  const safeName = name.replace(/[^\w.\-()[\] ]/g, "_");

  const headers = new Headers(upstream.headers);
  headers.set(
    "Content-Disposition",
    `attachment; filename="${safeName}"; filename*=UTF-8''${encodeURIComponent(name)}`
  );
  headers.set("Content-Type", "application/pdf");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Accept-Ranges", "bytes");
  headers.delete("set-cookie");
  // fetch() already decompressed the body, so passing Google's
  // content-encoding (and its compressed content-length) through would
  // make the browser try to gunzip plain bytes — a corrupt, unopenable PDF.
  headers.delete("content-encoding");
  headers.delete("content-length");
  headers.delete("transfer-encoding");

  return new Response(headOnly ? null : upstream.body, {
    status: upstream.status,
    headers,
  });
}
