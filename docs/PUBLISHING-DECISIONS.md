# Publishing decisions

Why the publishing component looks the way it does. Each entry is a decision
that constrains the code, written so a change can be made deliberately rather
than by accident. Read this before changing behaviour in
[`src/streamframe/publish/`](../src/streamframe/publish).

For the operational guide, see [PUBLISHING.md](PUBLISHING.md).

## Invariants

These hold today. Breaking one is a regression, not a trade-off.

1. **Publishing never fails a render or a capture.** Every publish path is
   inside a try/except and records an outcome.
2. **A clip never publishes twice.** The ledger claims `(content hash, platform)`
   before any network call.
3. **A clip never publishes under an auto-generated filename.** Manual and
   automatic paths both require a title.
4. **No credential is written inside the repository.** Client secrets live in
   `.env` (git-ignored); tokens live in the state directory at mode 0600.
5. **Only platforms listed in `STREAMFRAME_PUBLISH_PLATFORMS` are offered or
   attempted.** The picker and the worker read the same setting.
6. **`streamframe publish` never starts the API server.** It is a separate
   command, not a server route.

## Decisions

### 1. A separate package, outside the render path

**Context.** Capture, analysis and rendering are long-running and must stay
predictable. Network calls to three platforms are not.

**Decision.** `streamframe.publish` is a self-contained package. `jobs.py` calls
into it; it never imports the reverse direction, and it cannot edit a job, a
candidate or a file.

**Consequences.** Publishing can be tested with fakes, disabled entirely, or run
from a CLI without a server. The cost is a thin adapter layer in `jobs.py`.

### 2. Publish only from a committed export

**Context.** A render can be interrupted, and an export is not ready just because
a request was accepted.

**Decision.** The publisher takes a filename that already exists in
`job["exports"]`. It never renders.

**Consequences.** Failures are about delivery, never about missing footage. A
re-render is a new clip, because the bytes differ.

### 3. Content-hash ledger, claimed before the network

**Context.** Retries, double clicks, cron overlap and crashes are all normal.

**Decision.** One SQLite row per `(sha256(file bytes), platform)`, claimed with
`BEGIN IMMEDIATE` before the first request. States: `in_flight`, `published`,
`needs_manual`, `failed`. Stale `in_flight` rows recover after 15 minutes;
`failed` retries at most three times.

**Consequences.** Publishing is at-least-once across crashes but exactly-once in
practice: a killed process can re-attempt, but a completed post is never repeated.
Identity is content, not a job ID, so two renders of the same moment are two
clips. `--clip-key` overrides when that is wrong.

### 4. Client secrets in `.env`, tokens in the state directory

**Context.** A team shares configuration; a machine holds credentials.

**Decision.** App IDs and secrets go in `.env`. OAuth tokens go in
`<state dir>/credentials.json`, mode 0600, written by `publish auth`.

**Consequences.** `.env` can be passed around without granting posting rights.
Each machine authorizes separately. `STREAMFRAME_PUBLISH_STATE_DIR` exists
because the launcher serves from `./.streamframe` while the CLI defaults to
`~/.streamframe`; pin it so both share one token and one ledger.

### 5. One worker, per-platform isolation

**Context.** Platform rate limits are per account and unforgiving; a dead
platform must not stall the others.

**Decision.** A single background thread drains a queue sequentially. Inside, each
platform is attempted in its own try/except and recorded separately.

**Consequences.** One publish at a time per process keeps us inside TikTok's
per-minute limit and the daily caps. Throughput is bounded by the slowest
platform (Instagram processing). A hung request blocks the queue until its
timeout, which is the intended trade.

### 6. TikTok publishes drafts, not posts

**Context.** Public TikTok posting requires an audited app, and the audit
explicitly rejects utilities that upload to accounts the operator manages.

**Decision.** Use `video.upload` (inbox drafts). Record the outcome as
`needs_manual`, a terminal state.

**Consequences.** No audit, no cost, but one human step per video and a five-draft
pending cap. A re-run will not upload a second draft. `needs_manual` must not be
retried automatically.

### 7. Instagram pulls by URL, so hosting is pluggable

**Context.** The Reels endpoint takes `video_url` and fetches the file itself.
There is no bytes-upload variant on the Instagram-login path.

