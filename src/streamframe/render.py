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

import itertools
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

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
# Round to an even scanline for the 4:2:0 output.
WEBCAM_HEIGHT = 2 * round(OUTPUT_HEIGHT * 0.38 / 2)
SCREEN_HEIGHT = OUTPUT_HEIGHT - WEBCAM_HEIGHT
OUTRO_SECONDS = 1.5
OUTRO_FRAMES = round(OUTRO_SECONDS * 30)
TITLE_VIOLET = "0x392279"
TITLE_SECONDS = 2.4
TITLE_Y = WEBCAM_HEIGHT + 2
# The card sits a little above the divider so mobile playback controls and
# account text at the bottom do not crowd the hook.
TITLE_CARD_Y = TITLE_Y - 28
TITLE_CARD_HEIGHT = 190
TITLE_MAX_CHARS = 90
TITLE_MAX_LINES = 3
TITLE_MAX_SIZE = 62
TITLE_MIN_SIZE = 34
# Text room in the card beside the inset logo, in output pixels. Lines stay a
# little narrower than the full 716 px so a wider fallback font still fits.
TITLE_WIDTH = 686
# Burned-in captions end above Instagram's organic bottom UI margin (319 px),
# so the account row and caption text never cover the last line. Raise this to
# 672 to stay inside Meta's stricter Reels-ad safe area.
CAPTION_MARGIN_V = 319
# Bundled fonts for glyphs the system cannot supply; libass never falls back
# to them on macOS, so emoji runs switch font explicitly.
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
EMOJI_FONT = "Noto Emoji"

# Words that read badly at the end of a line, as in "BREAKS HIS / SETUP".
_TITLE_TRAILING = frozenset(
    "a an the and or but of to in on at for with while his her their our your "
    "my its this that from as by into onto over under".split()
)


def _brand_images(directory: Path) -> tuple[Path, Path]:
    images = directory / "watermark.png", directory / "outro.png"
    if any(not path.is_file() for path in images):
        raise RenderError("Lobby Crew branding requires watermark.png and outro.png")
    return images[0].resolve(), images[1].resolve()


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


def _title_width(line: str) -> float:
    """Approximate advance width of ``line`` in em units.

    The per-character factors come from measuring rendered lines: ASS glyphs
    are narrower than a naive box model, so the sum is scaled to match.
    """
    return 0.8 * sum(
        1.05 if ord(c) > 127 or c in "MW@" else
        0.32 if c in " ilI.,!:;'|" else
        0.69 if c.isupper() else 0.54
        for c in line
    )


def _title_penalty(lines: list[str]) -> float:
    """Penalize breaks that split a short phrase or orphan the last word."""
    penalty = 0.0
    for line in lines[:-1]:
        last = line.rsplit(" ", 1)[-1].lower().strip('.,;:!?"“”()')
        if last in _TITLE_TRAILING:
            penalty += 4
        if line.endswith((",", ";", ":")):
            penalty -= 1
    if len(lines) > 1 and len(lines[-1].split()) == 1:
        penalty += 8
    return penalty


