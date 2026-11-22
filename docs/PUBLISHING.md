# Publishing clips

StreamFrame renders clips. This component posts them. It is deliberately
separate from capture and rendering: nothing here can start a job, edit a
candidate, or delete footage.

| Platform | What happens | Human step |
| --- | --- | --- |
| YouTube | Uploaded as a Short, public or unlisted | none |
| Instagram | Published as a Reel | none |
| TikTok | Delivered as a draft to the account inbox | finish it in the app |

TikTok is a draft because public API posting needs an audited app, and TikTok's
audit rejects tools that upload to accounts you manage. The draft route
(`video.upload`) needs no audit and no payment. TikTok discards an unfinished
draft after 24 hours, and only five drafts may be pending at once.

This repository still ships no scheduler. Run `streamframe publish run` from
cron, a CI job, or by hand.

## Cost

Zero. YouTube's default quota is 100 uploads per day. Instagram's is 50 per day.
TikTok allows about 15 posts per day per account, of which roughly five can be
pending drafts. The only optional cost is media hosting for Instagram, and
Cloudflare R2's free tier covers it.

## 1. Configure `.env`

```sh
./streamframe publish setup --write-env
```

That creates `.env` with mode 0600 and prints the next console step for each
platform, with links. Re-run `./streamframe publish setup` after each step; it
reports what is still missing and exits non-zero until everything is ready.

To do it by hand instead: `cp .env.example .env`.

`.env` is git-ignored. It holds the client secrets that can post to your
accounts, so share it through your password manager, never through the
repository. OAuth tokens are **not** stored there; they are written to
`~/.streamframe/publish/credentials.json` with mode 0600.

Keep `STREAMFRAME_PUBLISH_PLATFORMS` set to the platforms you have actually
configured. Anything listed but unconfigured shows up in `doctor` as a problem.

## 2. Register three developer apps

All three apps need a Terms and Privacy page on a public URL. Create one page
per app or reuse a single page.

### YouTube

1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen as **External**, then add your own Google
   account as a **test user**. You do not need to publish the app for personal
   use; an unverified app is limited to 100 test users and shows a warning
   screen, which is fine here.
3. Create an OAuth client of type **Desktop app** and copy the client ID and
   secret into `.env`.
4. Redirect URI: `http://127.0.0.1:8766/callback` (the default; Google allows
   loopback).

### Instagram

1. The account must be a **Business or Creator** account.
2. In the Meta app dashboard create an app and add
   **Instagram → API setup with Instagram login**.
3. Copy the **Instagram app ID** and **Instagram app secret** into `.env`.
4. Add your HTTPS callback as a redirect URI and pass it with
   `--redirect-uri`. Meta does not reliably accept loopback redirects, so this
   flow uses a paste-the-code step.

You do **not** need App Review or Business Verification while the app only posts
to accounts you own or manage. That changes the day you post for someone else.

New Meta apps are no longer offered the standalone "Instagram login" product,
so set `STREAMFRAME_INSTAGRAM_LOGIN=facebook` and link the Instagram account to
a Facebook Page. That path uses `graph.facebook.com`; the standalone variant
(`instagram`) remains for older apps.

### TikTok

1. Create an app at `developers.tiktok.com`, add **Login Kit** and
   **Content Posting API**, and enable the **Upload** configuration only.
2. Request the `video.upload` scope. Do not request `video.publish`; it requires
   the audit this design avoids.
3. Put the client key and secret in `.env` and register an HTTPS redirect URI.

### Which secrets go where

| Value | Where | Why |
| --- | --- | --- |
| Client IDs and secrets | `.env` | shared by the team, rotates rarely |
| Access and refresh tokens | `~/.streamframe/publish/credentials.json` | per machine, rotates constantly |
| Publication history | `~/.streamframe/publish/publications.sqlite3` | the idempotency ledger |

## 3. Authorize this machine

```sh
./streamframe publish auth youtube
./streamframe publish auth instagram --redirect-uri https://example.com/callback
./streamframe publish auth tiktok    --redirect-uri https://example.com/callback
```

YouTube opens a browser and listens on loopback. Instagram and TikTok print a
URL; approve it, then paste the callback URL (or just the code) back into the
terminal. Nothing publishes during authorization.

