/**
 * Zafar API client (fetch-based, no dependencies).
 *
 * Point API_BASE at the api-proxy Worker in production (HTTPS front door
 * for the HTTP KataBump backend — see ../DEPLOYMENT.md), or straight at
 * the backend for local dev.
 */

const API_BASE = import.meta.env?.VITE_API_URL || "http://127.0.0.1:20208";

const TOKEN_KEY = "zafar_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function logout() {
  localStorage.removeItem(TOKEN_KEY);
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed with status ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, { method = "GET", json, form, auth = true } = {}) {
  const headers = {};
  let body;

  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form !== undefined) {
    body = new URLSearchParams(form); // application/x-www-form-urlencoded
  }

  if (auth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  let resp;
  try {
    resp = await fetch(`${API_BASE}${path}`, { method, headers, body });
  } catch (networkErr) {
    // fetch() rejects only on network failure (offline, DNS, CORS
    // preflight failure, mixed-content block) — never on HTTP status.
    throw new ApiError(0, "Network error — check your connection and try again.");
  }

  if (resp.status === 204) return null;

  let payload = null;
  try {
    payload = await resp.json();
  } catch {
    // Non-JSON response (e.g. a plain-text 502 from a Worker) — fall through.
  }

  if (!resp.ok) {
    const detail = payload?.detail || payload?.message;
    if (resp.status === 401) {
      logout(); // stale/expired token — clear it so the UI can prompt re-login
      throw new ApiError(401, detail || "Session expired — please sign in again.");
    }
    if (resp.status === 403) {
      throw new ApiError(403, detail || "You don't have permission to do that.");
    }
    if (resp.status === 404) {
      throw new ApiError(404, detail || "Not found.");
    }
    if (resp.status === 429) {
      throw new ApiError(429, detail || "Daily download limit reached — try again tomorrow.");
    }
    throw new ApiError(resp.status, detail || "Something went wrong.");
  }

  return payload;
}

// ---------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------

export async function register(email, password) {
  return request("/auth/register", { method: "POST", json: { email, password }, auth: false });
}

export async function verify(email, code) {
  return request("/auth/verify", { method: "POST", json: { email, code }, auth: false });
}

export async function resendCode(email) {
  return request("/auth/resend", { method: "POST", json: { email }, auth: false });
}

export async function login(email, password) {
  const data = await request("/auth/login", { method: "POST", form: { email, password }, auth: false });
  setToken(data.access_token);
  return data;
}

export async function getCurrentUser() {
  return request("/auth/me");
}

export async function getQuota() {
  return request("/auth/quota");
}

// ---------------------------------------------------------------------
// Papers / books
// ---------------------------------------------------------------------

/**
 * @param {"papers"|"books"|"notes"|"marking-keys"} section
 */
export async function listPapers(section = "papers", { category, q } = {}) {
  const params = new URLSearchParams({ section });
  if (category) params.set("category", category);
  if (q) params.set("q", q);
  return request(`/drive/papers?${params}`);
}

/**
 * Enforces quota server-side and returns a signed, short-lived URL.
 * Call this only when the user actually clicks "download".
 */
export async function requestDownload(fileId, section = "papers") {
  return request(`/drive/download/${encodeURIComponent(fileId)}?section=${section}`, { method: "POST" });
}

/** Convenience: request the signed URL, then navigate the browser to it. */
export async function download(fileId, section = "papers") {
  const { url } = await requestDownload(fileId, section);
  window.location.assign(url);
}

// ---------------------------------------------------------------------
// Example usage
// ---------------------------------------------------------------------
//
// try {
//   await register("student@gmail.com", "a-strong-password");
//   await verify("student@gmail.com", "123456");
//   await login("student@gmail.com", "a-strong-password");
//   const papers = await listPapers("papers", { category: "MSCE Maneb" });
//   await download(papers[0].id);
// } catch (err) {
//   if (err.status === 429) showQuotaExceededToast();
//   else if (err.status === 401) redirectToLogin();
//   else showErrorToast(err.message);
// }
