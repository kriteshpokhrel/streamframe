# StreamFrame

StreamFrame is a local video clipping prototype. Give it a supported public
Twitch or YouTube source, or a file on your computer. It retrieves or copies
the media, suggests highlights, and lets you
correct captions and framing before rendering a vertical clip. Rolling live
sessions can render clips automatically, but they do not publish them.

**[Start here: setup, browser workflow, and troubleshooting](START.md)**.
The launcher is `./streamframe` from the checkout; the editor is at
<http://127.0.0.1:8765>.

[Demo video](demo/streamframe-demo.mp4) · [Webcam framing example](demo/webcam-region-fit.png).
The demo composites a licensed portrait and synthetic content with timed
demonstration captions over a generated tone. It is not streamer footage or a
test of speech recognition.

## Quick start

StreamFrame runs locally and requires no paid API. Publishing to YouTube,
Instagram or TikTok is an optional, separate step; see
[publishing](docs/PUBLISHING.md). You
need Python 3.10, Node.js 22, and FFmpeg/ffprobe with `libx264`, AAC and the
`ass` filter. macOS and Linux use the shell commands below; a compatible FFmpeg
build must already be installed. See [START.md](START.md) for capability checks,
other operating-system details, and troubleshooting.

On Windows, run `start.cmd` from Command Prompt in the repository root.
It builds the editor with Windows npm and runs the backend in WSL, not
native Windows Python. See [Windows setup](START.md#windows-one-command-start).

```sh
git clone https://github.com/kriteshpokhrel/streamframe.git
cd streamframe
python3.10 -m venv .venv
.venv/bin/python -m pip install 'pip==25.3'
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
npm --prefix frontend ci
npm --prefix frontend run build
```

Transcription also needs the Whisper `small` model in the local Hugging Face
cache, or a model directory set through `STREAMFRAME_WHISPER_MODEL`.
StreamFrame does not download the model implicitly. If you choose to download
it, this one-time command requires internet access and disk space:

```sh
.venv/bin/python -c 'from huggingface_hub import snapshot_download; snapshot_download("Systran/faster-whisper-small")'
```

Once the model is available, start the app:

```sh
./streamframe
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765), choose **Add source**,
and review a suggested moment. Trim the clip, check framing and captions,
then select **Export clip**. For automated live clips, choose **Clip a channel**,
**Live now only**, and **Make clips automatically for a bounded session**.
Finished files appear under **Exports**, where each clip can be downloaded,
edited, or uploaded to your configured social platforms. Obtain the creator's
permission before sharing.

**Handing setup to an agent?** Give it [START.md](START.md); it includes
explicit setup permissions, verification, privacy limits, and recovery steps.

## Source support

| Source | Current support |
| --- | --- |
| Twitch channel | Live capture or latest finished archive when a compatible public HLS stream is available. |
| Twitch video | A selected range of a compatible finished HLS archive. |
| YouTube channel or live link | Public live streams with compatible HLS; availability is not guaranteed. |
| YouTube finished video | Verified 720p or better, with no SD fallback. **Full video** up to five minutes supports compatible MP4, separate MP4/WebM tracks, or HLS. Partial ranges require clear, multiplexed MPEG-TS HLS with a verified timeline. |
| Local file | A backend-readable file or selected range, subject to the configured import roots. |

Remote media is retrieved and cached automatically within the documented
limits. Private or protected videos, separate HLS audio, encrypted streams,
YouTube DASH and pasted direct media URLs are not supported. Partial ranges
never fall back to downloading the whole video.

## What to expect

- Short live captures and bounded archive imports. Finished YouTube video
  ranges work only with public, unencrypted, multiplexed MPEG-TS HLS and
  a complete, verified timeline, unless the request covers the entire short
  video. Streams still processing after going offline are not ready. See
  [source formats and limits](START.md#source-formats-and-limits).
- [Rolling live sessions](START.md#rolling-live-sessions) default to 10
  minutes and accept up to 30 minutes, with windows of at most 60 seconds.
  The 512 MiB transfer and shared media disk budgets, plus the 900-segment
  and 900-window caps, can end capture earlier.
- Local word-timed transcription, heuristic highlight ranking (including
  quick speech openings and visual cues for speech-free material), webcam-boundary suggestions, and editable
  caption timing and crops. Scores are not probabilities. Face detection is
  not identity verification, and captions are not translations.
- 1080 × 1920 H.264/AAC MP4 exports with split-screen framing and burned-in
  word captions when present. The top 38% preserves the full webcam crop
  without stretching; a divider separates it from the bottom 62% screen
  panel. Detection compares webcam seams across source frames, including
  overlays flush with a screen edge. If it cannot find a stable boundary, trace it
  in Framing before exporting. A webcam edit carries across moments in
  the same imported source and saving applies it to all those moments.
  Existing exports are unchanged until you re-export. Correcting captions
  does not change the audio.

With automatic export enabled in **Add Source**, choose the opening title for
the exported clips: **Use video title** (the default, or the filename for a
local video), **No title**, or **Custom title**. Moments without usable webcam
framing stay in review. The two highest-ranked moments are exported only when
they show mostly different footage; if more than half of the shorter clip is
already covered by the better one, the better clip is exported alone.

### Lobby Crew export preset

The preset ships with the repository in `src/streamframe/assets/lobby-crew`
(`watermark.png`, a transparent wordmark, and `outro.png`, the full logo), so a
fresh clone exports branded clips with no extra setup. New exports from the
editor for non-rolling jobs use the Lobby Crew template automatically. A
lavender line separates the full webcam view from gameplay; flat dark padding
preserves the full webcam view where needed. An optional opening title uses a
solid violet card with a thin lavender outline, centered soft-white text, and
an inset logo. A translucent wordmark appears centered on the divider when the
title clears (or from the start without a title). The 1.5-second animated
outro includes the Instagram, TikTok, and YouTube handles over a blurred
replay of the clip ending. The clip audio fades briefly at the cut. The outro
is silent; it never repeats dialogue or adds music.

Burned-in captions end above Instagram's organic bottom margin (319 px), so
the account row and caption text never cover the last line. Set
`CAPTION_MARGIN_V` in `render.py` to 672 to stay inside Meta's stricter
Reels-ad safe area instead. Titles accept up to 90 characters and are split
into at most three lines, preferring breaks that keep short phrases together.
The card sits a little above the divider so playback controls and account
text at the bottom of a phone screen do not crowd the hook. Emoji render
through the bundled Noto Emoji font in `src/streamframe/assets/fonts`, so the
hook looks the same on machines without a color emoji font.

To use different artwork without editing the repository, put `watermark.png`
and `outro.png` in `lobby-crew` inside the StreamFrame data directory
(`~/.streamframe` by default, or `./.streamframe` when the launcher starts the
server). That directory overrides the bundled artwork for that machine. The
handles are fixed in `src/streamframe/render.py`. The preset does not change
trim points or existing MP4s; rolling capture is unchanged.

## CLI and local automation

From the repository root, with the [local setup](START.md#2-install-the-locked-dependencies)
complete, start the foreground launcher first. Run these commands in a second
terminal from the same directory; substitute real source names and IDs.
`--no-start` prevents commands from launching a second server. Without it, the
CLI can start a local background server if the default API is unreachable.

```sh
./streamframe --help
./streamframe streamer --help
./streamframe --no-start streamer TWITCH_USERNAME --mode auto --duration 60 --follow
./streamframe --no-start streamer @YOUTUBE_HANDLE --platform youtube --mode live --duration 60 --follow
./streamframe --no-start streamer TWITCH_USERNAME --mode live --rolling --session-duration 120 --window-duration 60 --follow
./streamframe --no-start submit 'https://www.twitch.tv/videos/VIDEO_ID' --start 120 --end 180 --follow
./streamframe --no-start submit 'https://www.youtube.com/watch?v=VIDEO_ID' --start 120 --end 180 --follow
./streamframe --no-start submit '/absolute/path/to/video.mp4' --start 120 --end 180 --follow
./streamframe --no-start list
./streamframe --no-start status JOB_ID
./streamframe --no-start edit JOB_ID CANDIDATE_ID --start 10 --end 30 \
  --face-crop 0 0 0.25 0.33 --content-crop 0.25 0 0.75 1
./streamframe --no-start export JOB_ID CANDIDATE_ID --follow
./streamframe --no-start live-crop JOB_ID --face-crop 0 0 0.25 0.33
./streamframe --no-start live-crop JOB_ID --auto
./streamframe --no-start cancel JOB_ID
```

These are separate examples, not a script to run end to end. In particular,
`live-crop` affects future rolling windows; `edit` changes only the selected
candidate. Crop coordinates are normalized `x y width height`. For bounded
imports, requested start/end are in original-source seconds; candidate and
word times are relative to the retrieved range or retained rolling window.
`--follow` watches a submission or export; use `status JOB_ID --watch` for an
existing job. Use `cancel JOB_ID` to stop processing rather than assuming
closing the browser or ending a status poll will stop it.

For a local agent, reuse the existing loopback service, poll by job ID, and
surface failures instead of blindly resubmitting. Render only from authorized
sources and keep publishing and destructive cleanup separate from capture.
The API contract is in [`server.py`](src/streamframe/server.py); job
submission and export are asynchronous, and an export is not ready just
because the request was accepted. This repository ships no scheduler. An
optional publisher is separate from capture and export; see
[publishing](docs/PUBLISHING.md).

Publishing is configured through a git-ignored `.env` and is safe to re-run:
each clip publishes once per platform. With `STREAMFRAME_PUBLISH_AUTO=1`, or
`--auto-publish` on one job, finished exports post themselves — a separate
worker, not the render pipeline.

```sh
./streamframe publish setup --write-env    # create .env, show the next step
./streamframe publish doctor
./streamframe publish auth youtube
./streamframe publish run --file exports/clip.mp4 --caption "..."          # dry run
./streamframe publish run --file exports/clip.mp4 --caption "..." --yes
./streamframe publish status
./streamframe publish refresh

./streamframe submit clip.mp4 --auto-publish        # this source only
./streamframe streamer name --mode live --rolling --auto-publish
```

## Development

After setup, put the compatible FFmpeg directory first on `PATH` for direct
tests so `ffmpeg` and `ffprobe` resolve to the intended build:

```sh
.venv/bin/python -m pytest
npm --prefix frontend test
npm --prefix frontend run build
```

The optional `npm --prefix frontend run test:browser` uses an installed
Chromium/Chrome, FFmpeg with libass, local Whisper `small`, and macOS `say`
for its real-speech scenario. Run the frontend build first. See the
[frontend guide](frontend/README.md#real-browser--backend--renderer-regression)
for test overrides; the browser tests don't download a browser or model.

`npm --prefix frontend run dev` is optional for frontend development; its
Vite proxy points at port `8000`, so start the backend for that workflow with
`./streamframe serve --port 8000`. Regular use serves the production build
and API together on port `8765`.

To generate your own original test input with FFmpeg and macOS `say` or
`espeak`, run `mkdir -p data` and then
`.venv/bin/python scripts/create_synthetic_fixture.py data/synthetic-input.mp4`.
`.venv/bin/python -m streamframe.evaluate --help` describes the offline
evaluator for labeled footage: transcription error and timing, event
overlap and boundary errors, duplicate predictions, and face-box metrics.
Use authorized, human-labeled footage for quality comparisons; synthetic
tests don't establish accuracy on real streams.

| Area | Files |
| --- | --- |
| Launcher and API client | [`streamframe`](streamframe), [`cli.py`](src/streamframe/cli.py) |
| Loopback HTTP routes and persistent SQLite jobs | [`server.py`](src/streamframe/server.py), [`jobs.py`](src/streamframe/jobs.py) |
| Public source resolution and guarded live HLS ingestion | [`sources.py`](src/streamframe/sources.py), [`live.py`](src/streamframe/live.py) |
| Local transcription, ranking, framing, and FFmpeg rendering | [`analysis.py`](src/streamframe/analysis.py), [`framing.py`](src/streamframe/framing.py), [`render.py`](src/streamframe/render.py) |
| React editor and test entry points | [`frontend/`](frontend/), [`tests/`](tests/), [CI](.github/workflows/ci.yml) |

## Third-party notices

- The bundled [OpenCV Zoo YuNet model](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
  is distributed under the [MIT license](models/YUNET-LICENSE.txt).
  Its SHA-256 is `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`.
- The portrait in the fixture and demo is by **Pete Souza**, licensed
  [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). The demo
  resizes and composites it. See [fixture provenance](tests/fixtures/PROVENANCE.md)
  for the image source and attribution.
- The separately downloaded [faster-whisper `small` model](https://huggingface.co/Systran/faster-whisper-small),
  third-party packages, and your FFmpeg build have their own licensing terms.
  Keep the relevant notices when redistributing them.
- [PySceneDetect](https://github.com/Breakthrough/PySceneDetect),
  [Silero VAD](https://github.com/snakers4/silero-vad),
  [smartcrop.py](https://github.com/smartcrop/smartcrop.py), and
  [videohash](https://github.com/akamhy/videohash) were technique references;
  no source code from those projects is included.