## 4. Give Instagram a public URL

Meta downloads a Reel from a URL, so pick one free host in `.env`:

```sh
# a directory you already serve over HTTPS
STREAMFRAME_MEDIA_HOST=local
STREAMFRAME_MEDIA_LOCAL_DIR=/srv/clips/public
STREAMFRAME_MEDIA_PUBLIC_BASE_URL=https://clips.example.com

# or Cloudflare R2 / any S3-compatible bucket
STREAMFRAME_MEDIA_HOST=r2
STREAMFRAME_R2_BUCKET=streamframe-clips
STREAMFRAME_R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
STREAMFRAME_R2_ACCESS_KEY_ID=...
STREAMFRAME_R2_SECRET_ACCESS_KEY=...
STREAMFRAME_MEDIA_PUBLIC_BASE_URL=https://clips.example.com
```

Clips are uploaded just before publishing and removed afterwards. YouTube and
TikTok need no hosting because they accept the file bytes.

## 5. Check, then publish

```sh
./streamframe publish doctor          # config + credentials, exit 1 if incomplete
./streamframe publish run --file out/clip.mp4 --caption "..."      # dry run
./streamframe publish run --file out/clip.mp4 --caption "..." --yes  # real
./streamframe publish status
```

`run` without `--yes` is a dry run and publishes nothing. This is intentional:
the command is easy to put in cron by mistake.

## Upload from the editor

Every finished clip under **Exports** has an **Upload to** block:

- Checkboxes for YouTube, Instagram and TikTok, **pre-selected for every
  configured platform**, so the default is one click.
- A **Title** field, required. It is pre-filled from the clip's opening title or
  the source title; if neither exists it is left **empty** rather than falling
  back to the filename, and the upload button stays disabled until you type one.
- A **YouTube privacy** choice, defaulting to *public*.
- Platforms that are not enabled or not configured appear as *unavailable*,
  with the missing setting in the tooltip, and cannot be selected.
- Results appear one row per platform: `published`, `in the TikTok inbox`, or
  `failed` with the platform's reason.

Each platform is attempted independently. An Instagram rejection never stops
the YouTube upload, and a failed platform does not stop the button from being
used again. Clicking twice is harmless: the publish ledger sees the same clip
and reports `already published` instead of posting again.

The API behind it, if you are scripting:

```sh
curl -X POST localhost:8765/api/jobs/JOB_ID/exports/FILE.mp4/publish \
  -H 'Content-Type: application/json' \
  -d '{"platforms":["youtube","instagram"]}'      # omit platforms for all
```

It returns `202` at once; outcomes land in the job's `publish_results`.

## End-to-end automation

Once the setup is done, you do not run `publish run` at all. Turn publishing on
and every finished export posts itself.

**App level** — every new job:

```sh
STREAMFRAME_PUBLISH_AUTO=1
```

**Per source** — override it for one job only:

```sh
./streamframe submit clip.mp4 --auto-publish --publish-title "The hook" \
  --publish-platforms youtube,instagram --publish-privacy public
./streamframe streamer name --mode live --rolling --auto-publish \
  --publish-title "Live clips"
./streamframe submit clip.mp4 --no-auto-publish   # opt one source out

# or through the API
curl -X POST localhost:8765/api/jobs \
  -d '{"source":{...},"auto_export":{"title":null},"auto_publish":true,
       "publish_platforms":["youtube"],"publish_privacy":"public",
       "publish_title":"The hook"}'
```

The **Add source** screen exposes the same settings: *Upload finished clips to
social platforms*, with platform pills, a title and a privacy choice. It is
visible for every source type, because rolling sessions publish each window as
it is exported.

A title is required for automatic upload. A clip is never published under an
auto-generated filename, so the form refuses to submit without one.

The per-source flag wins, and the resolved value is stored on the job as
`auto_publish`, so a job is self-describing after the fact.

Once a job has `auto_publish`, **every** export it produces is published —
manual exports, automatic exports, and each rolling live window as it is
rendered. That is what makes a live session end to end: capture → clip →
export → publish, unattended.

How it behaves:

- **Separate worker.** One background thread with its own queue. It never takes
  a render slot, and a publish failure is recorded on the job, never returned to
  the capture pipeline. A dead platform cannot fail a render.