def _branded_title_layout(title: str) -> tuple[list[str], int]:
    """Split the hook into at most three lines and pick the largest font size.

    Every word split is scored instead of taking the shortest pair: breaks
    that keep short phrases together win, and ties prefer a wider last line,
    which is the shape of the approved title card.
    """
    words = title.split()
    if not words:
        return [], TITLE_MAX_SIZE
    best: tuple[tuple[float, float, float], list[str], int] | None = None
    for count in range(1, min(TITLE_MAX_LINES, len(words)) + 1):
        for cuts in itertools.combinations(range(1, len(words)), count - 1):
            bounds = (0, *cuts, len(words))
            lines = [" ".join(words[bounds[i]:bounds[i + 1]]) for i in range(count)]
            widths = [_title_width(line) for line in lines]
            size = min(TITLE_MAX_SIZE, int(TITLE_WIDTH / max(widths)))
            key = (size // 8, -_title_penalty(lines), widths[-1] - max(widths))
            if best is None or key > best[0]:
                best = (key, lines, size)
    _, lines, size = best
    return lines, max(TITLE_MIN_SIZE, size)


def _is_emoji(character: str) -> bool:
    code = ord(character)
    return (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF
            or 0x2B00 <= code <= 0x2BFF or 0x1F1E6 <= code <= 0x1F1FF
            or code in (0x200D, 0xFE0F))


def _title_text(text: str, font: str, size: int) -> str:
    """Switch to the bundled emoji font for emoji runs, then back again."""
    runs = []
    for emoji, group in itertools.groupby(text, _is_emoji):
        run = "".join(group)
        if emoji:
            # Noto Emoji has taller metrics than the title font; drawing it a
            # little smaller keeps the line height inside the card.
            emoji_size = max(1, round(size * 0.9))
            runs.append(rf"{{\fn{EMOJI_FONT}\fs{emoji_size}}}{run}{{\fn{font}\fs{size}}}")
        else:
            runs.append(run)
    return "".join(runs)


def _ass(words: list[tuple[float, float, str]], title: str = "", duration: float = 0,
         outro_start: float | None = None) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,100,100,{CAPTION_MARGIN_V},1
Style: Opening,Arial,58,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00170B25,-1,0,0,0,100,100,0,0,1,0,0,5,158,158,0,1
Style: BrandedTitle,DejaVu Sans,41,&H00FCF4FF,&H00FCF4FF,&H00FCF4FF,&H00170B25,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: Social,Arial,47,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00170B25,-1,0,0,0,100,100,0,0,1,0,0,4,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    title = _display_text(title)
    if title and outro_start is not None:
        end_cs = max(1, round(min(duration, TITLE_SECONDS) * 100))
        prefix = f"0:00:00.00,{_ass_time(end_cs)},BrandedTitle,,0,0,0,,"
        top = TITLE_CARD_Y - TITLE_CARD_HEIGHT // 2
        card = f"m 0 0 l 864 0 864 {TITLE_CARD_HEIGHT} 0 {TITLE_CARD_HEIGHT}"
        events.append(rf"Dialogue: 1,{prefix}{{\an7\pos(108,{top})\bord0\1c&H792239&\p1}}{card}")
        events.append(rf"Dialogue: 2,{prefix}{{\an7\pos(108,{top})\bord0\1c&HDD96B0&\p1}}"
                      f"m 0 0 l 864 0 864 {TITLE_CARD_HEIGHT} 0 {TITLE_CARD_HEIGHT} "
                      f"m 3 3 l 3 {TITLE_CARD_HEIGHT - 3} 861 {TITLE_CARD_HEIGHT - 3} 861 3")
        lines, font_size = _branded_title_layout(title)
        text = r"\N".join(_title_text(line, "DejaVu Sans", font_size) for line in lines)
        events.append(rf"Dialogue: 3,{prefix}{{\pos(614,{TITLE_CARD_Y - 2})\q2\fs{font_size}}}{text}")
    elif title:
        end_cs = max(1, round(min(duration, TITLE_SECONDS) * 100))
        center = WEBCAM_HEIGHT + 5
        prefix = f"0:00:00.00,{_ass_time(end_cs)},Opening,,0,0,0,,"
        card = r"m 24 0 l 816 0 b 829 0 840 11 840 24 l 840 148 b 840 161 829 172 816 172 l 24 172 b 11 172 0 161 0 148 l 0 24 b 0 11 11 0 24 0"
        events.append(rf"Dialogue: 1,{prefix}{{\an7\pos(120,{center - 86})\bord3\1c&H250B17&\1a&H12&\p1}}{card}")
        longest = max(map(len, title.split()))
        font_size = min(58, 730 // longest)
        events.append(rf"Dialogue: 2,{prefix}{{\pos(540,{center})\fs{font_size}}}"
                      f"{_title_text(title, 'Arial', font_size)}")
    offset = 0
    while offset < len(words):
        limit = offset + 1
        while limit < min(len(words), offset + 5) and words[limit][0] - words[limit - 1][1] <= 0.45:
            limit += 1
        phrase = [word[2] for word in words[offset:limit]]
        for i in range(offset, limit):
            start, end, text = words[i]
            stop = min(end, words[i + 1][0]) if i + 1 < len(words) else end
            begin_cs = math.ceil(start * 100 - 1e-8)
            stop_cs = math.floor(stop * 100 + 1e-8)
            if stop_cs > begin_cs:
                parts = phrase.copy()
                parts[i - offset] = rf"{{\c&H00FFFF&}}{text}{{\c&HFFFFFF&}}"
                events.append(f"Dialogue: 0,{_ass_time(begin_cs)},{_ass_time(stop_cs)},Default,,0,0,0,,{' '.join(parts)}")
        offset = limit
    if outro_start is not None:
        end_stamp = _ass_time(round((outro_start + OUTRO_SECONDS) * 100))
        camera = ("m 11 0 l 31 0 b 37 0 42 5 42 11 l 42 31 b 42 37 37 42 31 42 "
                  "l 11 42 b 5 42 0 37 0 31 l 0 11 b 0 5 5 0 11 0 "
                  "m 11 4 b 7 4 4 7 4 11 l 4 31 b 4 35 7 38 11 38 l 31 38 "
                  "b 35 38 38 35 38 31 l 38 11 b 38 7 35 4 31 4 l 11 4 "
                  "m 21 11 b 35 11 35 31 21 31 b 7 31 7 11 21 11 "
                  "m 21 15 b 13 15 13 27 21 27 b 29 27 29 15 21 15 "
                  "m 33 6 b 37 6 37 12 33 12 b 29 12 29 6 33 6")
        note = ("m 22 0 l 29 0 b 30 7 34 10 42 11 l 42 18 b 36 18 32 16 29 14 l 29 30 "
                "b 29 39 22 44 14 43 b 3 42 0 32 5 26 b 9 21 16 20 20 21 l 20 29 "
                "b 15 27 10 30 12 34 b 14 38 22 36 22 30 l 22 0")
        play = ("m 8 0 l 34 0 b 39 0 42 3 42 8 l 42 22 b 42 27 39 30 34 30 "
                "l 8 30 b 3 30 0 27 0 22 l 0 8 b 0 3 3 0 8 0 m 17 7 l 17 23 30 15")
        for index, (y, icon, top_offset, handle) in enumerate((
            (1310, camera, 21, "@thelobbycrew"),
            (1390, note, 21, "@thelobbycrew"),
            (1470, play, 15, "@TheLobbyCrew"),
        )):
            stamp = f"{_ass_time(round((outro_start + 0.07 + index * 0.12) * 100))},{end_stamp}"
            top = y - top_offset
            events.append(rf"Dialogue: 1,{stamp},Social,,0,0,0,,{{\an7\move(350,{top + 24},350,{top},0,300)\fad(220,0)\p1}}{icon}")
            events.append(rf"Dialogue: 1,{stamp},Social,,0,0,0,,{{\move(422,{y + 24},422,{y},0,300)\fad(220,0)}}{handle}")
    return header + "\n".join(events) + "\n"


def _ass_filter(name: str, source: str = "") -> str:
    """Build an ``ass`` filter that can also reach the bundled glyph fonts."""
    value = f"filename='{name}'"
    if FONT_DIR.is_dir():
        fonts = str(FONT_DIR).replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")
        value += f":fontsdir='{fonts}'"
    return f"{source}ass={value}"


def _filter_crop(rect: tuple[float, float, float, float], *, fit: bool = False) -> str:
    x, y, width, height = rect
    panel_height = WEBCAM_HEIGHT if fit else SCREEN_HEIGHT
    size = f"{OUTPUT_WIDTH}:{panel_height}"
    source_crop = f"crop=iw*{width:.12g}:ih*{height:.12g}:iw*{x:.12g}:ih*{y:.12g}:exact=1"
    if fit:
        fitted = f"scale={size}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1"
        return f"{source_crop},{fitted},pad={size}:(ow-iw)/2:(oh-ih)/2:color=0x141C22,setsar=1"
    return (f"{source_crop},"
            f"scale={size}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos,"
            f"crop={size},setsar=1")


def _divider(branded: bool) -> str:
    if branded:
        return (f"drawbox=x=0:y={WEBCAM_HEIGHT - 2}:w=iw:h=8:color=0xD6B6FF:t=fill,"
                f"drawbox=x=0:y={WEBCAM_HEIGHT + 6}:w=iw:h=2:color={TITLE_VIOLET}:t=fill")
    return (f"drawbox=x=0:y={WEBCAM_HEIGHT}:w=iw:h=8:color=0x141C22:t=fill,"
            f"drawbox=x=0:y={WEBCAM_HEIGHT + 2}:w=iw:h=4:color=0x68877F:t=fill")


def _brand_video(frames: int, title_seconds: float) -> str:
    logo = "[2:v]split[outro_logo][badge_logo];" if title_seconds else ""
    outro_input = "[outro_logo]" if title_seconds else "[2:v]"
    return (
        f";[stack]fps=30,trim=end_frame={frames},setpts=N/(30*TB),split[main][tail];"
        # The bundled wordmark is authored 172px wide, so this renders it 1:1.
        "[1:v]scale=172:-2:flags=lanczos,format=rgba,colorchannelmixer=aa=0.53[mark];"
        f"[main][mark]overlay=x=(W-w)/2:y={TITLE_Y}-h/2:enable='gte(t,{title_seconds:.2f})'[marked];"
        f"[tail]trim=start_frame={max(0, frames - OUTRO_FRAMES)},setpts=PTS-STARTPTS,"
        f"scale=270:480,gblur=sigma=12:steps=3,scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        f"drawbox=t=fill:color=black@0.52,tpad=stop_mode=clone:stop={max(0, OUTRO_FRAMES - frames)}[card];"
        f"{logo}{outro_input}scale=900:900:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
        "format=yuva420p,pad=1200:1200:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"zoompan=z='1+0.23*(1-pow(1-min(on/{OUTRO_FRAMES - 1},1),2))':"
        f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={OUTRO_FRAMES}:s=640x640:fps=30,format=yuva420p[logo];"
        "[card][logo]overlay=(W-w)/2:y='575+54*pow(max(1-t/0.55,0),3)':shortest=1[endcard];"
        "[marked][endcard]concat=n=2:v=1:a=0,setpts=N/(30*TB)[finished]"
    )


def _outro_audio(clip_seconds: float, clip_frames: int, loudness: float | None) -> str:
    normalize = f",loudnorm=I={loudness:.6g}:TP=-1.5:LRA=11" if loudness is not None else ""
    clip_samples = clip_frames * 1600
    outro_samples = OUTRO_FRAMES * 1600
    source_samples = min(clip_samples, max(1, round(clip_seconds * 48000)))
    fade_samples = min(3840, source_samples)
    return (f";[0:a:0]atrim=end={clip_seconds:.9f},asetpts=PTS-STARTPTS{normalize},"
            "aformat=sample_rates=48000:channel_layouts=stereo,"
            f"atrim=end_sample={clip_samples},afade=t=out:ss={source_samples - fade_samples}:ns={fade_samples},"
            f"apad=whole_len={clip_samples + outro_samples}[aout]")


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
    brand_dir: Path | None = None,
    opening_title: str = "",
) -> Path:
    """Render and atomically publish an MP4; return its absolute path.

    ``start`` and ``end`` are source-relative seconds, with an exclusive end.
    Each crop is a normalized ``(x, y, width, height)`` rectangle. The full
    facecam rectangle fits inside the top 38% with padding as needed; the content
    rectangle is center-cropped to fill the bottom 62%. ``words`` is
    ordered by start time, with absolute source-second ``start``/``end`` and
    ``text`` strings, plus optional string ``correction`` (including empty).
    Captions are placed inside 100px horizontal/250px bottom safe margins.
    ``include_audio=False`` omits audio; absent source audio is also allowed.
    ``cancel``, if supplied, is polled and raises RenderCancelled when true.
    ``max_output_bytes`` enforces a POSIX per-file limit on FFmpeg and rejects
    truncated or oversized output before publication.
    ``brand_dir`` is an optional local Lobby Crew preset with watermark.png
    and outro.png. It adds a centered watermark, title badge, and animated
    silent outro over a blurred replay, after a brief source-audio fade.
    ``opening_title`` overlays up to 90 characters without adding footage.
    Both tools may be executable paths. Existing output survives any failure.
    """
    start = _number(start, "start")
    end = _number(end, "end")
    if start < 0 or end <= start:
        raise ValueError("clip must have 0 <= start < end")
    face = _crop(facecam_crop, "facecam_crop")
    content = _crop(content_crop, "content_crop")
    captions = _words(words, start, end)
    if not isinstance(opening_title, str) or len(opening_title) > TITLE_MAX_CHARS:
        raise ValueError(f"Opening title must be a string of at most {TITLE_MAX_CHARS} characters")
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

    images = _brand_images(brand_dir) if brand_dir is not None else ()
    clip_seconds = end - start
    clip_frames = max(1, math.ceil(clip_seconds * 30 - 1e-8))
    expected_duration = clip_frames / 30 + OUTRO_SECONDS if images else clip_seconds
    token = uuid.uuid4().hex
    subtitle_path = output_path.parent / f".streamframe-{token}.ass"
    partial_path = output_path.parent / f".streamframe-{token}.mp4"
    try:
        webcam = _filter_crop(face, fit=True)
        graph = (f"[0:v:0]split=2[face][content];[face]{webcam}[top];"
                 f"[content]{_filter_crop(content)}[bottom];"
                 f"[top][bottom]vstack=inputs=2,{_divider(bool(images))}[stack]")
        if images:
            title_seconds = round(min(clip_seconds, TITLE_SECONDS) * 100) / 100 if _display_text(opening_title) else 0
            if _display_text(opening_title):
                title_seconds = max(0.01, title_seconds)
            subtitle_path.write_text(_ass(captions, opening_title, clip_seconds, clip_frames / 30),
                                     encoding="utf-8")
            graph += _brand_video(clip_frames, title_seconds)
            graph += ";" + _ass_filter(subtitle_path.name, "[finished]")
            if title_seconds:
                graph += ("[titled];[badge_logo]scale=124:124:force_original_aspect_ratio=decrease:"
                          f"force_divisible_by=2:flags=lanczos,pad=124:124:(ow-iw)/2:(oh-ih)/2:color={TITLE_VIOLET}[badge];"
                          f"[titled][badge]overlay=x=132:y={TITLE_CARD_Y - 62}:enable='lt(t,{title_seconds:.2f})'")
            graph += "[decorated]"
        elif captions or _display_text(opening_title):
            subtitle_path.write_text(_ass(captions, opening_title, clip_seconds), encoding="utf-8")
            # The generated basename contains no filter-grammar metacharacters.
            graph += ";" + _ass_filter(subtitle_path.name, "[stack]") + "[captioned]"
        else:
            graph += ";[stack]null[captioned]"
        video = "decorated" if images else "captioned"
        graph += f";[{video}]scale=in_range=auto:out_range=tv,format=yuv420p,setparams=range=limited[out]"
        if audio and images:
            graph += _outro_audio(clip_seconds, clip_frames, loudness_target_lufs)
        source_formats = str(info.get("format", {}).get("format_name", "")).split(",")
        input_options = _INPUT_OPTIONS + (_MOV_OPTIONS if "mov" in source_formats else [])
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                *input_options, "-ss", f"{start:.9f}", "-i", str(source_path)]
        for image in images:
            args += ["-protocol_whitelist", "file", "-f", "image2", "-pattern_type", "none",
                     "-framerate", "30", "-c:v", "png", "-i", str(image)]
        args += ["-filter_complex", graph, "-map", "[out]"]
        if audio:
            args += ["-map", "[aout]" if images else "0:a:0", "-c:a", "aac", "-b:a", "192k"]
            if not images and loudness_target_lufs is not None:
                args += ["-af", f"loudnorm=I={loudness_target_lufs:.6g}:TP=-1.5:LRA=11"]
        else:
            args += ["-an"]
        args += ["-t", f"{expected_duration:.9f}", "-r", "30", "-fps_mode", "cfr",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv",
                 "-movflags", "+faststart"]
        if max_output_bytes is None:
            args += ["-preset", "medium", "-crf", "17"]
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
        if not math.isfinite(actual_duration) or abs(actual_duration - expected_duration) > 0.1:
            raise RenderError(f"rendered duration {actual_duration} does not match expected duration {expected_duration}")
        try:
            video_duration = float(rendered_video[0]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderError("rendered video has no valid duration") from exc
        if not math.isfinite(video_duration) or abs(video_duration - expected_duration) > 0.1:
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
    brand_dir: Path | None = None,
) -> Path:
    """Render a job candidate using dictionary crops and a threading.Event.

    Candidate keys are ``start``, ``end``, optional ``words``, and optional
    ``face_crop``/``content_crop`` dictionaries with x/y/width/height. Missing
    screen crops use the full source; a webcam crop is required. ``progress`` takes a fraction
    and message; ``cancelled`` is an Event exposing ``is_set()``.
    """
    def rectangle(name: str) -> tuple[float, float, float, float]:
        value = candidate.get(name)
        if value is None:
            if name == "face_crop":
                raise ValueError("Set the webcam crop before exporting.")
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
        brand_dir=brand_dir,
        opening_title=candidate.get("opening_title", ""),
    )
    if progress:
        progress(1, "Clip rendered and validated")
    return result