**Decision.** `publish/media.py` defines a tiny host interface (`put`, `remove`)
with three implementations: `catbox` (default), `local` (a directory you already
serve), `r2` (S3-compatible).

**Consequences.** `catbox` was chosen as the default because R2 requires billing
details even on its free tier, which blocks a zero-cost setup. The trade is a
third-party dependency: Litterbox holds the file for an hour and serves
`video/mp4` with range support, and the file is public at an unguessable URL for
that window. Moving to `local` or `r2` is configuration, not code.

### 8. Instagram uses Facebook login with a Page

**Context.** Meta removed the standalone "Instagram login" product for new apps,
leaving Unified login (business portfolio + integration system user) or Facebook
login (Instagram account linked to a Page).

**Decision.** Implement the Page-linked variant behind
`STREAMFRAME_INSTAGRAM_LOGIN=facebook`, and keep the standalone flow selectable
as `instagram` for older apps.

**Consequences.** The flow is a normal user-token exchange followed by
`me/accounts`, so it can be implemented and tested without a system user. It
requires a Page link, and the Page access token has no scheduled expiry. Unified
login would need a second, different credential model; it is not implemented.

### 9. An optional explicit Page ID

**Context.** `me/accounts` can return no Pages even when the account can use the
Page, for example when a business portfolio owns it.

**Decision.** `STREAMFRAME_INSTAGRAM_PAGE_ID` skips discovery and reads that Page
directly.

**Consequences.** A deterministic escape hatch with no code change. The failure
messages distinguish "no Pages returned", "Page has no linked Instagram account",
and "this ID is not a Page".

### 10. A title is required; a filename is not a title

**Context.** Publishing a clip called `80a7e2f9….mp4` is worse than not
publishing.

**Decision.** The editor pre-fills from the opening title or source title only,
leaves the field empty otherwise, and disables upload. The API rejects a missing
or blank title.

**Consequences.** Automatic publishing therefore needs a title too: either
`publish_title` per job or an explicit auto-export title. A job without one is
rejected at creation rather than publishing badly.

### 11. Public is the default privacy

**Context.** The tool exists to publish; unlisted-by-default produced surprise.

**Decision.** `STREAMFRAME_YOUTUBE_PRIVACY` defaults to `public`, the editor's
privacy selector defaults to the configured value, and the form warns when the
choice is public.

**Consequences.** A first-time test is visible. Use `unlisted` or `private`
explicitly while validating, and remember Instagram has no unlisted state.

### 12. The server publishes; the CLI does not start it

**Context.** Auto-publishing has to happen while the server runs, and the CLI must
not spawn a second server as a side effect.

**Decision.** `publish run` is handled before API URL validation. Auto-publishing
lives in the job store's worker.

**Consequences.** A CLI publish and a server publish share one ledger, so they
cannot duplicate. `publish` works with no server at all.

### 13. Tests never read a real `.env`

**Context.** Once a checkout has real credentials, tests that build `Settings`
change their results.

**Decision.** An autouse fixture points `STREAMFRAME_PUBLISH_ENV` at an empty
file. Any test that needs credentials constructs `Settings` directly.

**Consequences.** `pytest` behaves the same on a fresh clone and on a configured
machine. A test that wants discovery deletes the variable first.

## Extension points

**Add a platform.** Implement a client with `upload` (or `publish`, or
`upload_draft`) returning a `PublishResult`; add the name to
`jobs.PUBLISH_PLATFORMS` and `settings.PLATFORMS`; dispatch it in
`runner.Publisher._dispatch`; add console links in `cli.CONSOLE_LINKS`; add a
section to `.env.example` and the settings dataclass. Nothing else needs to know.

**Add a media host.** Implement `put(path, key) -> PublishedMedia` and
`remove(key)`; add the name to `MEDIA_HOSTS` and a branch in `build_host`.

**Change delivery behaviour.** The ledger is the only place that decides whether a
clip is sent. Anything else (title, privacy, platforms) is per-call data.

## Known limitations

- No scheduler of its own. Auto-publish needs the server running.
- No delete or visibility update after publishing; that needs broader scopes
  (`youtube` rather than `youtube.upload`), and is deliberately out of scope.
- Instagram has no unlisted/private option; a published Reel is public.
- TikTok keeps one human step, and only five drafts may be pending.
- No analytics, insights or comment handling.
- `catbox` is a volunteer-run service; it is the default for cost reasons, not
  durability.
