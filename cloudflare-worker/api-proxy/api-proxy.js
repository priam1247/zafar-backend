/**
 * HTTPS front door for the backend (now hosted on Koyeb).
 * https://zafar-api.<you>.workers.dev/*  ->  https://bloody-brigitta-ahzafar-de9b1709.koyeb.app/*
 * Fixes mixed-content blocking (HTTPS frontend -> HTTP backend): browsers
 * silently block a request from an HTTPS page to a plain-HTTP API, with
 * no visible error other than the request never firing.
 */

const ORIGIN = "https://bloody-brigitta-ahzafar-de9b1709.koyeb.app"; // Koyeb service (Frankfurt)

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const upstream = ORIGIN + url.pathname + url.search;

    // Forward method, headers (incl. Authorization) and body untouched.
    const resp = await fetch(upstream, {
      method: request.method,
      headers: request.headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    });

    // Pass the response through as-is (FastAPI's CORS headers included).
    const headers = new Headers(resp.headers);
    return new Response(resp.body, { status: resp.status, headers });
  },
};
