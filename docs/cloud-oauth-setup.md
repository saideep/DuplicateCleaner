# Cloud OAuth setup

DuplicateCleaner v0.2 scans cloud storage accounts alongside local paths. This guide walks a fresh Mac mini through connecting each supported source: Google Drive, OneDrive, Google Photos, and iCloud.

Every command runs from your workspace with `uv run dc ...` (or plain `dc` inside an activated venv). This guide uses the shorter `dc` form throughout.

## What "connecting an account" does

DuplicateCleaner uses OAuth 2.0 for Google and Microsoft. Each `dc auth add` call:

1. Opens your default browser to the provider's login and consent page.
2. Redirects back to a temporary local server (`http://127.0.0.1:<random-port>`).
3. Exchanges the callback code for an access token and a long-lived refresh token.
4. Writes the tokens to `~/.config/duplicate_cleaner/tokens/<account_id>.json` at mode `0600`.
5. Appends the account to `~/.config/duplicate_cleaner/accounts.toml`.

You never paste a password into the CLI. The browser handles it.

## Bring your own OAuth client (BYO — required)

**DuplicateCleaner is BYO-only for cloud auth by design.** The repo is public and bundling personal OAuth client IDs in a public project would (a) share the maintainer's quota with every user, (b) create a shared-revocation risk (one bad actor can get the client_id revoked and break every user at once), and (c) make it harder for you to audit the exact permissions the app is asking for. rclone / gsutil ship bundled clients because they're maintained by companies with dedicated OAuth teams — a personal-scale tool is different.

You register your own OAuth clients once (10 minutes per provider) at Google Cloud Console + Azure Portal, then pass `--client-secret path.json` to `dc auth add`. See "Register your Google client" and "Register your OneDrive client" below.

The `dc auth add gdrive` / `dc auth add onedrive` commands without `--client-secret` will refuse with an actionable error telling you to BYO — this is intentional, not a "coming soon" state.

## Google Drive

### Add a Google account

```shell
dc auth add gdrive --client-secret ~/Downloads/client_secret_YOUR_ID.apps.googleusercontent.com.json
```

See "Register your Google client" at the bottom of this page for how to produce the JSON file.

What happens:

```
+------------------+     +------------------+     +---------------------+
| dc auth add      |     | Your browser     |     | accounts.google.com |
+--------+---------+     +--------+---------+     +----------+----------+
         |                        |                          |
         | starts local server    |                          |
         | at 127.0.0.1:<port>    |                          |
         |                        |                          |
         | opens browser -------> |                          |
         |                        | signs you in -----------> |
         |                        | consent screen --------- |
         |                        | callback with ?code=... <-|
         |                        |                          |
         | receives code ---------|                          |
         | exchanges for tokens ------------------------------>|
         | writes token file      |                          |
         | prints "Authorized as user@example.com"           |
```

The consent screen lists the exact scope: "See, edit, create, and delete only the specific Google Drive files you use with DuplicateCleaner." That corresponds to the `drive.file` scope — see "Scopes" below.

When the callback lands, a plain page in the browser says "Authorization complete. You may close this tab." The CLI prints the account ID it saved.

### Multi-account: add a second Google account

Run `dc auth add gdrive` again with a custom label. The Google account chooser lets you pick a different signed-in Google account or add one:

```shell
dc auth add gdrive --label family
dc auth add gdrive --label work
```

Now `dc auth list` shows three Google accounts:

```
gdrive:personal   me@gmail.com          added 2026-09-05
gdrive:family     partner@gmail.com     added 2026-09-05
gdrive:work       me@corp.example.com   added 2026-09-05
```

Labels become source IDs: `dc scan --sources local,gdrive:family,gdrive:work ...`.

If you omit `--label`, the first Google account is `gdrive:personal`, the second `gdrive:personal-2`, and so on.

### Corporate and Workspace accounts

A Google Workspace account (`@company.com`) may be gated by your organization's admin. If your admin has restricted third-party OAuth apps, the consent screen will refuse and the flow will end with a Google-provided error like "Access blocked: Your organization's admin needs to approve this app." That is a Workspace admin policy — no changes on our side can bypass it. Point your admin at the client ID printed by `dc auth add` and ask them to approve it (or use a personal Google account instead).

### Scopes

DuplicateCleaner requests exactly one scope:

- `https://www.googleapis.com/auth/drive.file`

That grants read and write access **only to files the tool itself created or that you explicitly picked** via a Google Drive Picker. It does not enumerate your whole Drive. For a full-Drive scan you must run `dc auth grant-drive-picker` and select the folders you want in scope (documented alongside `dc auth add gdrive` output).

