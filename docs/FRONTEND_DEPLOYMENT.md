# Frontend origin and CORS deployment

The only allowed production frontend origin is
**`https://studymw.netlibrary.dpdns.org`**. The backend `ALLOWED_ORIGINS` and
download Worker's `ALLOWED_ORIGINS` must match it exactly, including HTTPS.

## Production settings

1. In the Koyeb backend service environment, set:
   ```text
   ALLOWED_ORIGINS=https://studymw.netlibrary.dpdns.org
   ```
   Remove any previous frontend hostname from the value, then redeploy the backend.
2. In the Cloudflare **download-proxy Worker** settings, set:
   ```text
   ALLOWED_ORIGINS=https://studymw.netlibrary.dpdns.org
   ```
   Delete the obsolete singular `ALLOWED_ORIGIN` variable and deploy with only
   the new origin in `ALLOWED_ORIGINS`.
3. In the Cloudflare `zafar-frontend` Worker domain settings, keep
   `studymw.netlibrary.dpdns.org` attached to **Production** and remove the old
   `library.zafarh.dpdns.org` custom-domain binding. Remove its DNS route/record
   too if it is no longer used.
4. Verify registration/login, API list requests, and signed downloads from the
   new origin. Test both a successful download and an expired link.

Changing the website domain does not change the API origin: the frontend should
continue calling the configured API/API-proxy service. The API permits `GET`,
`POST`, `PATCH`, and `OPTIONS`; Bearer authorization and JSON content headers
are allowed. The download Worker returns CORS headers on success and error
responses and varies responses by `Origin`.
