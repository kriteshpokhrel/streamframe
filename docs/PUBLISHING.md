# Publishing clips

StreamFrame renders clips. The `streamframe.publish` package posts them. It is a
separate component with no write access to jobs, candidates, or footage: it reads
a finished export and sends it to a platform.

| Platform | Delivery | Human step |
| --- | --- | --- |
| YouTube | Short upload; public, unlisted or private | none |
| Instagram | Reel published through the Meta API | none |
| TikTok | Draft delivered to the account inbox | publish it in the app |

TikTok is the odd one because public API posting needs an audited app, and the
audit rejects tools that upload to accounts the operator manages. The draft route
(`video.upload`) needs no audit. TikTok discards an unfinished draft after 24
hours and allows roughly five pending drafts. See
[decisions](PUBLISHING-DECISIONS.md) for why this shape was chosen.

Nothing here schedules itself. Either a person runs `publish run`, the editor's
**Upload to** button calls the API, or `publish_auto` lets the server publish
each export as it lands.

## Prerequisites

Publishing runs inside StreamFrame, so it inherits the app's requirements. Each
one is here with the reason it is needed, because a missing piece usually shows
up as a confusing platform error rather than a clear one.

| Requirement | Why |
| --- | --- |
| Python 3.10 | the pinned interpreter; `requirements.lock` targets it |
| Node.js 22 | builds the editor the launcher serves |
| FFmpeg with `libx264` | H.264 encoding; the publisher uploads the rendered file |
| FFmpeg with AAC | the audio track in every export |
| FFmpeg with `libass` | burned-in captions; rendering fails without it |
| `ffprobe` beside `ffmpeg` | duration and dimension checks during import and export |
| Whisper `small` (optional) | word-timed captions; without it clips analyze speech-free |
| Writable data directory (`~/.streamframe` by default) | jobs, exports, tokens and the ledger |
| A public HTTPS URL for the OAuth callback | Meta and TikTok reject loopback redirects |
| No extra Python packages | publishing uses `requests`, already a dependency |

The callback URL needs no server of its own: the authorization code appears in
the address bar, so any page you control works. A static site is enough — the
consent pages in this repository are one example.

Each platform issues its own credentials through its own developer console;
there is no shared registry, which is why setup is per platform rather than one
sign-up.

## Quick start

```sh
./streamframe publish setup --write-env   # create .env, print the next step
./streamframe publish doctor              # exit 0 when nothing is missing
./streamframe publish auth youtube        # once per platform, per machine
./streamframe publish run --file clip.mp4 --title "The hook" --yes
```

## Configuration

Everything lives in a git-ignored `.env` at the repository root.
`./streamframe publish setup --write-env` creates it from `.env.example` with
mode 0600; `./streamframe publish setup` re-checks it and names the next missing
step. Real environment variables override the file.

### Core

| Variable | Default | Meaning |
| --- | --- | --- |
| `STREAMFRAME_PUBLISH_PLATFORMS` | all three | Comma-separated subset that may post. Anything listed but unconfigured shows as a problem. |
| `STREAMFRAME_PUBLISH_AUTO` | `0` | App-level default for `auto_publish` on new jobs. |
| `STREAMFRAME_PUBLISH_STATE_DIR` | `<data dir>/publish` | Where tokens and the ledger live. Pin it if the server and CLI use different data directories. |
| `STREAMFRAME_PUBLISH_HTTP_TIMEOUT` | `120` | Per-request timeout, seconds. |
| `STREAMFRAME_PUBLISH_POLL_TIMEOUT` | `300` | How long to wait for Instagram to finish processing a container. |

### YouTube

| Variable | Default | Meaning |
| --- | --- | --- |
| `STREAMFRAME_YOUTUBE_CLIENT_ID` | — | OAuth client ID. |
| `STREAMFRAME_YOUTUBE_CLIENT_SECRET` | — | OAuth client secret. |
| `STREAMFRAME_YOUTUBE_PRIVACY` | `public` | `public`, `unlisted`, `private`. |
| `STREAMFRAME_YOUTUBE_CATEGORY_ID` | `24` | YouTube category (24 = Entertainment). |
| `STREAMFRAME_YOUTUBE_MADE_FOR_KIDS` | `false` | Sets `selfDeclaredMadeForKids`. |

