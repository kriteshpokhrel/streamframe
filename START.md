# Start StreamFrame

Use this guide on a clean checkout or hand it to a local setup agent. Commands
are for a POSIX shell and run from the repository root unless noted.

## 1. Check prerequisites

- **Python 3.10**, with `venv` and `pip`. Use `python3.10` below, not an
  unverified `python3`. If your executable is named differently, verify
  that it reports 3.10 before substituting it.
- **Node.js 22** (22.6 or newer for the frontend tests), with npm.
- **FFmpeg and ffprobe** in the same directory. FFmpeg needs the `libx264`
  and `aac` encoders and the `ass` filter (a build with libass), plus the
  ability to decode your input. Having an `ffmpeg` executable alone is not
  enough. Use host fonts with the characters your captions need.

```sh
python3.10 --version
node --version
npm --version
```

Check the FFmpeg build you intend to use (replace `ffmpeg` and `ffprobe`
with their full executable paths if needed):

```sh
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -encoders | grep -E '[[:space:]](libx264|aac)[[:space:]]'
ffmpeg -hide_banner -filters | grep -E '[[:space:]]ass[[:space:]]'
```

Look for both encoders and a line for `ass`. If they are missing, select a
compatible build rather than disabling captions or substituting another codec.

## 2. Install the locked dependencies

For a setup agent: this setup authorizes the project-local installs below
from the pinned manifests. Reuse an existing compatible environment where
possible. It does not authorize global installs, shell-profile changes,
account credentials, a model download, or running a capture.

```sh
python3.10 -m venv .venv
.venv/bin/python -m pip install 'pip==25.3'
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
npm --prefix frontend ci
npm --prefix frontend run build
```

Python packages go into `.venv`; npm uses `frontend/package-lock.json` and
installs into `frontend/node_modules`. The build creates `frontend/dist`,
which the API serves as the editor. Package installation requires registry
access unless the packages are already available to your package managers.
Do not silently substitute unpinned versions when an installation fails.

## 3. Make the local models available

YuNet face detection is bundled in `models/`. To use another compatible
local model file, set `STREAMFRAME_YUNET_MODEL_PATH`.

Transcription defaults to faster-whisper `small` on CPU (`int8`). The default
lookup checks the standard Hugging Face cache under `~/.cache/huggingface/hub`.
For a custom cache location or a separately supplied model, set
`STREAMFRAME_WHISPER_MODEL` to the existing model snapshot directory.
StreamFrame does **not** download model weights implicitly. To check for
the default model's runtime files without contacting the network:

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
from streamframe.analysis import _resolve_local_whisper_model

snapshot = Path(_resolve_local_whisper_model("small", None))
for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
    file = snapshot / name
    if not file.is_file() or file.stat().st_size == 0:
        raise SystemExit(f"Missing or empty Whisper model file: {name}")
