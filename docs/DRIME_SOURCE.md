# Drime Cloud library source

Drime is an optional second source. When `DRIME_API_KEY` is set, its files are
merged into the existing Google Drive Papers, Books, Notes, and Marking Keys
sections. The existing category filters, account quotas, reservation/confirm
flow, and short-lived download URLs remain in effect.

## Deployment configuration

Set these Koyeb variables:

```text
DRIME_API_KEY=<the key used by the working Drime uploader>
DRIME_API_URL=https://app.drime.cloud/api/v1
DRIME_ROOT_FOLDER_ID=1016158780
DRIME_WORKSPACE_ID=0
DRIME_PAPERS_FOLDER_NAMES=Primary,Mock,MANEB
DRIME_BOOKS_FOLDER_NAMES=Books
DRIME_NOTES_FOLDER_NAMES=Notes
DRIME_MARKING_KEYS_FOLDER_NAMES=Marking Keys,Marking Schemes
```

The root ID and API base follow the supplied working uploader. Section-folder
names match the shown `secondary school BOOKS` Drime root. Adjust the `*_FOLDER_NAMES`
values if that structure changes.

Also add `DRIME_API_KEY` as a **secret** to the Cloudflare download-proxy Worker
(`zafar-dl`), alongside `DL_SECRET`, then deploy the updated Worker. The Drime
API key is needed in both places: Koyeb lists file entries, and the Worker
fetches the file bytes after validating the backend's short-lived signature.
Do not expose this key to the frontend or place it in a public repo.

The adapter uses the Drime API's Bearer key, paginated `drive/file-entries`
listing, and `file-entries/download/{hash}` downloads. The Worker accepts only
`/dl/google/{id}` and `/dl/drime/{hash}` links whose signature includes the
provider name, so a Google URL cannot be changed into a Drime URL (or vice
versa).