DuplicateCleaner does not request `drive.readonly` or the broad `drive` scope. The scopes it does hold are read-and-trash — it literally cannot hard-delete a file even if a bug tried to.

## Google Photos

### Add a Google Photos account

```shell
dc auth add gphotos --client-secret ~/Downloads/client_secret_YOUR_ID.apps.googleusercontent.com.json --label personal
```

Same Google Cloud Console setup as Google Drive, but you enable the **Google Photos Library API** for the project instead of (or in addition to) the Drive API. See "Register your Google client" at the bottom of this page for the full 10-minute walk-through; the only difference for Photos is the "APIs & Services → Library" page — search for and enable "Google Photos Library API".

The flow is identical in shape to the Google Drive one — local callback server, browser to `accounts.google.com`, consent screen, tokens written to `~/.config/duplicate_cleaner/tokens/gphotos:personal.json` at mode `0600`.

### Scopes

DuplicateCleaner v0.6 requests exactly one scope on `dc auth add gphotos`:

- `https://www.googleapis.com/auth/photoslibrary.readonly`

That grants read-only access to the photos the app can see (your own uploads, i.e. everything under `photoslibrary.readonly`). Under this scope the source can detect duplicates but cannot trash them.

### Scope escalation (v0.6.1)

Google Photos trash requires the broader `https://www.googleapis.com/auth/photoslibrary` scope, which needs a user re-consent flow. Escalation is per-account: granting trash on `gphotos:personal` does NOT grant it for `gphotos:family`.

```shell
dc auth grant-gphotos-trash gphotos:personal   # re-consent to full scope
dc auth revoke-gphotos-trash gphotos:personal  # downgrade back to read-only
```

`dc auth list` shows `read-only` or `trash-enabled` in the `scope` column so you can see the current state at a glance. A trash-enabled account's members flow through the normal cross-source scoring instead of being marked informational-only, so they can be proposed as discards in a mixed local + gphotos scan.

