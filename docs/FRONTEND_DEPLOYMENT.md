# Frontend origin and CORS deployment

The new canonical frontend origin is **`https://studymw.netlibrary.dpdns.org`**.
Keep the previous **`https://library.zafarh.dpdns.org`** origin allowed while
cutting over. The backend `ALLOWED_ORIGINS` and the download Worker's allowed
origins must include the exact HTTPS origin(s), including scheme and hostname.

## Cutover steps

1. In the Koyeb backend service environment, set:
   ```text
   ALLOWED_ORIGINS=https://studymw.netlibrary.dpdns.org,https://library.zafarh.dpdns.org
   ```
   Redeploy/restart the backend so its CORS middleware permits requests from
   the new frontend origin.
2. In the Cloudflare **download-proxy Worker** settings, set its allowed-origin
   variable(s) to both origins (comma-separated):
   ```text
   ALLOWED_ORIGINS=https://studymw.netlibrary.dpdns.org,https://library.zafarh.dpdns.org
   ```
   The worker continues to accept the legacy singular `ALLOWED_ORIGIN` setting,
   but use `ALLOWED_ORIGINS` for the migration. Deploy the worker.
3. In the Cloudflare `zafar-frontend` Worker domain settings, attach
   `studymw.netlibrary.dpdns.org` to the **Production** environment and make it
   the domain students use. The screenshot shows it already listed as a
   Production custom domain; confirm its DNS/route is active before switching.
4. Verify registration/login, API list requests, and signed downloads from the
   new origin. Check both a successful download and an expired link.
5. After the old hostname is no longer used, remove it from backend and
   download-worker origin lists, then redeploy both.

The frontend's API base URL must still point to the existing API/API-proxy
service; changing the website domain does not change the API origin. The API
permits `GET`, `POST`, `PATCH`, and `OPTIONS`; Bearer authorization and JSON
content headers are allowed. The download Worker returns CORS headers on
success and error responses and varies responses by `Origin`.
