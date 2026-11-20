"""Loopback-only FastAPI app. Run: uvicorn streamframe.server:app --host 127.0.0.1.

STREAMFRAME_DATA_DIR selects persistent storage; STREAMFRAME_IMPORT_ROOTS is an
os.pathsep-separated allowlist (default: home). Never bind this app to a public
interface or put it behind a proxy; forwarded headers are intentionally rejected.

Contract (JSON; progress is a 0..1 fraction):
GET /api/health -> {"status":"ok"}
GET /api/jobs -> {"jobs":[Job]}
POST /api/jobs {"source":{"type":"file","path":"/absolute/video.mp4"}}
  or {"source":{"type":"url","url":"https://www.twitch.tv/videos/123","start":0,"end":60}}
  or {"source":{"type":"streamer","identifier":"name","platform":"twitch",
                "mode":"auto","duration":60}} -> 202 Job
  File sources optionally accept numeric start/end in original-file seconds.
  With either bound, start defaults to 0 and end defaults to start + 1800.
  End must exceed start by at most 1800 seconds, and import can end early at EOF.
  Public finished video URLs require both numeric bounds (max span 1800 seconds).
  Twitch and YouTube channel URLs accept the same mode/duration options as streamer input.
  Streamer defaults: platform=twitch, mode=auto, duration=60 seconds (1–300).
  To automatically export during live capture, set mode=live, rolling=true,
  session_duration (1–1800 seconds, default 600) and window_duration (1–60
  seconds, default 60); each window must fit in the session. Rolling jobs
  cannot resume; completed exports remain accessible after cancellation.
  Windows without a confident or manual face crop skip automatic export, but
  retain the candidate window for manual correction. Published rolling clips
  retain their source window for editing and re-export.
  Automatic publication additionally requires verified source video dimensions
  of at least 720 pixels on the shorter edge. Lower-resolution windows have
  status=low_quality, source_width/source_height and a warning; their candidate
  remains available for manual review/export (no upscaling to pass this gate).
  YouTube identities must be explicit @handles or channel URLs. Auto picks live
  first, then latest finished archive; live and latest force one selection.
  Live uses duration only; optional start/end apply to VOD selection. Omitted
  bounds on a channel's finished archive select its first duration seconds.
  Preview, candidate, and word times are always relative to imported media:
  for a range import, time zero corresponds to the requested original start.
GET /api/jobs/{id} -> Job
GET /api/jobs/{id}/diagnostics -> {"text": paste-ready sanitized diagnostic report}
DELETE /api/jobs/{id} -> 204 (no body); removes the job and generated artifacts,
  never the original source. Returns 409 until active work and exports stop.
  If deletion fails midway (500), the list marks the job deletion_pending and
  DELETE can be retried; pending jobs cannot be used or restarted.
POST /api/jobs/{id}/cancel or /resume (no body) -> 202 Job
PATCH /api/jobs/{id}/candidates/{candidate_id} partial Candidate without id -> Job
GET /api/jobs/{id}/candidates/{candidate_id}/media -> retained full window video (Range supported)
  Rolling candidate and word times are window-relative, not export-relative.
  Returns 404 if the window is unavailable.
PATCH /api/jobs/{id}/live-crop {"face_crop":normalized rectangle or null} -> Job
  Applies to subsequent rolling windows; null restores automatic detection.
POST /api/jobs/{id}/candidates/{candidate_id}/export (no body) -> 202 Job
GET /api/jobs/{id}/media -> imported video bytes, including HTTP Range support
GET /api/jobs/{id}/exports/{filename} -> MP4 attachment
DELETE /api/jobs/{id}/exports/{filename} -> 204; removes only that published export
DELETE /api/jobs/{id}/exports -> 204; removes published exports for that source only
POST /api/jobs optionally accepts auto_export: {title?: string | null} alongside source.
  Exports the two highest-scored moments with usable framing in that selection.
  An omitted or blank title uses the imported video title or local filename.
  An explicit null exports without an opening title.
Job: id, source, status, stage, progress, candidates, exports, media_url, error,
  operation ("analyze"|"export"), export_candidate_id, created_at, updated_at;
  timestamps are Unix seconds, error/media_url/export_candidate_id nullable.
  message is an optional progress string. Remote jobs may expose resolved_source
  {url,platform,title,is_live,selection}, imported_range {start,end}, and
  capture_started_at (UTC ISO timestamp or null when no reliable alignment).
  Rolling jobs expose live_session with phases, window status, global offsets,
  warnings and face_crop; exports contain per-window sequence and timing data.
  Status is queued/running/exporting/
  cancelling/cancelled/failed/succeeded/interrupted; restart requires resume.
Candidate: id (immutable string), start, end, score (finite number), evidence
  (list of strings), words (list of {start,end,text,correction?:string}),
  face_crop, content_crop (null or normalized {x,y,width,height}), optional
  opening_title (up to 60 characters; empty disables it).
  For imported sources, face_crop applies to all candidates on the job.
Export: {candidate_id,filename,url}. Edits and exports return the whole Job.
Missing resources: 404; invalid JSON/schema/source: 422; busy/state conflict:
409; unsafe peer/Host/Origin: 403; non-JSON mutation payload: 415; body >1MiB: 413.
Remote retrieval is unauthenticated, with pinned public DNS at every connection
including redirects. Ended Twitch VODs require plain MPEG-TS HLS and fetch only
overlapping segments (max 512 MiB/1200 segments/10 minutes); supported YouTube
archives additionally require a verified complete timeline. YouTube video
links may use full_video=true instead of start/end for a finished video up
to 300 seconds; compatible MP4 and split MP4/WebM tracks are allowed for those whole-video
requests. Partial ranges never fall back to a full download.
YouTube VOD imports must verify at 720p or better before publishing the local
source. The legacy require_hd boolean is still accepted on explicit full-video
requests but no longer changes this requirement. Whole-video selection tries
compatible HD alternatives with at most 12 track requests under the same byte
and deadline limits. If the CDN rejects a whole-track request, the importer can
assemble that track from validated byte ranges, with a separate shared cap of
160 range requests. HD HLS resolution tries at most 12 compatible manifests.
Full local imports are limited to 2GiB.
Range imports can seek into larger regular files without copying them first;
the generated H.264 MP4 must be under 2GiB and finish within 15 minutes.
Range imports require FFmpeg with seekable fd input and libx264 support.
STREAMFRAME_ALLOWED_ORIGINS optionally lists comma-separated exact loopback
development origins; it does not enable CORS.
Analysis configuration: STREAMFRAME_FFMPEG_PATH (or STREAMFRAME_FFMPEG),
STREAMFRAME_YUNET_MODEL_PATH (or STREAMFRAME_YUNET_MODEL),
STREAMFRAME_WHISPER_MODEL, STREAMFRAME_DEVICE, STREAMFRAME_COMPUTE_TYPE.
YuNet defaults to models/face_detection_yunet_2023mar.onnx when present.
Rendering uses ffmpeg/ffprobe on PATH. No models are silently downloaded here;
the real analysis module owns model provisioning and reports genuine failures.
"""