### Instagram

| Variable | Default | Meaning |
| --- | --- | --- |
| `STREAMFRAME_INSTAGRAM_APP_ID` | — | Meta app ID. |
| `STREAMFRAME_INSTAGRAM_APP_SECRET` | — | Meta app secret. |
| `STREAMFRAME_INSTAGRAM_API_VERSION` | `v23.0` | Graph API version. Match the app dashboard. |
| `STREAMFRAME_INSTAGRAM_LOGIN` | `instagram` | `facebook` for Page-linked accounts, `instagram` for older standalone apps. |
| `STREAMFRAME_INSTAGRAM_PAGE_ID` | — | Optional. Skips `me/accounts` discovery and reads this Page directly. |
| `STREAMFRAME_INSTAGRAM_SHARE_TO_FEED` | `true` | Also show the Reel in the feed. |

### TikTok

| Variable | Default | Meaning |
| --- | --- | --- |
| `STREAMFRAME_TIKTOK_CLIENT_KEY` | — | App client key. |
| `STREAMFRAME_TIKTOK_CLIENT_SECRET` | — | App client secret. |

### Media host (Instagram only)

Meta downloads a Reel from a URL, so Instagram needs the clip to be reachable.
YouTube and TikTok accept the file bytes and never touch this.

| Variable | Default | Meaning |
| --- | --- | --- |
| `STREAMFRAME_MEDIA_HOST` | `none` | `none`, `catbox`, `local`, `r2`. |
| `STREAMFRAME_CATBOX_EXPIRY` | `1h` | `1h`, `12h`, `24h`, `72h`. |
| `STREAMFRAME_MEDIA_PUBLIC_BASE_URL` | — | Required for `local` and `r2`. |
| `STREAMFRAME_MEDIA_LOCAL_DIR` | — | Required for `local`. |
| `STREAMFRAME_R2_BUCKET` / `_ENDPOINT` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | — | Required for `r2`. R2 asks for billing details even on the free tier. |

### Where secrets live

| Value | Location | Reason |
| --- | --- | --- |
| Client IDs and secrets | `.env` (0600, git-ignored) | shared by a team, rotates rarely |
| Access and refresh tokens | `<state dir>/credentials.json` (0600) | per machine, rotates constantly |
| Publication history | `<state dir>/publications.sqlite3` | idempotency ledger |

### Sharing configuration with a teammate

`.env` carries app registrations and choices, not credentials for posting.
Copying it to another machine is enough to *configure* publishing, but that
machine must still authorize itself:

| Travels in `.env` | Does not travel |
| --- | --- |
| Client IDs and secrets | OAuth tokens (`credentials.json`) |
| Platform selection, privacy default | The publish ledger |
| Media host and expiry | Anything already published |
| `STREAMFRAME_PUBLISH_REDIRECT_URI` (the registered callback) | |
| `STREAMFRAME_INSTAGRAM_PAGE_ID` (account-specific, not machine-specific) | |

Practical steps for a collaborator:

1. Copy `.env` and `chmod 600 .env`. Never commit it.
2. Install as usual (Python 3.10, Node 22, FFmpeg with `libx264`, AAC and
   `libass`). Publishing adds no dependency of its own.
3. Run `./streamframe publish doctor` — it will list every platform as needing
   authorization, which is expected.
4. Run `./streamframe publish auth <platform>` once per platform. It uses
   `STREAMFRAME_PUBLISH_REDIRECT_URI` unless `--redirect-uri` is passed.
5. `STREAMFRAME_PUBLISH_STATE_DIR` uses `~`, so it resolves per machine; tokens
   and the ledger land in that machine's home directory.

**Authorization needs a human.** `publish auth` prints a URL, waits for a
browser approval, then asks for the callback URL on stdin. Everything before and
after that can be automated; this one step cannot, because it exists to prove
the operator's identity. An agent should run other commands freely, but print the
URL and stop here rather than trying to script the consent.

Two people publishing from two machines both authorize separately, and each keeps
its own ledger. That means the same file could be posted once per machine. Share
one state directory (or check `publish status`) if that matters; do not share the
tokens themselves.

