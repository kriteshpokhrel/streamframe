# StreamFrame local editor

React + TypeScript frontend. No processing is mocked: jobs, edits, media, and
exports use the local backend.

## Run and integrate

From `frontend/`, install the pinned packages with your preferred package manager,
then run `npm run dev`. Vite proxies `/api` to `http://127.0.0.1:8000`; adjust
`vite.config.ts` if your backend listens elsewhere. `npm run build` type-checks
and writes the static application to `frontend/dist/`. Serve that directory from
the backend at `/`, preserving `/api/*` routes. Sources, the import form,
review (with source and moment IDs), live monitoring, and exports use hash
routes so browser Back and deep links work without a server-side fallback.
No external fonts, analytics, or services are used.
The development proxy preserves the browser Host header (`changeOrigin: false`)
and does not enable forwarded headers, so same-origin checks see the Vite origin.
If deploying behind another proxy that rewrites Host, configure the backend's
explicit allowed loopback origins for development rather than disabling checks.

## Contract assumptions

- IDs are strings and URL-encoded when used as path segments.
- `GET /api/jobs` returns `Job[]` (also accepts `{ jobs: Job[] }`).
  `GET /api/jobs/{id}` and `POST /api/jobs` return a job object, with `id` required
  from creation. The primary workflow is a Twitch username or channel/stream link.
  YouTube links select the platform automatically; YouTube @handles require
  explicitly selecting YouTube.
  Source selection offers `auto` (live first, otherwise latest finished), `live`,
  and `latest`. Import submits
  `{ "source": { "type": "streamer", "platform": "twitch", "identifier": "name",
  "mode": "auto", "duration": 60 } }`; duration must be 1–300 seconds.
  Retrieval is automatic and bounded: users do not manually download videos.
  Guarded HLS is supported; YouTube VOD ranges require a finished, clear,
  muxed MPEG-TS HLS archive with a verified complete timeline. Most YouTube
  VOD formats and other non-HLS bounded retrieval fail explicitly.
  Protected/authenticated streams are unsupported. No silent full-download
  fallback is promised. Live capture is limited to 512 MiB transfer and 512 MiB
  combined temporary/output disk. Twitch VOD retrieval is limited to 512 MiB,
  1200 segments, and 10 minutes; output must be below 2 GiB. Local range encoding
  has a 15-minute deadline and output below 2 GiB. These are enforced backend
  limits, not invented user-configurable quotas.
  For a specific finished-video range, import submits
  `{ "source": { "type": "url", "url": "https://…", "start": 60, "end": 120 } }`
  or `{ "source": { "type": "file", "path": "/absolute/path" } }`.
  The import form accepts original-video times as `HH:MM:SS`, `MM:SS`, or
  seconds (including fractional seconds), and converts them to API seconds.
  Local sources may include optional numeric `start` and/or `end` import bounds
  in original-file seconds. End-only starts at 0; start-only ends at start + 1800
  seconds or EOF; explicit bounds require `0 ≤ start < end` and at most 1800
  seconds. Omitting both imports the full local file. URL sources require both
  bounds and at most 1800 seconds; live channels, playlists, authentication, and
  unavailable/unsafe video formats are not silently worked around.
  The source is a URL or a path accessible
  **to the backend**, not a browser file upload.
- Job fields: `id`, `source`, `status`, numeric `progress`, `candidates`, optional
  `error`. Source is the same discriminated object sent on import (legacy strings
  also display). Progress accepts fractions 0–1 or percentages above 1–100 (1 means
  100%). Status is displayed verbatim; cancel/resume are exposed without guessing
  the backend's status enum. Both actions POST an empty body and accept JSON
  or HTTP 204 responses.
- Optional `resolved_source: { url, platform, title, is_live, selection }` and
  `capture_started_at` are displayed only when supplied by the backend. The UI
  never infers live status from a username or selection mode. Active capture
  offers **Stop capture** through the existing cancel endpoint. Streamer capture
  and range-import timestamps are relative to the retrieved media, not wall time.
- Jobs poll every 2.5 seconds, health every 15 seconds. Health is based on the
  HTTP status, not a particular JSON shape. Failed polls retry automatically.
- Candidate fields: `id`, `start`, `end`, `score`, `evidence`, `words`,
  optional `face_crop` and `content_crop`. Score is shown as the raw numeric
  backend score; evidence can be a string, array, or object.
- All editor timestamps are seconds relative to the **imported source**, including
  word timestamps. Words are `{ start, end, text, correction?, ...metadata }`;
  for bounded imports this means zero-based relative to the trimmed range,
  not the original file. The job summary explicitly shows original-file bounds.
  The confirmed backend supports optional `correction` strings. Edits preserve
  recognized `text` and set `correction`; the UI shows original text and allows
  restoring it. New words use entered caption text as their initial `text`.
  Extra metadata is preserved. Words outside the selected clip stay in the payload and are
  marked in the editor. The backend is responsible for filtering/clipping captions
  to the export range. Caption preview displays the words active at the playhead.
  Correcting captions does not change original speech/audio. Users are reminded
  to obtain permission and check source attribution before sharing.