- **Sequential on purpose.** One publish at a time per process, which keeps you
  inside TikTok's 6-requests-per-minute and the per-account daily caps.
- **Idempotent.** The content hash ledger means a re-queued or restarted clip
  publishes once.
- **Visible.** Each outcome lands in `job["publish_results"]`:
  `{platform, action, url, detail, filename, at}`.

```sh
./streamframe status JOB_ID | grep publish_results
```

The caption and title come from the clip's opening title, falling back to the
imported source title, then the filename. Set an opening title per source when
you submit, or use `publish run --caption` when publishing by hand.

TikTok still lands as an inbox draft. That is the one remaining hand-off, and
it is where you choose the cover frame.

### Captions

The first line is what survives truncation, and it is rarely the same sentence
everywhere. Pass per-platform text when you need it:

```sh
./streamframe publish run --file clip.mp4 \
  --title "The four-second rule" \
  --caption "default caption" \
  --platforms youtube,instagram \
  --yes
```

Instagram and TikTok read `--caption`. YouTube reads `--title` and
`--description`, and `#Shorts` is added automatically.

## Idempotency

Each clip is keyed by the SHA-256 of its bytes. One row per (clip, platform) is
claimed **before** any network call, so:

- re-running the same command publishes nothing the second time;
- two processes racing publish once;
- a crashed run is recovered after 15 minutes, not left stuck forever;
- a failed publish is retried up to three times, then needs `publish retry`.

Re-rendering the same moment produces different bytes, so it is a new clip.
Override with `--clip-key` when you want to treat several renders as one.

`needs_manual` is a terminal state for TikTok. A second run will not upload a
second draft.

## Running unattended

With `STREAMFRAME_PUBLISH_AUTO=1`, there is nothing to schedule for publishing:
the server publishes each finished export as it lands. That covers the whole
live loop, where a rolling session exports a window every minute or so.

```cron
# refresh tokens monthly; Instagram expires after 60 days
0 3 1 * * cd /srv/streamframe && ./streamframe publish refresh >> publish.log 2>&1
```

If clips arrive from somewhere else — a different machine, a manual folder —
publish the newest one on a schedule instead:

```cron
0 */2 * * * cd /srv/streamframe && ./streamframe publish run \
  --file "$(ls -t exports/*.mp4 | head -1)" --yes >> publish.log 2>&1
```

Alerting matters more than retries: a stale token costs a day of posts silently.
Have cron mail any non-zero exit. Publishing failures are also recorded on each
job, so `publish status` shows what did not go out.

## Viewer impact

Automation does not change the clip, but it does change two things a viewer
feels, so decide them rather than defaulting:

- **The cover frame.** TikTok uses the first frame unless
  `video_cover_timestamp_ms` is set, and that is the frame a person sees while
  scrolling. Finish TikTok drafts in the app with the cover on the hook.
- **The first line.** Only the opening characters survive truncation in the
  feed. Write per-platform captions at export time, not at publish time.

No retention claim is made here. Volume does not create reach, and since
September 2026 TikTok keeps unoriginal clips out of the For You feed and YouTube
reduces impressions for channels that mostly repost. Publish clips you have the
right to share and add something of your own.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `401` / `session expired` | token expired or revoked | `publish auth <platform>` |
| Instagram `HTTP 400` on media | URL not publicly reachable | check `STREAMFRAME_MEDIA_PUBLIC_BASE_URL` from another machine |
| Instagram container `ERROR` | bad codec or aspect ratio | export 9:16 H.264/AAC MP4 |
| TikTok `spam_risk_too_many_pending_share` | five drafts already waiting | publish or discard drafts in the app |
| TikTok `unaudited_client_can_only_post_to_private_accounts` | `video.publish` was requested instead of `video.upload` | fix the app scopes |
| `retry limit reached` | three failed attempts | fix the cause, then `publish retry --clip-key ...` |
| `No such filter: 'ass'` in render tests | FFmpeg built without libass | see `START.md` prerequisites |

## What this component never does

- It does not upload to a platform that is not listed in
  `STREAMFRAME_PUBLISH_PLATFORMS`.
- It does not store credentials in the repository.
- It does not delete or edit clips; it reads an exported file and copies it out.
- It does not run inside the API server. `publish` never starts the server.
