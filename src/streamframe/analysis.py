"""Local media analysis helpers for StreamFrame.

The public entry point is :func:`analyze_media`. All returned data is composed
of JSON-serializable dictionaries, lists, strings, numbers, booleans, and nulls.
Whisper models and YuNet model files must already exist locally; this module
never downloads either model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Sequence


class AnalysisError(RuntimeError):
    """Raised when probing or analyzing media fails."""


class AnalysisCancelled(AnalysisError):
    """Raised when the supplied cancellation callback requests cancellation."""


_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SENTENCE_END_RE = re.compile(r"""[.!?]["'’”)\]]*$""")
_EMPHASIS_WORDS = {
    "amazing", "best", "crazy", "finally", "incredible", "never", "no",
    "perfect", "really", "unbelievable", "wait", "what", "why", "wow",
}
_SAFE_INPUT_PROTOCOLS = "file,pipe"
# Keep playlist/reference demuxers (notably HLS, DASH, and concat) unavailable.
_SAFE_INPUT_FORMATS = (
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,flv,mpeg,mpegts,"
    "wav,mp3,aac,flac,ogg,asf,rm,ape,amr,au,ac3,dts,h264,hevc"
)


def _check_cancelled(callback: Callable[[], bool] | None) -> None:
    if callback is not None and callback():
        raise AnalysisCancelled("Media analysis was cancelled")


def _resolve_binary(binary: str, sibling: str) -> str:
    path = Path(binary)
    if path.is_file():
        candidate = path.with_name(sibling)
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(sibling)
    if found:
        return found
    raise AnalysisError(f"Could not find {sibling}; install FFmpeg or provide its path")


