/**
 * Zafar signed download proxy for Google Drive and Drime Cloud.
 *
 * Required Worker variables:
 *   DL_SECRET       Same secret used by the backend to sign download URLs.
 *   ALLOWED_ORIGINS Exact HTTPS origins; production uses only
 *                   https://studymw.netlibrary.dpdns.org.
 *   DRIME_API_KEY   Drime API key (only needed for Drime-sourced files).
 *
 * Never configure cacheEverything/cacheTtl for these signed downloads. Drive
 * can return an HTML virus-scan page instead of file bytes, and links are
 * individually signed and short-lived.
 */
const FILE_ID_PATTERN = /^[A-Za-z0-9_-]{1,256}$/;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;
const ALLOWED_METHODS = "GET, OPTIONS";
const ALLOWED_HEADERS = "Range";
const EXPOSED_HEADERS = "Accept-Ranges, Content-Range, Content-Length";

function noStoreHeaders(extra = {}) {
  const headers = new Headers(extra);
  headers.set("Cache-Control", "no-store, private");
  headers.set("CDN-Cache-Control", "no-store");
  headers.set("Cloudflare-CDN-Cache-Control", "no-store");
  return headers;
}

function errorResponse(message, status) {
  return new Response(message, { status, headers: noStoreHeaders() });
}

function allowedOrigins(env) {
  return (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((value) => value.trim())
    .filter((value) => {
      if (!value) return false;
      try {
        const parsed = new URL(value);
        return parsed.protocol === "https:" && parsed.origin === value;
      } catch {
        return false;
      }
    });
}

function addVaryOrigin(headers) {
  const values = (headers.get("Vary") || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (!values.some((value) => value.toLowerCase() === "origin")) values.push("Origin");
  headers.set("Vary", values.join(", "));
}

function withCors(response, request, env) {
  const headers = noStoreHeaders(response.headers);
  addVaryOrigin(headers);

  const origin = request.headers.get("Origin");
  if (origin && allowedOrigins(env).includes(origin)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Access-Control-Allow-Methods", ALLOWED_METHODS);
    headers.set("Access-Control-Allow-Headers", ALLOWED_HEADERS);
    headers.set("Access-Control-Expose-Headers", EXPOSED_HEADERS);
    headers.set("Access-Control-Max-Age", "86400");
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function decodeBase64Url(value) {
  if (!BASE64URL_PATTERN.test(value)) return null;
  try {
    let base64 = value.replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) base64 += "=";
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  } catch {
    return null;
  }
}

async function verifySignature(secret, source, fileId, expiry, signature) {
  const signatureBytes = decodeBase64Url(signature);
  if (!secret || !signatureBytes || signatureBytes.byteLength !== 32) return false;

  try {
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["verify"]
    );
    return await crypto.subtle.verify(
      "HMAC",
      key,
      signatureBytes,
      new TextEncoder().encode(`${source}.${fileId}.${expiry}`)
    );
  } catch {
    return false;
  }
}

function contentDispositionName(name, fileId) {
  const source = (name || `${fileId}.pdf`).slice(0, 255);
  const fallback = source
    .replace(/[\r\n\0]/g, "")
    .replace(/[^\w.\-()[\] ]/g, "_")
    .slice(0, 180) || `${fileId}.pdf`;
  const encoded = encodeURIComponent(source).replace(/[!'()*]/g, (character) =>
    `%${character.charCodeAt(0).toString(16).toUpperCase()}`
  );
  return `attachment; filename="${fallback}"; filename*=UTF-8''${encoded}`;
}

function isHtmlResponse(response) {
  const contentType = response.headers.get("Content-Type") || "";
  return contentType.split(";", 1)[0].trim().toLowerCase() === "text/html";
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/dl\/(google|drime)\/([A-Za-z0-9_-]{1,256})$/);
    if (!match) return withCors(errorResponse("Not found", 404), request, env);

    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }), request, env);
    }
    if (request.method !== "GET") {
      return withCors(errorResponse("Method not allowed", 405), request, env);
    }

    const source = match[1];
    const fileId = match[2];
    if (!FILE_ID_PATTERN.test(fileId)) {
      return withCors(errorResponse("Invalid file id", 400), request, env);
    }

    const expiry = url.searchParams.get("exp") || "";
    const signature = url.searchParams.get("sig") || "";
    if (!/^\d{1,12}$/.test(expiry) || !signature) {
      return withCors(errorResponse("Missing or invalid signature", 403), request, env);
    }
    const expirySeconds = Number(expiry);
    if (!Number.isSafeInteger(expirySeconds) || Date.now() / 1000 > expirySeconds) {
      return withCors(errorResponse("Link expired — request a fresh one", 403), request, env);
    }
    if (!env.DL_SECRET) {
      console.error("Download proxy signing secret is not configured");
      return withCors(errorResponse("Download service unavailable", 503), request, env);
    }
    if (source === "drime" && !env.DRIME_API_KEY) {
      console.error("Drime download API key is not configured");
      return withCors(errorResponse("Download service unavailable", 503), request, env);
    }
    if (!(await verifySignature(env.DL_SECRET, source, fileId, expiry, signature))) {
      return withCors(errorResponse("Invalid signature", 403), request, env);
    }

    const upstreamHeaders = new Headers();
    if (source === "drime") {
      upstreamHeaders.set("Authorization", `Bearer ${env.DRIME_API_KEY}`);
      upstreamHeaders.set("Accept", "application/octet-stream, application/pdf, */*");
    }
    const range = request.headers.get("Range");
    if (range) upstreamHeaders.set("Range", range);

    const upstreamUrl = source === "drime"
      ? `https://app.drime.cloud/api/v1/file-entries/download/${fileId}`
      : `https://drive.google.com/uc?export=download&id=${fileId}`;
    let upstreamResponse;
    try {
      upstreamResponse = await fetch(upstreamUrl, {
        method: "GET",
        headers: upstreamHeaders,
        redirect: "follow",
      });
    } catch (error) {
      // Do not include the upstream exception, API key, or identifiers in the response.
      console.error(`${source} download fetch failed`, error instanceof Error ? error.name : "unknown error");
      return withCors(errorResponse("The file could not be fetched. Please try again.", 502), request, env);
    }

    if (isHtmlResponse(upstreamResponse)) {
      return withCors(errorResponse("Upstream returned a non-file page", 502), request, env);
    }
    if (upstreamResponse.status !== 200 && upstreamResponse.status !== 206) {
      return withCors(errorResponse("Upstream error fetching file", 502), request, env);
    }

    const headers = noStoreHeaders(upstreamResponse.headers);
    headers.set("Content-Disposition", contentDispositionName(url.searchParams.get("name"), fileId));
    headers.set("X-Content-Type-Options", "nosniff");
    headers.set("Referrer-Policy", "no-referrer");
    headers.delete("Set-Cookie");

    // Preserve 206 and the upstream Content-Range/Accept-Ranges headers so
    // clients can resume interrupted downloads.
    return withCors(new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      statusText: upstreamResponse.statusText,
      headers,
    }), request, env);
  },
};
