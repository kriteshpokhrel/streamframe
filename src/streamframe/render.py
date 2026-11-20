"""Render a vertical, two-panel clip with optional burned-in word captions.

``render_clip`` accepts source-relative times in seconds. Crop rectangles are
``(x, y, width, height)`` fractions of the source frame, not of the output.
Words are mappings with ``start``, ``end``, ``text`` and optional
``correction``. A correction changes only the displayed caption, never audio.

FFmpeg must provide libx264 and AAC; captions additionally require its libass
``ass`` filter. Outputs are MP4 files. The optional loudness target uses
single-pass EBU R128 loudnorm (TP=-1.5 dB, LRA=11); it is an approximation,
not a guarantee of measured integrated loudness, especially for short clips.
Inputs are restricted to local files and common self-contained media demuxers;
network protocols, playlists, concatenation scripts, and MOV external tracks
are not supported.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class RenderError(RuntimeError):
    """The source, rendering, or output validation failed."""


class RenderCancelled(RenderError):
    """The caller requested cancellation; no output was published."""


_INPUT_OPTIONS = [
    "-protocol_whitelist", "file,pipe",
    "-format_whitelist",
    ("mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,flv,mpeg,mpegts,"
     "wav,mp3,aac,flac,ogg,asf,nut,rm,ape,amr,au,ac3,dts,h264,hevc"),
]
_MOV_OPTIONS = ["-enable_drefs", "0", "-use_absolute_path", "0"]


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _crop(rect: Sequence[float], label: str) -> tuple[float, float, float, float]:
    if isinstance(rect, (str, bytes)) or len(rect) != 4:
        raise ValueError(f"{label} must be (x, y, width, height)")
    x, y, width, height = (_number(v, label) for v in rect)
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise ValueError(f"{label} must be a nonempty rectangle within the source")
    return x, y, width, height


def _run(args: list[str], cancel: Callable[[], bool] | None, *, cwd: Path | None = None) -> str:
    """Capture diagnostics without a shell, polling for cancellation."""
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=cwd,
        )
    except OSError as exc:
        raise RenderError(f"Could not start {args[0]}: {exc}") from exc
    try:
        while True:
            if cancel is not None and cancel():
                raise RenderCancelled("Rendering cancelled")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RenderError(f"{Path(args[0]).name} failed ({process.returncode}): {stderr.strip()}")
        return stdout
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def _probe(path: Path, ffprobe: str, cancel: Callable[[], bool] | None) -> dict[str, Any]:
    raw = _run([ffprobe, "-v", "error", *_INPUT_OPTIONS, *_MOV_OPTIONS, "-show_streams",
                "-show_format", "-of", "json", str(path)], cancel)
    try:
        info = json.loads(raw)
        if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
            raise ValueError("missing streams")
        return info
    except (ValueError, TypeError) as exc:
        raise RenderError("ffprobe returned invalid JSON or stream information") from exc


def _display_text(value: str) -> str:
    # ASS has no portable way to quote all control characters in dialogue text.
    # Visually similar Unicode forms prevent both override tags and \N escapes.
    return " ".join(value.replace("\\", "＼").replace("{", "｛").replace("}", "｝").split())


def _words(words: Sequence[Mapping[str, Any]], start: float, end: float) -> list[tuple[float, float, str]]:
    result: list[tuple[float, float, str]] = []
    previous = -math.inf
    for index, word in enumerate(words):
        if not isinstance(word, Mapping):
            raise ValueError(f"word {index} must be a mapping")
        try:
            begin = _number(word["start"], f"word {index} start")
            finish = _number(word["end"], f"word {index} end")
            text = word["text"]
        except KeyError as exc:
            raise ValueError(f"word {index} is missing {exc.args[0]}") from exc
        correction = word.get("correction")
        if not isinstance(text, str) or (correction is not None and not isinstance(correction, str)):
            raise ValueError(f"word {index} text and correction must be strings")
        if begin < 0 or finish <= begin or begin < previous:
            raise ValueError("words must have positive durations and be ordered by start time")
        previous = begin
        text = _display_text(text if correction is None else correction)
        if finish > start and begin < end and text:
            result.append((max(begin, start) - start, min(finish, end) - start, text))
    return result


def _ass_time(centiseconds: int) -> str:
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, hundredths = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def _ass(words: list[tuple[float, float, str]]) -> str:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,100,100,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    # Five words per phrase bounds line length; the highlighted word changes
    # with its own source timestamps rather than altering the speech.
    for offset in range(0, len(words), 5):
        group = words[offset:offset + 5]
        for i, (start, end, _) in enumerate(group):
            start_cs = round(start * 100)
            end_cs = max(start_cs + 1, round(end * 100))
            parts = [f"{{\\c&H00FFFF&}}{text}{{\\c&HFFFFFF&}}" if i == j else text
                     for j, (_, _, text) in enumerate(group)]
            events.append(f"Dialogue: 0,{_ass_time(start_cs)},{_ass_time(end_cs)},Default,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"


def _filter_crop(rect: tuple[float, float, float, float], *, fit: bool = False) -> str:
    x, y, width, height = rect
    if fit:
        return (f"crop=iw*{width:.12g}:ih*{height:.12g}:iw*{x:.12g}:ih*{y:.12g},"
                "scale=1080:960:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "pad=1080:960:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    return (f"crop=iw*{width:.12g}:ih*{height:.12g}:iw*{x:.12g}:ih*{y:.12g},"
            "scale=1080:960:force_original_aspect_ratio=increase,"
            "crop=1080:960,setsar=1")


def render_clip(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    start: float,
    end: float,
    facecam_crop: Sequence[float],
    content_crop: Sequence[float],
    words: Sequence[Mapping[str, Any]] = (),
    include_audio: bool = True,
    loudness_target_lufs: float | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    cancel: Callable[[], bool] | None = None,
    max_output_bytes: int | None = None,
) -> Path:
    """Render and atomically publish an MP4; return its absolute path.

    ``start`` and ``end`` are source-relative seconds, with an exclusive end.
    Each crop is a normalized ``(x, y, width, height)`` rectangle. The full
    facecam rectangle fits inside its panel with padding as needed; the content
    rectangle is center-cropped to fill its panel. ``words`` is
    ordered by start time, with absolute source-second ``start``/``end`` and
    ``text`` strings, plus optional string ``correction`` (including empty).
    Captions are placed inside 100px horizontal/250px bottom safe margins.
    ``include_audio=False`` omits audio; absent source audio is also allowed.
    ``cancel``, if supplied, is polled and raises RenderCancelled when true.
    ``max_output_bytes`` enforces a POSIX per-file limit on FFmpeg and rejects
    truncated or oversized output before publication.
    Both tools may be executable paths. Existing output survives any failure.
    """
    start = _number(start, "start")
    end = _number(end, "end")
    if start < 0 or end <= start:
        raise ValueError("clip must have 0 <= start < end")
    face = _crop(facecam_crop, "facecam_crop")
    content = _crop(content_crop, "content_crop")
    captions = _words(words, start, end)
    if not isinstance(include_audio, bool):
        raise ValueError("include_audio must be a bool")
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes < 1):
        raise ValueError("max_output_bytes must be a positive integer")
    if loudness_target_lufs is not None:
        loudness_target_lufs = _number(loudness_target_lufs, "loudness_target_lufs")
        if not -70 <= loudness_target_lufs <= -5:
            raise ValueError("loudness_target_lufs must be between -70 and -5")
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path == output_path:
        raise ValueError("source and output must be different files")
    if output_path.suffix.lower() != ".mp4":
        raise ValueError("output must have an .mp4 extension")
    if not output_path.parent.is_dir():
        raise ValueError("output directory must already exist")
    if os.path.dirname(ffmpeg):
        ffmpeg = str(Path(ffmpeg).expanduser().resolve())
    info = _probe(source_path, ffprobe, cancel)
    videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
    if not videos:
        raise RenderError("source has no video stream")
    audio = include_audio and any(s.get("codec_type") == "audio" for s in info["streams"])
    try:
        duration = float(info.get("format", {}).get("duration", "nan"))
    except (TypeError, ValueError):
        duration = math.nan
    if math.isfinite(duration) and end > duration + 0.001:
        raise ValueError("clip end exceeds source duration")

    token = uuid.uuid4().hex
    subtitle_path = output_path.parent / f".streamframe-{token}.ass"
    partial_path = output_path.parent / f".streamframe-{token}.mp4"
    try:
        graph = (f"[0:v:0]split=2[face][content];[face]{_filter_crop(face, fit=True)}[top];"
                 f"[content]{_filter_crop(content)}[bottom];"
                 "[top][bottom]vstack=inputs=2[stack]")
        if captions:
            subtitle_path.write_text(_ass(captions), encoding="utf-8")
            # The generated basename contains no filter-grammar metacharacters.
            graph += f";[stack]ass=filename='{subtitle_path.name}'[captioned]"
        else:
            graph += ";[stack]null[captioned]"
        graph += ";[captioned]scale=in_range=auto:out_range=tv,format=yuv420p,setparams=range=limited[out]"
        source_formats = str(info.get("format", {}).get("format_name", "")).split(",")
        input_options = _INPUT_OPTIONS + (_MOV_OPTIONS if "mov" in source_formats else [])
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                *input_options, "-ss", f"{start:.9f}", "-i", str(source_path),
                "-filter_complex", graph, "-map", "[out]"]
        if audio:
            args += ["-map", "0:a:0", "-c:a", "aac"]
            if loudness_target_lufs is not None:
                args += ["-af", f"loudnorm=I={loudness_target_lufs:.6g}:TP=-1.5:LRA=11"]
        else:
            args += ["-an"]
        args += ["-t", f"{end - start:.9f}", "-r", "30", "-fps_mode", "cfr",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv",
                 "-movflags", "+faststart"]
        if max_output_bytes is not None:
            args += ["-fs", str(max_output_bytes)]
        args.append(str(partial_path))
        if max_output_bytes is not None:
            # Set the file-size limit in a separate process, not preexec_fn in
            # the multithreaded job runner. FFmpeg's -fs alone can overshoot.
            limiter = ("import os,resource,sys; n=int(sys.argv[1]); "
                       "resource.setrlimit(resource.RLIMIT_FSIZE,(n,n)); "
                       "os.execvp(sys.argv[2],sys.argv[2:])")
            args = [sys.executable, "-c", limiter, str(max_output_bytes), *args]
        _run(args, cancel, cwd=output_path.parent)
        if max_output_bytes is not None and partial_path.stat().st_size > max_output_bytes:
            raise RenderError("rendered output exceeds its byte limit")
        rendered = _probe(partial_path, ffprobe, cancel)
        streams = rendered["streams"]
        rendered_video = [s for s in streams if s.get("codec_type") == "video"]
        rendered_audio = [s for s in streams if s.get("codec_type") == "audio"]
        if (len(rendered_video) != 1 or rendered_video[0].get("codec_name") != "h264"
                or rendered_video[0].get("width") != 1080 or rendered_video[0].get("height") != 1920
                or rendered_video[0].get("pix_fmt") != "yuv420p"
                or len(rendered_audio) != int(bool(audio))
                or (audio and rendered_audio[0].get("codec_name") != "aac")):
            raise RenderError("rendered output has unexpected streams or encoding")
        try:
            actual_duration = float(rendered["format"]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderError("rendered output has no valid duration") from exc
        if not math.isfinite(actual_duration) or abs(actual_duration - (end - start)) > 0.1:
            raise RenderError(f"rendered duration {actual_duration} does not match requested duration {end - start}")
        try:
            video_duration = float(rendered_video[0]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderError("rendered video has no valid duration") from exc
        if not math.isfinite(video_duration) or abs(video_duration - (end - start)) > 0.1:
            raise RenderError("rendered video duration does not match requested duration")
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
              *_INPUT_OPTIONS, *_MOV_OPTIONS, "-i", str(partial_path), "-map", "0:v:0", "-map", "0:a:0?",
              "-f", "null", "-"], cancel)
        if cancel is not None and cancel():
            raise RenderCancelled("Rendering cancelled")
        os.replace(partial_path, output_path)
        return output_path
    finally:
        subtitle_path.unlink(missing_ok=True)
        partial_path.unlink(missing_ok=True)


def render_candidate(
    source: str | os.PathLike[str],
    candidate: Mapping[str, Any],
    output: str | os.PathLike[str],
    progress: Callable[[float, str], None] | None = None,
    cancelled: Any = None,
    max_output_bytes: int | None = None,
) -> Path:
    """Render a job candidate using dictionary crops and a threading.Event.

    Candidate keys are ``start``, ``end``, optional ``words``, and optional
    ``face_crop``/``content_crop`` dictionaries with x/y/width/height. Missing
    crops use the full source for that panel. ``progress`` takes a fraction
    and message; ``cancelled`` is an Event exposing ``is_set()``.
    """
    def rectangle(name: str) -> tuple[float, float, float, float]:
        value = candidate.get(name)
        if value is None:
            return (0, 0, 1, 1)
        try:
            return tuple(value[key] for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{name} must contain x, y, width, height") from exc

    if progress:
        progress(0, "Rendering clip")
    result = render_clip(
        source, output, start=candidate["start"], end=candidate["end"],
        facecam_crop=rectangle("face_crop"), content_crop=rectangle("content_crop"),
        words=candidate.get("words", ()),
        cancel=cancelled.is_set if cancelled is not None else None,
        max_output_bytes=max_output_bytes,
    )
    if progress:
        progress(1, "Clip rendered and validated")
    return result