def _local_media_path(media_path: str | os.PathLike[str]) -> Path:
    try:
        path = Path(media_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AnalysisError(f"Media file does not exist or cannot be resolved: {media_path}") from exc
    if not path.is_file():
        raise AnalysisError(f"Media path is not a local regular file: {media_path}")
    return path


def _safe_input_options() -> list[str]:
    return [
        "-protocol_whitelist", _SAFE_INPUT_PROTOCOLS,
        "-format_whitelist", _SAFE_INPUT_FORMATS,
        "-enable_drefs", "0",
        "-use_absolute_path", "0",
    ]


def _run_process(
    args: Sequence[str],
    *,
    cancellation_callback: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise AnalysisError(f"Could not start {args[0]}: {exc}") from exc

    while True:
        try:
            _check_cancelled(cancellation_callback)
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
        try:
            stdout, stderr = proc.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            continue
        except BaseException:
            proc.kill()
            proc.communicate()
            raise

    result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if result.returncode:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise AnalysisError(f"Command failed ({args[0]}): {detail or result.returncode}")
    return result


def probe_media(
    media_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str = "ffmpeg",
    cancellation_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Probe local media with network protocols and playlist demuxers disabled."""
    path = _local_media_path(media_path)
    ffprobe = _resolve_binary(ffmpeg_path, "ffprobe")
    result = _run_process(
        [
            ffprobe, "-v", "error",
            *_safe_input_options(),
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate",
            "-of", "json", str(path),
        ],
        cancellation_callback=cancellation_callback,
    )
    try:
        data = json.loads(result.stdout.decode("utf-8"))
        streams = data.get("streams", [])
        duration = float(data.get("format", {}).get("duration", 0) or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"Could not parse media probe output for {path}") from exc
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_available = any(s.get("codec_type") == "audio" for s in streams)
    fps = None
    rate = video.get("avg_frame_rate")
    if rate and rate != "0/0":
        try:
            numerator, denominator = map(float, rate.split("/", 1))
            fps = numerator / denominator if denominator else None
        except (ValueError, ZeroDivisionError):
            fps = None
    return {
        "path": str(path),
        "duration": max(0.0, duration),
        "audio_available": audio_available,
        "video_available": bool(video),
        "width": int(video["width"]) if video.get("width") else None,
        "height": int(video["height"]) if video.get("height") else None,
        "fps": fps,
    }


def extract_audio(
    media_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str = "ffmpeg",
    sample_rate: int = 16_000,
    cancellation_callback: Callable[[], bool] | None = None,
) -> tuple[Any, int]:
    """Decode audio to a mono float32 NumPy array without creating temp files."""
    try:
        import numpy as np
    except ImportError as exc:
        raise AnalysisError("Audio analysis requires numpy") from exc
    path = _local_media_path(media_path)
    result = _run_process(
        [
            ffmpeg_path, "-v", "error",
            *_safe_input_options(),
            "-i", str(path), "-vn", "-ac", "1",
            "-ar", str(sample_rate), "-f", "s16le", "-acodec", "pcm_s16le",
            "pipe:1",
        ],
        cancellation_callback=cancellation_callback,
    )
    if len(result.stdout) % 2:
        raise AnalysisError("FFmpeg returned malformed PCM audio")
    audio = np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0
    return audio, sample_rate


def _resolve_local_whisper_model(model: str | os.PathLike[str], cache_dir: str | None) -> str:
    model_path = Path(model).expanduser()
    if model_path.is_dir():
        return str(model_path)
    name = str(model)
    if Path(name).is_absolute() or "/" in name or "\\" in name:
        raise AnalysisError(f"Whisper model directory was not found: {model_path}")

    # faster-whisper's model aliases are cached in Hugging Face's standard layout.
    roots = [Path(cache_dir).expanduser()] if cache_dir else []
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    repo = "models--Systran--faster-whisper-" + name
    for root in roots:
        snapshots = root / repo / "snapshots"
        if snapshots.is_dir():
            local = sorted(p for p in snapshots.iterdir() if p.is_dir())
            if local:
                return str(local[-1])
    raise AnalysisError(
        f"Whisper model {name!r} is not available locally. Install/cache it before analysis; "
        "StreamFrame will not download models."
    )


def _cache_key(path: Path, model: str, device: str, compute_type: str) -> str:
    stat = path.stat()
    payload = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{model}|{device}|{compute_type}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_words(words: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in words:
        try:
            start = max(0.0, float(item["start"]))
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            continue
        if end == start:
            end = start + 0.04
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        probability = item.get("probability", item.get("confidence"))
        try:
            probability = min(1.0, max(0.0, float(probability))) if probability is not None else None
        except (ValueError, TypeError):
            probability = None
        normalized.append({
            "start": start, "end": end, "text": text, "probability": probability,
        })
    return sorted(normalized, key=lambda w: (w["start"], w["end"]))


def transcribe_audio(
    media_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str = "ffmpeg",
    model: str | os.PathLike[str] = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    cache_dir: str | os.PathLike[str] | None = None,
    cache_transcription: bool = True,
    audio: Any = None,
    sample_rate: int = 16_000,
    cancellation_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Transcribe safe FFmpeg-decoded samples with local faster-whisper.

    faster-whisper receives an in-memory mono waveform rather than a source path,
    so its PyAV decoder cannot independently parse playlists or external refs.
    """
    path = _local_media_path(media_path)
    if sample_rate != 16_000:
        raise AnalysisError("faster-whisper waveform input must use a 16000 Hz sample rate")
    cache_root = Path(cache_dir).expanduser() if cache_dir else Path.home() / ".cache" / "streamframe"
    key = _cache_key(path, str(model), device, compute_type)
    cache_path = cache_root / f"transcript-{key}.json"
    if cache_transcription and cache_path.is_file():
        _check_cancelled(cancellation_callback)
        try:
            with cache_path.open(encoding="utf-8") as file:
                cached = json.load(file)
            cached["cached"] = True
            return cached
        except (OSError, ValueError, TypeError):
            pass

    local_model = _resolve_local_whisper_model(model, str(cache_root))
    _check_cancelled(cancellation_callback)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AnalysisError("Transcription requires the faster-whisper package") from exc
    try:
        if audio is None:
            audio, sample_rate = extract_audio(
                path, ffmpeg_path=ffmpeg_path, sample_rate=sample_rate,
                cancellation_callback=cancellation_callback,
            )
        _check_cancelled(cancellation_callback)
        whisper = WhisperModel(local_model, device=device, compute_type=compute_type)
        segments, info = whisper.transcribe(
            audio, word_timestamps=True, vad_filter=True,
        )
        words: list[dict[str, Any]] = []
        text_parts = []
        for segment in segments:
            _check_cancelled(cancellation_callback)
            segment_text = str(getattr(segment, "text", "")).strip()
            if segment_text:
                text_parts.append(segment_text)
            for word in getattr(segment, "words", None) or []:
                words.append({
                    "start": getattr(word, "start", None),
                    "end": getattr(word, "end", None),
                    "text": getattr(word, "word", ""),
                    "probability": getattr(word, "probability", None),
                })
    except AnalysisCancelled:
        raise
    except Exception as exc:
        raise AnalysisError(f"Local transcription failed: {exc}") from exc
    result = {
        "text": " ".join(text_parts).strip(),
        "words": _normalize_words(words),
        "language": getattr(info, "language", None),
        "language_probability": _finite_or_none(getattr(info, "language_probability", None)),
        "cached": False,
    }
    if cache_transcription:
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # A read-only cache must not prevent otherwise successful analysis.
            pass
    return result


def _finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def find_sentence_boundaries(words: Sequence[Mapping[str, Any]]) -> list[float]:
    """Return end times at punctuation-delimited sentence boundaries."""
    boundaries = []
    for word in _normalize_words(words):
        if _SENTENCE_END_RE.search(word["text"]):
            boundaries.append(word["end"])
    return boundaries


def _window_energy(audio: Any, sample_rate: int, start: float, end: float) -> float | None:
    if audio is None or sample_rate <= 0:
        return None
    left = max(0, int(start * sample_rate))
    right = min(len(audio), int(end * sample_rate))
    if right <= left:
        return None
    import numpy as np

    values = np.asarray(audio[left:right], dtype=np.float32)
    return float(np.sqrt(np.mean(values * values))) if values.size else None


def _candidate_score(
    start: float,
    end: float,
    words: Sequence[Mapping[str, Any]],
    boundaries: Sequence[float],
    audio: Any,
    sample_rate: int,
    target: float,
    max_audio_energy: float,
    shot_boundaries: Sequence[float],
    visual_activity: Sequence[Mapping[str, Any]],
) -> tuple[float, dict[str, float | None]]:
    duration = end - start
    overlaps = [
        w for w in words
        if float(w["end"]) > start and float(w["start"]) < end
    ]
    speech_seconds = sum(
        max(0.0, min(end, float(w["end"])) - max(start, float(w["start"])))
        for w in overlaps
    )
    speech_coverage = min(1.0, speech_seconds / duration) if duration else 0.0
    tokens = [token.lower() for w in overlaps for token in _WORD_RE.findall(w["text"])]
    words_per_minute = len(tokens) * 60.0 / duration if duration else 0.0
    density_score = min(1.0, words_per_minute / 150.0)
    confidence_values = [w.get("probability") for w in overlaps if w.get("probability") is not None]
    confidence = sum(confidence_values) / len(confidence_values) if confidence_values else None
    emphasis = sum(token in _EMPHASIS_WORDS for token in tokens)
    emphasis_score = min(1.0, emphasis / max(1.0, len(tokens) * 0.08)) if tokens else 0.0
    nearby_boundaries = [b for b in boundaries if start <= b <= end]
    boundary_score = min(1.0, len(nearby_boundaries) / max(1, duration / 20.0))
    boundary_alignment = max(
        [max(0.0, 1.0 - min(abs(start - b), abs(end - b)) / 3.0) for b in boundaries]
        or [0.0]
    )
    visual_cut_alignment = max(
        [
            max(0.0, 1.0 - min(abs(start - cut), abs(end - cut)) / 2.0)
            for cut in shot_boundaries
        ]
        or [0.0]
    )
    visual_samples = [
        sample for sample in visual_activity
        if start <= float(sample.get("time", -1)) <= end
    ]
    motion_values = [
        min(1.0, max(0.0, float(sample.get("motion", 0.0))))
        for sample in visual_samples
    ]
    visual_motion = sum(motion_values) / len(motion_values) if motion_values else None
    cuts_in_window = sum(
        1 for cut in shot_boundaries if start < float(cut) <= end
    )
    visual_scene_changes = min(1.0, cuts_in_window / max(1.0, duration / 10.0))

    energy = _window_energy(audio, sample_rate, start, end)
    normalized_energy = None
    if energy is not None:
        normalized_energy = min(1.0, energy / max(0.03, max_audio_energy))
    duration_fit = max(0.0, 1.0 - abs(duration - target) / max(target, 1.0))
    opening_speech = None
    opening_emphasis = 0.0
    if tokens:
        first = overlaps[0]
        lead_in = float(first["start"]) - start
        if lead_in < 0:
            opening_speech = 0.0  # Starts inside a word.
        else:
            opening_speech = max(0.0, 1.0 - max(0.0, lead_in - 0.15) / 1.35)
            opening_text = " ".join(w["text"] for w in overlaps if float(w["end"]) <= start + 2.0)
            if re.search(r"[!?]", opening_text) or any(
                token.lower() in _EMPHASIS_WORDS for token in _WORD_RE.findall(opening_text)
            ):
                opening_emphasis = opening_speech
    opening_motion = max(
        (motion for sample, motion in zip(visual_samples, motion_values)
         if float(sample["time"]) <= start + 2.0),
        default=None,
    )

    # Missing audio/confidence is neutral rather than penalizing valid text-only clips.
    evidence: dict[str, float | None] = {
        "speech_coverage": round(speech_coverage, 4),
        "transcript_density": round(density_score, 4),
        "transcription_confidence": round(confidence, 4) if confidence is not None else None,
        "emphasis": round(emphasis_score, 4),
        "sentence_boundaries": round(boundary_score, 4),
        "boundary_alignment": round(boundary_alignment, 4),
        "visual_cut_alignment": round(visual_cut_alignment, 4),
        "visual_motion": round(visual_motion, 4) if visual_motion is not None else None,
        "visual_scene_changes": round(visual_scene_changes, 4),
        "audio_energy": round(normalized_energy, 4) if normalized_energy is not None else None,
        "duration_fit": round(duration_fit, 4),
        "opening_speech": round(opening_speech, 4) if opening_speech is not None else None,
        "opening_emphasis": round(opening_emphasis, 4) if opening_speech is not None else None,
        "opening_motion": round(opening_motion, 4) if opening_motion is not None else None,
    }
    parts: list[tuple[float, float]] = []
    if tokens:
        parts.extend([
            (0.35, speech_coverage),
            (0.25, density_score),
            (0.12, emphasis_score),
            (0.12, boundary_score),
            (0.10, boundary_alignment),
            (0.45, opening_speech or 0.0),
            (0.12, opening_emphasis),
        ])
        if confidence is not None:
            parts.append((0.06, confidence))
    if visual_motion is not None:
        parts.append((0.45, visual_motion))
        parts.append((0.20, visual_scene_changes))
        parts.append((0.10, visual_cut_alignment))
    if not tokens and opening_motion is not None:
        parts.append((0.25, opening_motion))
    if energy is not None and tokens:
        # Loudness is only a small supporting cue when speech is present.
        parts.append((0.05, normalized_energy or 0.0))
    if parts:
        parts.append((0.04, duration_fit))
    total_weight = sum(weight for weight, _ in parts)
    score = sum(weight * value for weight, value in parts) / total_weight if total_weight else 0.0
    return round(min(1.0, max(0.0, score)), 4), evidence


def rank_candidates(
    duration: float,
    words: Sequence[Mapping[str, Any]],
    *,
    audio: Any = None,
    sample_rate: int = 16_000,
    target_durations: Sequence[float] = (15.0, 30.0, 45.0),
    shot_boundaries: Sequence[float] = (),
    visual_activity: Sequence[Mapping[str, Any]] = (),
    min_duration: float = 6.0,
    max_duration: float = 60.0,
    max_candidates: int = 10,
) -> list[dict[str, Any]]:
    """Rank varied clip windows using transcript, sentence, energy, and duration evidence."""
    try:
        import numpy as np
    except ImportError as exc:
        raise AnalysisError("Candidate audio scoring requires numpy") from exc
    duration = max(0.0, float(duration))
    if duration <= 0 or max_candidates <= 0:
        return []
    normalized = _normalize_words(words)
    boundaries = find_sentence_boundaries(normalized)
    speech_openings = set()
    previous_end = 0.0
    previous_text = ""
    for word in normalized:
        onset = word["start"]
        gap = onset - previous_end
        if not previous_text or gap >= 0.45 or (gap >= 0.08 and _SENTENCE_END_RE.search(previous_text)):
            # Keep a breath of lead-in without dragging in the preceding sentence.
            speech_openings.add(max(previous_end, onset - 0.12))
        previous_end = max(previous_end, word["end"])
        previous_text = word["text"]
    valid_targets = [float(t) for t in target_durations if float(t) > 0]
    if not valid_targets:
        valid_targets = [min(30.0, duration)]
    max_audio_energy = 0.0
    if audio is not None and len(audio):
        chunk = max(1, sample_rate)
        energies = [
            float(np.sqrt(np.mean(np.asarray(audio[i:i + chunk], dtype=np.float32) ** 2)))
            for i in range(0, len(audio), chunk)
        ]
        max_audio_energy = max(energies, default=0.0)
    max_audio_energy = max(max_audio_energy, 0.03)

    windows: set[tuple[float, float, float]] = set()
    # Cover the requested short, medium, and long clip bands, rather than only
    # producing one exact length per target. Each duration remains a ranking target.
    requested_lengths = {
        min(max_duration, max(min_duration, value))
        for value in (15.0, 22.5, 30.0, 37.5, 45.0, 52.5, 60.0)
    }
    requested_lengths.update(min(max_duration, max(min_duration, t)) for t in valid_targets)
    for requested in sorted(requested_lengths):
        clip_length = min(duration, requested)
        if duration < min_duration:
            clip_length = duration
        target = min(valid_targets, key=lambda value: abs(value - requested))
        if clip_length <= 0:
            continue
        last_start = max(0.0, duration - clip_length)
        step = max(1.0, min(5.0, clip_length / 4.0))
        starts = set(float(i) for i in np.arange(0.0, last_start + 0.001, step))
        starts.add(last_start)
        starts.update(start for start in speech_openings if start <= last_start)
        # Include windows anchored around sentence endings, in addition to the grid.
        for boundary in boundaries:
            starts.add(min(last_start, max(0.0, boundary - clip_length * 0.75)))
            starts.add(min(last_start, max(0.0, boundary - clip_length)))
        # A visual cut can anchor a clip, but never move a boundary through a
        # transcribed word. The regular grid remains available in either case.
        for cut in shot_boundaries:
            cut = float(cut)
            if not any(float(w["start"]) < cut < float(w["end"]) for w in normalized):
                starts.add(min(last_start, max(0.0, cut)))
                starts.add(min(last_start, max(0.0, cut - clip_length)))
        for start in starts:
            end = min(duration, start + clip_length)
            if end - start >= min(min_duration, duration):
                windows.add((round(start, 3), round(end, 3), target))

    scored = []
    for start, end, target in windows:
        score, evidence = _candidate_score(
            start, end, normalized, boundaries, audio, sample_rate, target,
            max_audio_energy, shot_boundaries, visual_activity,
        )
        excerpt_words = [
            w["text"] for w in normalized
            if float(w["end"]) > start and float(w["start"]) < end
        ]
        candidate = {
            "start": start,
            "end": end,
            "duration": round(end - start, 3),
            "score": score,
            "evidence": evidence,
            "text": " ".join(excerpt_words),
        }
        has_transcript_evidence = evidence["transcript_density"] not in (None, 0.0)
        has_visual_evidence = (
            evidence["visual_motion"] is not None
            and (
                float(evidence["visual_motion"]) >= 0.01
                or float(evidence["visual_scene_changes"] or 0.0) > 0
            )
        )
        if not has_transcript_evidence and not has_visual_evidence:
            continue
        scored.append(candidate)
    scored.sort(key=lambda c: (-c["score"], c["start"], c["duration"]))

    # Suppress near-identical windows while retaining genuinely different lengths.
    selected = []
    for candidate in scored:
        duplicate = False
        candidate_tokens = set(_WORD_RE.findall(candidate["text"].lower()))
        for existing in selected:
            overlap = max(
                0.0,
                min(candidate["end"], existing["end"]) - max(candidate["start"], existing["start"]),
            )
            shorter = min(candidate["duration"], existing["duration"])
            length_similarity = shorter / max(candidate["duration"], existing["duration"])
            if shorter > 0 and length_similarity >= 0.8 and overlap / shorter >= 0.8:
                duplicate = True
                break
            existing_tokens = set(_WORD_RE.findall(existing["text"].lower()))
            union = candidate_tokens | existing_tokens
            similarity = len(candidate_tokens & existing_tokens) / len(union) if union else 0.0
            if length_similarity >= 0.8 and similarity >= 0.85:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
            if len(selected) >= max_candidates:
                break
    return selected


def _decode_video_frame(
    media_path: str | os.PathLike[str],
    timestamp: float,
    *,
    ffmpeg_path: str,
    frame_size: tuple[int, int] = (320, 180),
    cancellation_callback: Callable[[], bool] | None = None,
) -> Any:
    """Decode one bounded-size BGR frame using FFmpeg's restricted local input."""
    path = _local_media_path(media_path)
    try:
        import numpy as np
    except ImportError as exc:
        raise AnalysisError("Video analysis requires numpy") from exc
    width, height = frame_size
    result = _run_process(
        [
            ffmpeg_path, "-v", "error", "-nostdin", "-ss", str(max(0.0, timestamp)),
            *_safe_input_options(), "-i", str(path),
            "-frames:v", "1", "-an", "-sn", "-dn",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"format=pix_fmts=bgr24,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ],
        cancellation_callback=cancellation_callback,
    )
    if not result.stdout:
        return None
    expected_size = width * height * 3
    if len(result.stdout) != expected_size:
        raise AnalysisError("FFmpeg returned a malformed video frame")
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape((height, width, 3))


def _sample_geometry(source_width, source_height, frame_width, frame_height):
    scale = min(frame_width / source_width, frame_height / source_height)
    width = max(1, math.floor(source_width * scale + 0.5))
    height = max(1, math.floor(source_height * scale + 0.5))
    return (frame_width - width) // 2, (frame_height - height) // 2, width, height


def _map_padded_box_to_source(
    box: Mapping[str, float],
    source_width: int,
    source_height: int,
    *,
    frame_width: int = 320,
    frame_height: int = 180,
) -> dict[str, float]:
    """Undo centered aspect-ratio padding and resize, clipping to source bounds."""
    if source_width <= 0 or source_height <= 0:
        return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
    pad_x, pad_y, scaled_width, scaled_height = _sample_geometry(
        source_width, source_height, frame_width, frame_height,
    )
    scale_x, scale_y = scaled_width / source_width, scaled_height / source_height
    left = min(source_width, max(0.0, (box["x"] - pad_x) / scale_x))
    top = min(source_height, max(0.0, (box["y"] - pad_y) / scale_y))
    right = min(
        source_width,
        max(left, (box["x"] + box["width"] - pad_x) / scale_x),
    )
    bottom = min(
        source_height,
        max(top, (box["y"] + box["height"] - pad_y) / scale_y),
    )
    return {
        "x": round(left, 1),
        "y": round(top, 1),
        "width": round(round(right, 1) - round(left, 1), 1),
        "height": round(round(bottom, 1) - round(top, 1), 1),
    }


def analyze_visual_cues(
    media_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str = "ffmpeg",
    duration: float | None = None,
    sample_interval: float = 1.0,
    threshold: float = 0.42,
    cancellation_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Extract coarse cut and frame-change cues without requiring OpenCV."""
    _local_media_path(media_path)
    try:
        import numpy as np
    except ImportError as exc:
        raise AnalysisError("Visual analysis requires numpy") from exc
    if duration is None:
        duration = probe_media(
            media_path, ffmpeg_path=ffmpeg_path,
            cancellation_callback=cancellation_callback,
        )["duration"]
    if duration <= 0:
        return {"shot_boundaries": [], "activity": []}
    interval = max(0.25, float(sample_interval), float(duration) / 599.0)
    previous_histogram = None
    previous_gray = None
    cuts: list[float] = []
    activity: list[dict[str, float]] = []
    timestamp = 0.0
    while timestamp < duration:
        _check_cancelled(cancellation_callback)
        frame = _decode_video_frame(
            media_path, timestamp, ffmpeg_path=ffmpeg_path,
            cancellation_callback=cancellation_callback,
        )
        if frame is not None:
            gray = np.mean(frame.astype(np.float32), axis=2)
            histogram, _ = np.histogram(gray, bins=32, range=(0, 256))
            histogram = histogram.astype(np.float64)
            histogram /= max(1.0, histogram.sum())
            motion = (
                float(np.mean(np.abs(gray - previous_gray)) / 255.0)
                if previous_gray is not None else 0.0
            )
            cut_distance = 0.0
            if previous_histogram is not None:
                cut_distance = math.sqrt(max(
                    0.0, 1.0 - float(np.sum(np.sqrt(histogram * previous_histogram))),
                ))
                if cut_distance >= threshold:
                    cuts.append(round(timestamp, 3))
            activity.append({
                "time": round(timestamp, 3),
                "motion": round(min(1.0, max(0.0, motion)), 4),
                "cut_strength": round(min(1.0, max(0.0, cut_distance)), 4),
            })
            previous_histogram = histogram
            previous_gray = gray
        timestamp += interval
    return {"shot_boundaries": cuts, "activity": activity}


def detect_shot_changes(
    media_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str = "ffmpeg",
    duration: float | None = None,
    sample_interval: float = 1.0,
    threshold: float = 0.42,
    cancellation_callback: Callable[[], bool] | None = None,
) -> list[float]:
    """Return coarse cut timestamps using adjacent-frame grayscale histograms.

    This lightweight signal is a candidate-generation hint, not a scene
    detector; frames are sampled at a fixed interval and gradual transitions
    may be missed.
    """
    return analyze_visual_cues(
        media_path, ffmpeg_path=ffmpeg_path, duration=duration,
        sample_interval=sample_interval, threshold=threshold,
        cancellation_callback=cancellation_callback,
    )["shot_boundaries"]


def discover_facecam(
    media_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str = "ffmpeg",
    duration: float | None = None,
    source_width: int | None = None,
    source_height: int | None = None,
    yunet_model_path: str | os.PathLike[str] | None,
    sample_count: int = 8,
    shot_boundaries: Sequence[float] = (),
    cancellation_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Suggest a source-pixel webcam crop using faces as seeds for visible edges.

    ``box`` is the estimated webcam boundary, or None when uncertain.
    ``face_box`` is only diagnostic; ``confidence`` describes YuNet, not
    the boundary.
    """
    fallback = {"detected": False, "box": None, "confidence": 0.0}
    if not yunet_model_path:
        return {**fallback, "reason": "model_not_provided"}
    _local_media_path(media_path)
    model_path = Path(yunet_model_path).expanduser()
    if not model_path.is_file():
        raise AnalysisError(f"YuNet model file was not found locally: {model_path}")
    try:
        import cv2
    except ImportError:
        return {**fallback, "reason": "opencv_unavailable"}
    from .framing import frame_rectangles, stable_rectangle, temporal_rectangle

    if duration is None or not source_width or not source_height:
        media = probe_media(
            media_path, ffmpeg_path=ffmpeg_path,
            cancellation_callback=cancellation_callback,
        )
        duration = media["duration"] if duration is None else duration
        source_width = source_width or media["width"]
        source_height = source_height or media["height"]
    if not source_width or not source_height:
        return {**fallback, "reason": "no_video"}
    try:
        detector = cv2.FaceDetectorYN.create(
            str(model_path), "", (320, 320), 0.6, 0.3, 5000,
        )
    except Exception as exc:
        raise AnalysisError(f"Could not initialize YuNet face detector: {exc}") from exc
    count = min(32, max(1, int(sample_count)))
    timestamps = [duration * (index + 0.5) / count for index in range(count)]
    temporal_times = set(timestamps[::max(1, math.ceil(count / 8))])
    cuts = sorted(max(0.0, float(boundary)) for boundary in shot_boundaries)
    if len(cuts) > 32:
        cuts = [cuts[round(i * (len(cuts) - 1) / 31)] for i in range(32)]
    for boundary in cuts:
        timestamps.extend([boundary, boundary + 0.25])
    timestamps = sorted({value for value in timestamps if value < duration} or {0.0})
    detections = []
    observations = []
    temporal_frames = []
    decoded_samples = 0
    for sample, timestamp in enumerate(timestamps):
        _check_cancelled(cancellation_callback)
        frame = _decode_video_frame(
            media_path, timestamp, ffmpeg_path=ffmpeg_path,
            frame_size=(640, 360),
            cancellation_callback=cancellation_callback,
        )
        if frame is None:
            continue
        decoded_samples += 1
        height, width = frame.shape[:2]
        px, py, sw, sh = _sample_geometry(source_width, source_height, width, height)
        unpadded = frame[py:py + sh, px:px + sw]
        if timestamp in temporal_times:
            temporal_frames.append(unpadded)
        detector.setInputSize((width, height))
        _, faces = detector.detect(frame)
        if faces is None:
            continue
        seeds, sample_detections = [], []
        for face in sorted(faces, key=lambda row: -row[-1])[:5]:
            confidence = float(face[-1])
            x, y, w, h = (float(value) for value in face[:4])
            if not all(map(math.isfinite, (x, y, w, h, confidence))):
                continue
            box = _map_padded_box_to_source(
                {"x": x, "y": y, "width": w, "height": h},
                source_width, source_height, frame_width=width, frame_height=height,
            )
            if min(box["width"], box["height"]) <= 0:
                continue
            detection = {"box": box, "confidence": confidence, "timestamp": timestamp}
            detections.append(detection)
            if confidence >= 0.7:
                sample_detections.append(detection)
                seeds.append((x - px, y - py, w, h))
        for score, rect, index in frame_rectangles(unpadded, seeds):
            observations.append((score, rect, sample, sample_detections[index]))
    if not detections:
        return {**fallback, "reason": "no_face_detected"}

    def summary(group):
        boxes = [d["box"] for d in group]
        x, y = (round(median(box[key] for box in boxes), 1) for key in ("x", "y"))
        right = round(median(box["x"] + box["width"] for box in boxes), 1)
        bottom = round(median(box["y"] + box["height"] for box in boxes), 1)
        return {
            "detected": True,
            "face_box": {
                "x": x, "y": y, "width": round(right - x, 1), "height": round(bottom - y, 1),
            },
            "confidence": round(sum(d["confidence"] for d in group) / len(group), 4),
            "samples": len(group),
            "timestamp": round(median(d["timestamp"] for d in group), 3),
        }

    def webcam_result(webcam):
        (left, top, right, bottom), group = webcam
        region = _map_padded_box_to_source(
            {"x": left + px, "y": top + py, "width": right - left, "height": bottom - top},
            source_width, source_height, frame_width=width, frame_height=height,
        )
        return {**summary(group), "box": region, "framing": "webcam_boundary"}

    webcam = stable_rectangle(observations, decoded_samples)
    if webcam is not None:
        return webcam_result(webcam)

    def iou(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
        a, b = left["box"], right["box"]
        x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
        x2 = min(a["x"] + a["width"], b["x"] + b["width"])
        y2 = min(a["y"] + a["height"], b["y"] + b["height"])
        intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        area_a = a["width"] * a["height"]
        area_b = b["width"] * b["height"]
        union = area_a + area_b - intersection
        return intersection / union if union else 0.0

    clusters = []
    for detection in sorted(detections, key=lambda d: -d["confidence"]):
        cluster = next(
            (group for group in clusters if iou(group[0], detection) >= 0.35),
            None,
        )
        if cluster is None:
            clusters.append([detection])
        elif all(item["timestamp"] != detection["timestamp"] for item in cluster):
            cluster.append(detection)
    stable = max(
        clusters,
        key=lambda group: (len(group), sum(d["confidence"] for d in group) / len(group)),
    )
    mean_confidence = sum(d["confidence"] for d in stable) / len(stable)
    if len(stable) < 2 or mean_confidence < 0.7:
        best_confidence = max(d["confidence"] for d in detections)
        return {
            **fallback,
            "reason": "detection_uncertain",
            "best_confidence": round(best_confidence, 4),
        }
    result = summary(stable)
    if len(stable) >= math.ceil(decoded_samples * 0.6):
        face = result["face_box"]
        seed = (face["x"] * sw / source_width, face["y"] * sh / source_height,
                face["width"] * sw / source_width, face["height"] * sh / source_height)
        webcam = temporal_rectangle(temporal_frames, seed, observations)
        if webcam is not None:
            return webcam_result(webcam)
    return {
        **result,
        "detected": False,
        "box": None,
        "reason": "webcam_boundary_uncertain",
    }


def analyze_media(
    media_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str = "ffmpeg",
    whisper_model: str | os.PathLike[str] = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    cache_dir: str | os.PathLike[str] | None = None,
    cache_transcription: bool = True,
    yunet_model_path: str | os.PathLike[str] | None = None,
    target_durations: Sequence[float] = (15.0, 30.0, 45.0),
    max_candidates: int = 10,
    cancellation_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run local probe, transcription, candidate ranking, and optional facecam discovery.

    Raises :class:`AnalysisError` for operational failures and
    :class:`AnalysisCancelled` when ``cancellation_callback`` returns true.
    """
    _check_cancelled(cancellation_callback)
    media = probe_media(
        media_path, ffmpeg_path=ffmpeg_path,
        cancellation_callback=cancellation_callback,
    )
    visual_cues = (
        analyze_visual_cues(
            media_path, ffmpeg_path=ffmpeg_path, duration=media["duration"],
            cancellation_callback=cancellation_callback,
        )
        if media["video_available"]
        else {"shot_boundaries": [], "activity": []}
    )
    shot_boundaries = visual_cues["shot_boundaries"]
    warnings: list[str] = []
    if media["audio_available"]:
        audio, rate = extract_audio(
            media_path, ffmpeg_path=ffmpeg_path,
            cancellation_callback=cancellation_callback,
        )
        transcript = transcribe_audio(
            media_path, ffmpeg_path=ffmpeg_path, model=whisper_model,
            device=device, compute_type=compute_type,
            cache_dir=cache_dir, cache_transcription=cache_transcription,
            audio=audio, sample_rate=rate,
            cancellation_callback=cancellation_callback,
        )
        candidates = rank_candidates(
            media["duration"], transcript["words"], audio=audio, sample_rate=rate,
            target_durations=target_durations, shot_boundaries=shot_boundaries,
            visual_activity=visual_cues["activity"],
            max_candidates=max_candidates,
        )
        status = "ok" if transcript["words"] else "no_speech"
        if not transcript["words"]:
            status = "no_speech_visual_fallback" if candidates else "no_speech"
            warnings.append(
                "No speech transcript was detected. "
                + (
                    "Candidates use visual motion and scene-change signals only and are not "
                    "estimates of semantic highlight quality."
                    if candidates else
                    "No sufficiently informative visual activity was found, so no candidates were returned."
                )
            )
    else:
        transcript = {
            "text": "", "words": [], "language": None,
            "language_probability": None, "cached": False,
        }
        candidates = rank_candidates(
            media["duration"], [], target_durations=target_durations,
            shot_boundaries=shot_boundaries,
            visual_activity=visual_cues["activity"],
            max_candidates=max_candidates,
        )
        status = "no_audio_visual_fallback" if candidates else "no_audio"
        if candidates:
            warnings.append(
                "No audio stream is present; candidates use visual motion and scene-change "
                "signals only and are not estimates of semantic highlight quality."
            )
        else:
            warnings.append(
                "No audio stream or sufficiently informative visual activity was found; no candidates were returned."
            )
    facecam = (
        discover_facecam(
            media_path, ffmpeg_path=ffmpeg_path, duration=media["duration"],
            source_width=media["width"], source_height=media["height"],
            yunet_model_path=yunet_model_path,
            shot_boundaries=shot_boundaries,
            cancellation_callback=cancellation_callback,
        )
        if media["video_available"]
        else {"detected": False, "box": None, "confidence": 0.0, "reason": "no_video"}
    )
    if facecam.get("reason") == "webcam_boundary_uncertain":
        warnings.append(
            "A face was found, but the webcam boundary is uncertain. Set the webcam crop manually."
        )
    return {
        "status": status,
        "media": media,
        "transcript": transcript,
        "candidates": candidates,
        "facecam": facecam,
        "visual_activity": visual_cues["activity"],
        "warnings": warnings,
    }