- Crops are normalized source-frame `{ x, y, width, height }`. Disabling an
  existing crop PATCHes `null`, meaning use backend defaults. Guides show source
  regions, not an exact simulation of the backend's final composite.
- Candidate PATCH sends **only changed fields** among `start`, `end`, `words`,
  `face_crop`, `content_crop`. It accepts a JSON or 204 response, then re-fetches
  the persisted job. Local unsaved edits survive polling. Concurrent server
  changes to locally edited fields are flagged; saving is last-write-wins because
  the supplied contract has no version/ETag precondition.
- `GET /api/jobs/{id}/media` must serve browser-decodable video with correct MIME
  type and byte-range support for seeking. Native controls are available in
  **Source video**; **Play clip** bounds playback to the selected range from
  either view. The preview format and correction tools share one player.
  **Portrait framing** is not the final render. Open **Export history** or
  **View exports** to watch the finished clip.
- Export POST has an empty body and uses saved candidate data. The preferred
  JSON response is `{ "filename": "clip.mp4" }` or
  `{ "files": [{ "filename": "clip.mp4" }] }`. Arrays, `exports`/`outputs`
  collections, same-origin `/api/` `url`/`download_url`, and direct video/ZIP
  responses are also accepted. Filenames map to
  `GET /api/jobs/{id}/exports/{filename}`.
  A 202 job response is supported; its existing exports are displayed immediately,
  and polling discovers completed outputs. Async 204/no-filename responses require output filenames to later appear in
  `job.exports`/`job.outputs` or `candidate.exports`/`candidate.outputs`; these
  optional extensions are not guaranteed by the supplied contract. Export POST
  alone cannot identify a download without a filename, URL, or binary response.
- Error responses can be text or JSON with `detail`, `error`, or `message`.
  Same-origin cookies use browser fetch defaults; no auth token UI is supplied.
- `DELETE /api/jobs/{id}` removes a stopped job and its StreamFrame-owned
  artifacts after confirmation. Original local files are not removed. Active
  jobs and exports must stop first.

## Validation

After dependencies are installed, run `npm run build`. Run `npm test` with Node
22.6+ (native TypeScript stripping) for export-mapping regression tests. Job-level
exports are shown only when `candidate_id` matches the selected candidate;
unassigned job exports are not guessed from filenames. Candidate-scoped outputs
may omit this identifier. Both polling and asynchronous export POST job responses
use this mapping.

For an integration smoke
test: import a backend-readable video; observe progress and candidates; play a
clip; edit a word, its timestamps, clip bounds, and both crop rectangles; save;
reload and confirm persistence; export and download; exercise cancel/resume and
backend-offline recovery. All editor inputs are labeled and keyboard accessible.

### Real browser / backend / renderer regression

Prerequisites: installed frontend dependencies (including Playwright `1.58.2`),
the repository's Python API/analysis dependencies in `.venv`, cached Whisper
`small`, the repository YuNet model, macOS `say` for generated speech, and
`ffmpeg`/`ffprobe` with the libass `ass` filter for real corrected-caption exports.
The test uses installed Homebrew `ffmpeg-full` when available (the default macOS
FFmpeg here lacked libass). Override with `STREAMFRAME_TEST_FFMPEG` and
`STREAMFRAME_SAY`, or choose a cached model via `STREAMFRAME_WHISPER_MODEL`.
This test never downloads models, media, fonts, or browser binaries.
It uses installed macOS Google Chrome when available, otherwise Playwright's
already-installed Chromium. Set `STREAMFRAME_CHROME` to another installed Chrome
executable or `STREAMFRAME_TEST_PYTHON` to the backend Python interpreter.

```sh
cd frontend
npm run build
npm run test:browser
```

Two browser scenarios generate original five-second test-pattern media: one
uses sine audio and an explicitly deterministic analyzer fixture; the other
generates speech with `say` and runs the actual local Whisper transcription,
visual analysis, YuNet discovery, and ranking functions. A small fixture adapter
maps real analysis output into the API candidate schema, without faking results.
Both start the real API on an ephemeral loopback port and serve the production
editor. Import/range trimming, PATCH persistence, playback, crop controls,
caption corrections, rendering, HTTP downloads, and FFprobe validation are real.
The tests check reload persistence, candidate-specific exports, source status,
streamer duration limits, URL range validation, and cancellation. The cancellation
scenario uses a deliberately waiting analyzer, not a fake HTTP response. No
live-provider integration is claimed by this offline regression.
All browser requests outside the test origin
are blocked and fail the test. Fixture/profile/download files are created inside
`frontend/.browser-test-*` and cleaned up with the browser and server on exit.
No existing jobs or user media are modified.