from contextlib import asynccontextmanager
import ipaddress
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from .jobs import Conflict, DeletionFailed, JobStore


MAX_BODY_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)


def create_app(data_dir=None, import_roots=None, analyzer=None, renderer=None):
    allowed_origins = set()
    for origin in os.environ.get("STREAMFRAME_ALLOWED_ORIGINS", "").split(","):
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("STREAMFRAME_ALLOWED_ORIGINS accepts exact loopback origins only")
        allowed_origins.add(origin)
    @asynccontextmanager
    async def lifespan(app):
        roots = import_roots
        if roots is None and os.environ.get("STREAMFRAME_IMPORT_ROOTS"):
            roots = os.environ["STREAMFRAME_IMPORT_ROOTS"].split(os.pathsep)
        app.state.jobs = JobStore(
            data_dir or os.environ.get("STREAMFRAME_DATA_DIR", str(Path.home() / ".streamframe")),
            import_roots=roots, analyzer=analyzer, renderer=renderer,
        )
        try:
            yield
        finally:
            await run_in_threadpool(app.state.jobs.close)

    app = FastAPI(title="StreamFrame local API", lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            if request.client is None or not ipaddress.ip_address(request.client.host).is_loopback:
                raise ValueError()
            host = urlsplit("//" + request.headers.get("host", ""))
            if (host.hostname not in {"127.0.0.1", "localhost", "::1"} or host.username
                    or host.password or host.path or host.query or host.fragment
                    or host.port is not None and not 0 < host.port <= 65535):
                raise ValueError()
            if any(key in request.headers for key in ("forwarded", "x-forwarded-for", "x-forwarded-host")):
                raise ValueError()
            origin = request.headers.get("origin")
            if origin is not None:
                expected = urlsplit(origin)
                if origin not in allowed_origins and (
                        expected.scheme != request.url.scheme or expected.netloc != host.netloc
                        or expected.path or expected.query or expected.fragment):
                    raise ValueError()
            if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
                raise ValueError()
        except ValueError:
            return JSONResponse({"detail": "Local same-origin access only"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Content-Type must be application/json")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise HTTPException(413, "Request body too large")
            chunks.append(chunk)
        try:
            result = json.loads(b"".join(chunks))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise HTTPException(422, "Body must be a JSON object")

    async def invoke(request, method, *args):
        try:
            return await run_in_threadpool(getattr(request.app.state.jobs, method), *args)
        except KeyError:
            raise HTTPException(404, "Job, candidate, or media not found")
        except Conflict as exc:
            raise HTTPException(409, str(exc))
        except DeletionFailed as exc:
            logger.exception("Job deletion failed")
            raise HTTPException(500, str(exc))
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/jobs")
    async def list_jobs(request: Request):
        return {"jobs": await invoke(request, "list")}

    @app.post("/api/jobs", status_code=202)
    async def create_job(request: Request):
        payload = await body(request)
        if "source" not in payload or set(payload) - {"source", "auto_export"}:
            raise HTTPException(422, "Expected source and optional auto_export")
        if "auto_export" in payload:
            return await invoke(request, "create", payload["source"], payload["auto_export"])
        return await invoke(request, "create", payload["source"])

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        return await invoke(request, "get", job_id)

    @app.get("/api/jobs/{job_id}/diagnostics")
    async def get_diagnostics(job_id: str, request: Request):
        return await invoke(request, "diagnostics", job_id)

    @app.delete("/api/jobs/{job_id}", status_code=204, response_class=Response)
    async def delete_job(job_id: str, request: Request):
        await invoke(request, "delete", job_id)
        return Response(status_code=204)

    @app.post("/api/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(job_id: str, request: Request):
        return await invoke(request, "cancel", job_id)

    @app.post("/api/jobs/{job_id}/resume", status_code=202)
    async def resume_job(job_id: str, request: Request):
        return await invoke(request, "resume", job_id)

    @app.patch("/api/jobs/{job_id}/candidates/{candidate_id}")
    async def edit_candidate(job_id: str, candidate_id: str, request: Request):
        return await invoke(request, "edit", job_id, candidate_id, await body(request))

    @app.patch("/api/jobs/{job_id}/live-crop")
    async def set_live_crop(job_id: str, request: Request):
        return await invoke(request, "set_live_crop", job_id, await body(request))

    @app.post("/api/jobs/{job_id}/candidates/{candidate_id}/export", status_code=202)
    async def export_candidate(job_id: str, candidate_id: str, request: Request):
        return await invoke(request, "export", job_id, candidate_id)

    @app.get("/api/jobs/{job_id}/media")
    async def media(job_id: str, request: Request):
        path = await invoke(request, "media_path", job_id)
        return FileResponse(path)

    @app.get("/api/jobs/{job_id}/candidates/{candidate_id}/media")
    async def candidate_media(job_id: str, candidate_id: str, request: Request):
        path = await invoke(request, "candidate_media_path", job_id, candidate_id)
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/jobs/{job_id}/exports/{filename}")
    async def export_file(job_id: str, filename: str, request: Request):
        path = await invoke(request, "media_path", job_id, filename)
        return FileResponse(path, media_type="video/mp4", filename=filename)

    @app.delete("/api/jobs/{job_id}/exports/{filename}", status_code=204, response_class=Response)
    async def delete_export(job_id: str, filename: str, request: Request):
        await invoke(request, "delete_export", job_id, filename)
        return Response(status_code=204)

    @app.delete("/api/jobs/{job_id}/exports", status_code=204, response_class=Response)
    async def delete_exports(job_id: str, request: Request):
        await invoke(request, "delete_exports", job_id)
        return Response(status_code=204)

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    return app


app = create_app()