print("Default Whisper small model files found.")
PY
```

If missing, with approval for network and disk use, explicitly download the
model into the same environment's Hugging Face cache:

```sh
.venv/bin/python -c 'from huggingface_hub import snapshot_download; print(snapshot_download("Systran/faster-whisper-small"))'
```

If the download uses a custom cache, use the printed snapshot directory for
`STREAMFRAME_WHISPER_MODEL`. The file check does not exercise transcription.
An agent should not report transcription ready if neither a complete cached
model nor a valid local directory is available. See the
[third-party notices](README.md#third-party-notices).

## 4. Launch and verify

```sh
./streamframe
```

Keep that terminal open and go to **<http://127.0.0.1:8765>** in a browser on
the same computer. `./streamframe` defaults to `serve`, uses the checkout's
`.venv`, and serves the production frontend and API on the same origin.
It prefers `STREAMFRAME_FFMPEG_PATH` (or `STREAMFRAME_FFMPEG`), then the
Homebrew `ffmpeg-full` build if installed at the launcher's recognized
location, then `ffmpeg` on `PATH`. For an override, set
`STREAMFRAME_FFMPEG_PATH` to the full executable path, not its containing
directory; put the matching `ffprobe` beside it. The launcher adjusts `PATH`
only for its own process and children.

From a second terminal, check the API and editor separately:

```sh
curl -fsS http://127.0.0.1:8765/api/health
curl -fsS http://127.0.0.1:8765/ >/dev/null
```

Health returns `{"status":"ok"}`. This does not test the model, FFmpeg export,
or access to a provider. Don't start a second instance on the same port or
with the same job directory. Stop your foreground launcher with Ctrl-C when
you are done. The server is loopback-only; do not expose or forward it as a
public service.

## 5. Capture, review, and export

From **Add a video**, choose the matching source type:

| Source | What to do |
| --- | --- |
| Twitch channel | Choose **A streamer or channel**. Enter a Twitch username or public channel URL. In **Capture options**, use **Auto** for live or the latest verified finished archive if offline, **Live now only** for a broadcast, or **Latest finished video only** for an archive. If an archive is selected without a range, it starts at the beginning and uses the capture duration (or ends sooner at EOF). |
| Public YouTube live | Choose **A streamer or channel** and **Live now only**. Paste the channel or live video URL, or choose **YouTube** and enter its `@handle`. A display name is not a handle. If channel resolution fails, a known public live video URL can avoid channel lookup, but the video itself must still be supported. |
| Specific Twitch or YouTube archive range | Choose **Part of a finished video**. Enter a public Twitch `/videos/` URL or a public YouTube `watch?v=…` URL, and both start and end in original-video seconds. The selected span must be positive and at most 1,800 seconds. YouTube support is limited to finished videos with a verified timeline in the format described below. |
| Local video | Choose **A file on this computer** and enter an absolute path readable by the backend. This is not a browser upload. No bounds imports the full file subject to its size limit; end-only starts at zero; start-only takes up to 1,800 seconds or EOF. A specified range can span at most 1,800 seconds. |

Select **Import** and check the job's resolved source and stage. Single
live samples default to 60 seconds (1–300 seconds) and finish without
waiting for the broadcast to end. For manual export, select a candidate,
review its evidence and preview, adjust timing, crops and captions, then
select **Save changes** and **Export clip**. Play the finished result under
**Exported videos** before downloading or sharing it. The webcam panel
fits the entire selected rectangle, adding padding rather than cutting
off its edges. A suggested rectangle can be an estimated webcam boundary;
when no stable boundary is found, the detector can instead suggest a roomy
head-and-shoulders crop. Check the source guides and adjust the rectangle
if needed: the guides show the selected region, not the final padded
composition. Editor timestamps are zero-based within the retrieved range
or retained source window, not the original video or wall clock.

### Rolling live sessions

Under **Live now only**, choose **Make clips automatically for a short
session** instead of taking one sample. A rolling session defaults to
600 seconds (10 minutes) and can request 1–1,800 seconds (up to 30 minutes).
Windows default to 60 seconds, accept 1–60 seconds, and cannot be longer
than the session.

- A continuous HLS cursor feeds a bounded two-window queue. The session
  stops at its wall-clock deadline, so actual media coverage can be shorter;
  window encoding can also omit a small tail. Follow reported coverage,
  not the planned wall-clock duration.
- At most one candidate is selected per window. No usable candidate means
  no clip. If no face crop is found, the candidate can be retained for
  manual correction without an automatic export. A session crop affects
  future windows only. This is not continuous identity tracking or
  guaranteed duplicate detection; review each export.
- **Stop session** preserves completed clips and drops incomplete work.
  Stopped, failed, or interrupted rolling sessions cannot resume. Completed
  clips with retained source windows can be corrected and re-exported,
  even after stopping or reopening the app. A re-export creates a new file.
  Older clips without retained media are not editable.
- Per session, the limits are **512 MiB transfer** and **512 MiB
  shared media disk**. The disk budget includes queued windows, retained
  sources, and exports including revisions. Transfer covers payload and
  reserves metadata budgets, not HTTP/TLS overhead. Retained windows are
  not silently evicted. The session also caps cumulative media segments
  and processed windows at 900 each. Gaps, discontinuities, slow processing,
  and budget exhaustion can end the session with an error. There is no
  automatic restart or budget reset.

### Source formats and limits

Remote ingest is limited to public, **unencrypted, multiplexed MPEG-TS
HLS** (video and audio together). Public availability alone doesn't
guarantee a compatible rendition. Encrypted streams, separate HLS audio,
discontinuities, and private or protected videos are unsupported.

**YouTube finished-video support is limited.** The video must be public and
finished, with a compatible HLS rendition and a complete, verified timeline.
StreamFrame fetches only the bounded set of media segments needed for the
requested range. Most YouTube VOD formats are unsupported: there is no
DASH, direct MP4, or whole-video fallback. A broadcast marked `post_live`
is still processing and is explicitly unavailable. Once processing finishes,
the video still needs to offer a compatible rendition and timeline.

For channel lookup, Auto falls back only when the live probe establishes
that the channel is not live, not when metadata access fails. Latest
examines at most 20 archives and requires evidence that a VOD is finished.
Twitch VOD retrieval has a 512 MiB transfer budget, 1,200-segment cap and
10-minute deadline. Single live capture has 512 MiB transfer and combined
media disk limits. The local-file allowlist is `.mp4`, `.mov`, `.mkv`,
`.webm`, `.m4v`, `.avi`, `.mpeg`, and `.mpg`; actual decode and browser
playback still depend on codecs. Full-file copies are limited to 2 GiB;
local range encoding has a 15-minute deadline and must output less than 2 GiB.

## Troubleshooting and shareable diagnostics

| Symptom | Check |
| --- | --- |
| Launcher can't find Python or FFmpeg | Check `.venv/bin/python` and the selected FFmpeg directory. Re-check the encoders and `ass` filter before exporting. Use `STREAMFRAME_FFMPEG_PATH` if the intended build is not on `PATH`. |
| Connection refused, address in use, or root page missing | Read the foreground launcher output. If health works but `/` doesn't, build `frontend/dist` using the npm command above. If the port is occupied, identify the process rather than killing an unknown listener. `./streamframe serve --port 8766` is an alternative loopback port if no other instance is using the same data directory; use the matching browser URL and CLI `--api-url`. |
| Source resolution fails, or starts but won't capture | Check platform, source type, live status and format. If a YouTube archive is still processing (`post_live`), wait until it finishes; that does not guarantee a supported format. Treat sign-in and bot-verification prompts as unsupported; no cookies, alternate accounts, or bypasses are needed or supported. A 403, rate limit, timeout, or extractor incompatibility is not proof the channel is offline. Try again later for temporary provider issues or use an authorized, compatible source. |
| Missing Whisper model or no captions | Check the local model for the same environment as the server. Speech-free clips can legitimately have no words; the app does not invent captions. |
| No face crop, empty candidate list, or automatic export skipped | Try a clearer compatible source or a manual crop on an editable candidate; if appropriate, set a crop for future windows. A missing candidate is not an export failure. |
| Source file rejected | Use a readable absolute path under the configured import roots (default: your home directory) and an allowed extension. Restrict access with `STREAMFRAME_IMPORT_ROOTS`, an OS-path-separated list of allowed roots. |
| Session ended early or revision was rejected | Inspect the job's stage, coverage, and budget notices. Use a shorter capture next time. Previously completed exports are preserved; the app does not evict their retained sources to make room. |

In the job details, use **Copy diagnostics**. If the clipboard is unavailable,
the editor shows text to copy manually. A local agent can also get the
sanitized JSON report with
`curl -fsS 'http://127.0.0.1:8765/api/jobs/JOB_ID/diagnostics'`.
Reports use allowlisted fields: job/status/stage, error categories, selected
public source fields, tool versions, and recent UTC transitions. Local paths,
URL query strings, and raw exception text are omitted. The report can still
identify a public channel, a job, and times; review it before sharing and
remove anything you don't want to disclose. Don't send raw logs, full job
JSON, environment dumps, browser cookies, signed media URLs, or media as a
substitute.

## Privacy, updates, and removal

Jobs, media, edits, and exports stay local by default in `.streamframe/`
(SQLite database `jobs.sqlite3` and per-job files), or in the directory
selected by `STREAMFRAME_DATA_DIR`. Non-rolling analysis may also cache
transcripts under `~/.cache/streamframe`; deleting a job does not clear
that separate cache. Keep both locations private. Remote
sources contact the provider and media hosts; the explicit model download
contacts Hugging Face. StreamFrame runs local analysis and rendering, doesn't
upload clips or transcripts for processing, and has no paid API or account
requirement. Expect CPU load, disk use, electricity and network transfer;
session limits are not a cap on all jobs combined. The pinned extractor is
run without external yt-dlp configuration or browser cookies.

- **Update a checkout:** Stop active jobs before an approved maintenance
  window and back up the effective data directory with the app stopped.
  For updated Python manifests, rerun the pinned installs in `.venv` and
  `pip check`; if stale dependencies remain, recreate only `.venv`. For a
  changed npm lockfile run `npm --prefix frontend ci`; rebuild
  `frontend/dist` after frontend updates. Don't upgrade yt-dlp globally or
  override the lock to work around provider changes. Preserve the data
  directory and model cache unless intentionally removing them.
- **Delete a job:** Stop the job if active and wait for any active or queued
  exports to finish. Use **Delete** beside that job in **Source library** and
  confirm. It removes the job's StreamFrame-owned copies, retained windows
  and exports, not the original file or any export saved separately. If
  deletion is incomplete, fix the reported issue and retry **Delete**.
- **Remove the local installation:** After stopping the app, remove only
  the checkout's `.venv`, `frontend/node_modules` and `frontend/dist` to
  remove generated setup files. If you also want to erase all local jobs,
  back up anything you want to keep and verify the exact effective data
  directory before deleting it. If removing cached transcripts, target
  StreamFrame's cache separately, not the entire user cache. A model in
  the Hugging Face cache can be shared with other apps; leave it alone
  unless you intentionally remove that specific model separately.

See the [README](README.md) for CLI examples, test commands, and architecture.
