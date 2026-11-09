# Frontend origin and CORS deployment

The canonical frontend origin is **`https://library.zafarh.dpdns.org`** (the
hostname includes the `h` in `zafarh`). It is the only production origin to add
by default. Use HTTPS only. The backend `ALLOWED_ORIGINS` environment variable
and Cloudflare download Worker's `ALLOWED_ORIGIN` must agree exactly, including
scheme and hostname. The browser frontend calls the API at the configured API
base URL and fetches signed downloads from the Worker, so both services need
matching CORS settings.

If the frontend hostname changes, deploy in this order:

1. Add the new exact origin to backend `ALLOWED_ORIGINS` (comma-separated) and
   set the Worker's `ALLOWED_ORIGIN` to the new origin.
2. Deploy/restart the backend and Worker, then deploy the frontend.
3. Verify `/auth/login` preflight/API access and both successful and expired
   `/dl/*` browser fetches from the new origin.
4. Remove the old origin only after the new deployment is verified.

The download Worker returns CORS headers on success and error responses, and
varies responses by `Origin`. The API permits `GET`, `POST`, `PATCH`, and
`OPTIONS`; Bearer authorization and JSON content headers are allowed.