**Google Photos Library API limitation.** Even after a successful `grant-gphotos-trash` escalation, the actual trash step fails with an actionable `SourceError` pointing at [https://photos.google.com/trash](https://photos.google.com/trash) for the manual step. This is because Google Photos Library API v1 does NOT expose a library-wide trash / delete endpoint — the full `photoslibrary` scope grants write access only to app-created content (album creation, `batchAddMediaItems`, `batchRemoveMediaItems`), not arbitrary library items. This is a documented Google API limitation, not a DuplicateCleaner constraint; if Google reintroduces a library-scoped delete on the Photos API, `move_to_trash` will start working without further config changes. Until then, use the web UI at [https://photos.google.com](https://photos.google.com) → select item → Delete.

The Google Photos API also does not expose a per-item MD5 or SHA-256. To detect cross-source duplicates (a JPEG in Google Photos vs. the same JPEG on your local disk), DuplicateCleaner reads the media item's `baseUrl` (re-fetched at reconcile time because these URLs expire in ~60 minutes), streams the `=d`-suffixed original-quality bytes, and computes a BLAKE3 hash locally. Cross-source reconciliation is gated by `--max-cloud-download-mb` (same knob as Drive / OneDrive) so a full-library scan does not silently blow through your data cap.

### Multi-account Google Photos

Same pattern as Google Drive:

```shell
dc auth add gphotos --label personal --client-secret ~/oauth.json
dc auth add gphotos --label family --client-secret ~/oauth.json
```

## OneDrive

### Add a OneDrive account

```shell
dc auth add onedrive --client-secret ~/msal-client.json
```

See "Register your OneDrive client" at the bottom of this page for how to produce the JSON file.

The flow mirrors Google Drive: local callback server, browser opens to `login.microsoftonline.com`, you consent, tokens are saved.

```
+------------------+     +------------------+     +---------------------------+
| dc auth add      |     | Your browser     |     | login.microsoftonline.com |
+--------+---------+     +--------+---------+     +--------------+------------+
         |                        |                              |
         | opens browser -------> |                              |
         |                        | signs you in ---------------> |
         |                        | consent screen ------------- |
         |                        | callback with ?code=... <----|
         |                        |                              |
         | exchanges for tokens ---------------------------------->|
         | writes token file      |                              |
         | prints "Authorized as user@outlook.com"               |
```

DuplicateCleaner supports **OneDrive Personal only** in v0.2. The OAuth flow uses Microsoft's `/consumers/` authority which rejects Business / Work tenants at the token endpoint — Business tenants cannot complete the flow. This is deliberate: OneDrive Business exposes only `quickXorHash`, which the scanner cannot align with local BLAKE3 without extra work. Business support is not on the v0.2 roadmap.

Business items missing `file.hashes.sha256Hash` (which is Personal-only) are skipped during enumeration with a debug log line. If you find yourself with a Business account that somehow completed the flow, `dc scan` will find zero indexable files and warn accordingly.

### Multi-account OneDrive

Same pattern as Google:

```shell
dc auth add onedrive --label main
dc auth add onedrive --label spouse
```

### Scopes

DuplicateCleaner requests exactly:

- `Files.ReadWrite`
- `offline_access`
- `User.Read`

`Files.ReadWrite` covers reading files and moving items to the recycle bin. `offline_access` is what makes the refresh token issue. `User.Read` is used at `dc auth add` time only, to fetch your account's email and object id via `GET /me` for account labelling and shared-vs-owned classification. None of these grants hard-delete beyond recycle bin.

### Restore quirk on OneDrive Personal

Microsoft Graph documents `POST /me/drive/items/{id}/restore` as OneDrive Business only. OneDrive Personal historically returns HTTP 501 or an error body with `code: "notSupported"` for the same call. When `dc undo` encounters this on a OneDrive Personal manifest entry, the entry is reported as failed with a message pointing at your Recycle Bin at `https://onedrive.live.com/?id=recyclebin`. Successful entries in the same manifest still complete. This is a provider limitation — no changes on our side can bypass it.

## iCloud Photos

```shell
dc auth add icloud --label personal
```

Optionally pass `--library-path` to point at a Photos library outside the default location:

```shell
dc auth add icloud --library-path /Volumes/OldMac/Users/me/Pictures/Photos\ Library.photoslibrary --label oldmac
```

No OAuth is involved. `dc auth add icloud` just:

1. Verifies the `Photos Library.photoslibrary` bundle exists at the given path (default `~/Pictures/Photos Library.photoslibrary`).
2. Registers the account in `~/.config/duplicate_cleaner/accounts.toml` with `type="icloud"` and the resolved library path.

No token file is written. Terminal or your shell needs Full Disk Access granted in System Settings → Privacy & Security → Full Disk Access to actually read the bundle — the first `dc scan` will surface the OS's permission prompt if that grant is missing.

### How iCloud Photos scanning works

The library is read via [`osxphotos`](https://github.com/RhetTbull/osxphotos), a Python reader for the local Photos database. Every photo's `original_filepath` is opened directly; DuplicateCleaner reads the bytes, computes a BLAKE3 hash, and caches it per `(source_id, uuid, date_modified)` so unchanged photos aren't re-hashed on rescan.

### Optimize Mac Storage caveat

If you have "Optimize Mac Storage" enabled in Photos → Preferences → iCloud, some photos are represented locally as stubs — the metadata exists but the original bytes are only on iCloud. DuplicateCleaner skips these (they have no local `path` for us to hash) and logs the skipped count on every scan:

```
iCloud Photos: skipped 342 stubs (files not downloaded locally).
Toggle 'Download Originals to This Mac' in Photos → Preferences → iCloud to make them scannable.
```

### Deletion

iCloud Photos is **read-only permanently** in DuplicateCleaner. `osxphotos` is a reader library, not a writer — the safe path to delete an iCloud photo is through the Photos.app itself (which also handles the iCloud sync side of the deletion). `dc apply` refuses to trash iCloud Photos entries with a message pointing you at the Photos.app.

If you want to bypass the Photos.app and trash the underlying local file directly, run `dc scan` on `~/Pictures` (or the relevant folder inside the Photos library bundle) — the standard local-mover rails then apply. This is a power-user path and is not recommended: Photos.app maintains its own database that will not reconcile with a raw filesystem delete.

## Listing, testing, removing accounts

```shell
dc auth list             # print every configured account
dc auth test gdrive:work # refresh the token and hit /me to prove it works
dc auth remove gdrive:work
```

`dc auth list` prints account ID, provider type, user email, and when the account was added. It never prints token values.

`dc auth test` refreshes the access token (using the stored refresh token), calls a low-cost identity endpoint (`/me` on Graph, `about.get` on Drive), and prints OK plus the user info on success. Any auth error surfaces here early — no need to run a full scan to find out a token is stale.

`dc auth remove` deletes the token file, best-effort revokes at the provider's revocation endpoint, and removes the entry from `accounts.toml`. If the revocation call fails (offline, provider rate limit, etc.), the local file is still deleted and a warning is logged. To fully cut access, sign in to the provider's account page and remove the app from your connected apps list.

Note: Microsoft does not expose a programmatic token-revocation endpoint the way Google does. For OneDrive accounts, `dc auth remove` deletes the local token file and prints a link to `https://account.live.com/consent/Manage` where you can revoke the app for good. For Google accounts, the revoke call hits `oauth2.googleapis.com/revoke` and only falls back to the account-page instructions if that call fails.

## Token storage

### Files on disk

```
~/.config/duplicate_cleaner/
  accounts.toml                     # mode 0600, list of accounts
  tokens/                           # mode 0700 directory
    gdrive:personal.json            # mode 0600
    gdrive:family.json              # mode 0600
    onedrive:main.json              # mode 0600
```

The tokens directory is created with mode `0700` (owner-only). Each token file is written with mode `0600` and re-checked on every read — if the file is world- or group-readable, DuplicateCleaner refuses to use it and prints a fix (`chmod 600 ~/.config/duplicate_cleaner/tokens/<id>.json`).

### What is inside a token file

```json
{
  "account_id": "gdrive:personal",
  "type": "gdrive",
  "user_email": "me@gmail.com",
  "access_token": "ya29...",
  "refresh_token": "1//0g...",
  "expires_at": 1793145600.0,
  "scopes": ["https://www.googleapis.com/auth/drive.file"]
}
```

OneDrive follows the same shape with `"type": "onedrive"` and the Microsoft scopes:

```json
{
  "account_id": "onedrive:main",
  "type": "onedrive",
  "user_email": "me@outlook.com",
  "access_token": "EwB...",
  "refresh_token": "M.C1_BAY...",
  "expires_at": 1793145600.0,
  "scopes": ["Files.ReadWrite", "offline_access", "User.Read"]
}
```

You may inspect this file. Do not commit it, mail it, or paste it into a bug report. If you have to share logs, use `dc auth list` output instead — tokens are redacted from all logs and reports the tool writes.

### Rotation

Provider settings expose "connected apps" pages where you can revoke DuplicateCleaner:

- Google: [https://myaccount.google.com/permissions](https://myaccount.google.com/permissions)
- Microsoft: [https://account.live.com/consent/Manage](https://account.live.com/consent/Manage)

If you suspect a token file has leaked, revoke there and then `dc auth add <type>` again to re-authorize.

## Bring your own OAuth client (advanced)

If you would rather use your own OAuth client — because you want your own quota, want to whitelist which Google/Microsoft users can use the client, or run in an environment where the bundled client is not appropriate — pass `--client-secret path.json` to `dc auth add`.

### Google BYO client

1. Go to [https://console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials).
2. Create or pick a project.
3. Enable the **Google Drive API** for the project.
4. Configure the **OAuth consent screen**. External user type. Testing mode is fine for personal use; you never need to submit for verification if the app stays in testing and you add your own account as a test user.
5. **Create Credentials → OAuth client ID**. Application type: **Desktop app**. Give it any name.
6. Download the JSON. It looks like `client_secret_<numbers>.googleusercontent.com.json`.

```shell
dc auth add gdrive --client-secret ~/Downloads/client_secret_...json --label personal
```

### Microsoft BYO client

1. Go to [https://portal.azure.com/](https://portal.azure.com/) → Microsoft Entra ID → App registrations → New registration.
2. Name it, choose **Personal Microsoft accounts only**, redirect URI type **Public client / native (mobile & desktop)**, value `http://localhost`.
3. In **API permissions** add delegated `Files.ReadWrite` and `offline_access`.
4. In **Authentication** confirm the loopback redirect is registered.
5. Copy the Application (client) ID. Paste it into a JSON file like:

   ```json
   { "client_id": "<application-id>", "authority": "https://login.microsoftonline.com/consumers" }
   ```

6. `dc auth add onedrive --client-secret ~/msal-client.json --label main`.

BYO clients get their own quota and their own revocation surface. The bundled client is fine for most users; BYO is there so power users are never stuck.

## Security notes

- **Tokens are equivalent to SSH keys.** A refresh token grants ongoing access to the account's Drive or OneDrive files until you revoke it. Protect the token file. Do not check it into git. Do not paste it into support threads.
- **Scopes cannot hard-delete.** The chosen scopes (`drive.file`, `Files.ReadWrite`) support reading and moving to trash / recycle bin. There is no API path for hard-delete in these scopes. See [safety.md](safety.md#cloud-safety) for the full invariant.
- **Shared files are never proposed for deletion.** Any file the provider reports as shared with you (owned by someone else) is treated as informational only. See [safety.md](safety.md#cloud-safety).
- **Revocation.** If a token leaks or the machine is lost, revoke at the provider's connected-apps page immediately. Then re-authorize with `dc auth add`.
- **Corporate policy.** If your Workspace or Entra admin blocks third-party OAuth apps, the flow will error out on the consent screen; no changes on the tool's side can bypass it.