If the collaborator publishes to a **different** brand, change
`STREAMFRAME_INSTAGRAM_PAGE_ID` to that Page, or leave it empty and let
`me/accounts` pick it up.

## Platform setup

Each platform needs a developer app. All of them want a privacy policy URL; a
static page on any domain you control is enough.

### YouTube

1. Create a Google Cloud project and enable **YouTube Data API v3**.
2. OAuth consent screen: **External**, then add your own account as a test user.
3. Create an OAuth client of type **Desktop app**.
4. Put the ID and secret in `.env`.
5. Redirect URI: `http://127.0.0.1:8766/callback` (Google allows loopback for
   desktop clients; no registration needed).

> **Publish the consent screen to production.** While it stays in *Testing*,
> Google issues refresh tokens that expire after **7 days**, so unattended
> publishing dies weekly. An unverified app in production still works for your
> own account; users just see a warning.

An upload costs 1 unit from a dedicated bucket of 100 per day, so ten clips a day
is comfortable.

### Instagram

Since 2026 Meta offers new apps two variants, and neither is the old standalone
"Instagram login":

- **Unified login** — needs a business portfolio and an integration system user
- **Facebook login** — needs the Instagram professional account linked to a
  Facebook Page

`STREAMFRAME_INSTAGRAM_LOGIN=facebook` implements the second, which needs the
least setup:

1. The Instagram account must be **Business or Creator**.
2. Link it to a Facebook Page, inside the Instagram app (Business tools and
   controls → connect a Page). A Page alone is not enough; the link is what the
   API reads.
3. Create a Meta app, then add **Instagram → API setup with Facebook login** and
   **Facebook Login for Business**.
4. **App settings → Basic**: copy the App ID and secret, set the privacy policy
   URL, and set **App domains** to the bare host of your callback (for example
   `example.com`).
5. **Facebook Login for Business → Settings**: add the exact HTTPS redirect URI
   under *Valid OAuth Redirect URIs*. Any page you control works, because the
   authorization code appears in the address bar; no server code is required.
6. Put the App ID and secret in `.env`, then authorize:

   ```sh
   ./streamframe publish auth instagram \
     --redirect-uri https://example.com/callback
   ```

   Approve in the browser, select the **Page** when Facebook asks, then paste the
   full callback URL back into the terminal.

App Review is not needed while the app only posts to accounts you own. Keep the
app in Development mode if you prefer.

If `me/accounts` returns no Pages even though the account can see the Page (a
Page owned by a business portfolio can behave this way), set the Page ID
explicitly:

```sh
STREAMFRAME_INSTAGRAM_PAGE_ID=1234567890
```

## TikTok

1. Create an app at `developers.tiktok.com`.
2. Add **Login Kit** and **Content Posting API**, and enable the **Upload**
   configuration only.
3. Request `video.upload`. Do **not** request `video.publish`; that is the audited
   path this design avoids.
4. Put the client key and secret in `.env`, register an HTTPS redirect URI, and
   authorize:

   ```sh
   ./streamframe publish auth tiktok --redirect-uri https://example.com/callback
   ```

Drafts arrive in the account inbox. Five pending drafts per 24 hours is the
ceiling, so publish or discard them in the app.

## Using it

### From the editor

Each finished clip under **Exports** has an **Upload to** block:

- platform pills, pre-selected for every configured platform;
- a **Title** field, required — pre-filled from the clip's opening title or the
  source title, and left empty (button disabled) when neither exists, so a clip
  is never published under an auto-generated filename;
- a **YouTube privacy** choice, defaulting to public;
- one result row per platform: published (with a link), in the TikTok inbox, or
  failed with the platform's reason.

### While adding a source

**Add source** has *Upload finished clips to social platforms*: platforms, title
and privacy for the whole job. It is available for every source type, including
rolling live sessions, where each window that auto-exports is published as it
lands.

### From the CLI

```sh
./streamframe publish run --file clip.mp4 --title "The hook"            # dry run
./streamframe publish run --file clip.mp4 --title "The hook" --yes      # publish
./streamframe publish status
./streamframe publish refresh
```

`run` without `--yes` prints the plan and posts nothing.

### From the API

```sh
# publish one export
curl -X POST localhost:8765/api/jobs/JOB_ID/exports/FILE.mp4/publish \
  -H 'Content-Type: application/json' \
  -d '{"platforms":["youtube"],"title":"The hook","privacy":"public"}'

# what the editor's picker is allowed to offer
curl -s localhost:8765/api/publish
```

The request returns `202` immediately; outcomes accumulate in the job's
`publish_results`.

## Behaviour

- **Isolation.** Each platform is attempted independently. One failure is
  recorded on the job and never stops the others or the render.
- **Idempotency.** A clip is keyed by the SHA-256 of its bytes. One row per
  (clip, platform) is claimed before any network call, so retries, crashes and
  double clicks cannot post twice. A crashed claim is recovered after 15 minutes.
- **Ordering.** One background worker publishes sequentially, which keeps the
  process inside TikTok's per-minute limit and the per-account daily caps.
- **Terminal states.** `published` and `needs_manual` are never retried.
  `needs_manual` is TikTok's inbox draft.
- **Titles.** Manual publishing requires a title of 1–100 characters. Automatic
  publishing requires one too, either supplied per job or inherited from an
  explicit auto-export title.

## Operations

- `.env` is read at first use; **restart the server** after changing it.
- YouTube tokens do not expire while used. Instagram Page tokens from a
  long-lived user token have no scheduled expiry; re-run `publish auth instagram`
  if Meta invalidates one. Standalone Instagram login tokens last 60 days and are
  refreshed by `publish refresh`.
- Cron example, if clips arrive from outside the server:

  ```cron
  0 */2 * * * cd /srv/streamframe && ./streamframe publish run \
    --file "$(ls -t exports/*.mp4 | head -1)" --title "..." --yes >> publish.log 2>&1
  ```

Alert on non-zero exits. A revoked token otherwise costs a day of posts silently.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `publish doctor` exits 1 | a listed platform is incomplete | it names the missing variable |
| `401` / session expired | token revoked or expired | `publish auth <platform>` |
| `Facebook returned no Pages` | authorized profile has no Page role | switch profile or grant Page access |
| `No Instagram professional account is linked to` | Page has no linked IG account | link it in the Instagram app |
| `Could not read Page <id>` | the ID is a profile, not a Page | use the Page ID from Business Settings |
| Instagram `HTTP 400` on media | URL not reachable by Meta | verify the public URL from another machine |
| Instagram container `ERROR` | codec or aspect rejected | export 9:16 H.264/AAC under 300 MB |
| TikTok `spam_risk_too_many_pending_share` | five drafts already waiting | publish or discard drafts |
| `retry limit reached` | three failed attempts | fix the cause, then `publish retry --clip-key ...` |

## Where the code lives

| File | Responsibility |
| --- | --- |
| `publish/settings.py` | `.env` parsing, typed config, per-platform problems |
| `publish/ledger.py` | SQLite idempotency ledger and stale-claim recovery |
| `publish/media.py` | public hosting for Instagram (`catbox`, `local`, `r2`) |
| `publish/tokens.py` | credential store and refresh |
| `publish/auth.py` | OAuth flows, one per platform |
| `publish/youtube.py`, `instagram.py`, `tiktok.py` | one client per platform |
| `publish/runner.py` | orchestration, dry run, error sanitising |
| `publish/cli.py` | `streamframe publish …` |
| `jobs.py` | queue, worker, `publish_export`, per-job settings |
| `server.py` | `/api/publish` and the export publish route |

See [decisions](PUBLISHING-DECISIONS.md) for why it is shaped this way and how to
extend it.

## Viewer impact

Automation does not change the clip, but two things a viewer feels are set here
rather than at export time:

- **The first line.** Only the opening characters survive truncation in a feed,
  which is why a title is required and why per-platform captions exist.
- **The cover frame.** TikTok uses the first frame unless the draft is finished
  with a different one, so the inbox step is where the hook is chosen.

No retention claim is made. Volume does not create reach: since September 2026
TikTok keeps unoriginal clips out of the For You feed and YouTube reduces
impressions for channels that mostly repost. Publish what you have the right to
share.
