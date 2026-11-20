import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamframe.analysis import (
    AnalysisCancelled,
    AnalysisError,
    _normalize_words,
    _map_padded_box_to_source,
    analyze_media,
    discover_facecam,
    find_sentence_boundaries,
    rank_candidates,
    transcribe_audio,
    _decode_video_frame,
)


class AnalysisTests(unittest.TestCase):
    def test_normalizes_word_timestamps_and_sentence_boundaries(self):
        words = _normalize_words([
            {"start": -1, "end": 0.3, "text": " Hello", "probability": 1.2},
            {"start": 0.4, "end": 0.8, "text": "world!"},
            {"start": 1.0, "end": 1.0, "text": "zero"},
            {"start": 2.0, "end": 1.9, "text": "reversed"},
            {"start": "bad", "end": 1, "text": "discard"},
        ])
        self.assertEqual([word["text"] for word in words], ["Hello", "world!", "zero"])
        self.assertEqual(words[0]["start"], 0)
        self.assertEqual(words[0]["probability"], 1)
        self.assertAlmostEqual(words[2]["end"] - words[2]["start"], 0.04)
        self.assertEqual(find_sentence_boundaries(words), [0.8])

    def test_short_clip_is_kept_with_actual_duration(self):
        candidates = rank_candidates(
            4.2,
            [{"start": 0.2, "end": 3.8, "text": "A short useful moment."}],
            target_durations=(15, 30, 45),
            max_candidates=5,
        )
        self.assertTrue(candidates)
        self.assertTrue(all(candidate["start"] >= 0 for candidate in candidates))
        self.assertTrue(all(candidate["end"] <= 4.2 for candidate in candidates))
        self.assertEqual(candidates[0]["duration"], 4.2)
        self.assertIn("sentence_boundaries", candidates[0]["evidence"])
        self.assertIn("audio_energy", candidates[0]["evidence"])
        self.assertIsInstance(candidates[0]["score"], float)

    def test_candidate_lengths_cover_requested_bands_and_deduplicate(self):
        words = []
        for index in range(120):
            start = index * 0.75
            text = "This is a surprising moment!" if index % 20 == 19 else "interesting event"
            words.append({"start": start, "end": start + 0.4, "text": text})
        candidates = rank_candidates(100, words, max_candidates=30)
        lengths = {round(candidate["duration"]) for candidate in candidates}
        self.assertTrue(any(15 <= length <= 30 for length in lengths))
        self.assertTrue(any(30 <= length <= 45 for length in lengths))
        self.assertTrue(any(45 <= length <= 60 for length in lengths))
        for i, left in enumerate(candidates):
            for right in candidates[i + 1:]:
                length_similarity = min(left["duration"], right["duration"]) / max(
                    left["duration"], right["duration"],
                )
                if length_similarity < 0.8:
                    continue
                overlap = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
                self.assertLess(overlap / min(left["duration"], right["duration"]), 0.8)

    def test_audio_level_alone_does_not_create_candidates(self):
        import numpy as np

        candidates = rank_candidates(
            30, [], audio=np.ones(30 * 100, dtype=np.float32) * 0.1,
            sample_rate=100, target_durations=(15,), max_candidates=3,
        )
        self.assertEqual(candidates, [])

    def test_suggested_clip_opens_on_speech_instead_of_dead_air_or_mid_word(self):
        words = [
            {"start": 5.32 + index * 0.5, "end": 5.72 + index * 0.5,
             "text": "Surprise!" if index == 0 else "reaction"}
            for index in range(24)
        ]
        best, = rank_candidates(25, words, min_duration=15, max_duration=15,
                                target_durations=(15,), max_candidates=1)
        self.assertAlmostEqual(best["start"], 5.2, places=2)
        self.assertEqual(best["evidence"]["opening_speech"], 1)

    def test_invalid_duration_or_candidate_limit_returns_no_candidates(self):
        self.assertEqual(rank_candidates(0, []), [])
        self.assertEqual(rank_candidates(15, [], visual_activity=[{"time": 1, "motion": 1}], max_candidates=0), [])

    def _create_visual_fixture(self, with_audio=False):
        test_media = Path(__file__).parent / (
            ".analysis-silent-with-audio.mp4" if with_audio else ".analysis-no-audio.mp4"
        )
        command = [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:s=160x90:r=5:d=6",
            "-f", "lavfi", "-i", "color=c=white:s=160x90:r=5:d=6",
        ]
        if with_audio:
            command.extend(["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono"])
        command.extend([
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]",
        ])
        command.extend(["-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p"])
        if with_audio:
            command.extend(["-map", "2:a:0", "-c:a", "aac", "-shortest"])
        command.extend(["-y", str(test_media)])
        try:
            subprocess.run(command, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"FFmpeg fixture generation unavailable: {exc}")
        return test_media

    def test_real_no_audio_video_produces_visual_fallback_candidates(self):
        from streamframe.analysis import analyze_media, analyze_visual_cues

        test_media = self._create_visual_fixture()
        try:
            cues = analyze_visual_cues(test_media, duration=12)
            self.assertTrue(cues["activity"])
            self.assertTrue(cues["shot_boundaries"])
            result = analyze_media(
                test_media,
                target_durations=(15,),
                max_candidates=3,
            )
            self.assertEqual(result["status"], "no_audio_visual_fallback")
            self.assertTrue(result["candidates"])
            self.assertTrue(any("visual" in warning for warning in result["warnings"]))
            self.assertIn("visual_motion", result["candidates"][0]["evidence"])
            self.assertTrue(all(
                candidate["evidence"]["transcript_density"] == 0
                for candidate in result["candidates"]
            ))
        finally:
            test_media.unlink(missing_ok=True)

    def test_silent_audio_video_uses_visual_fallback_when_transcript_empty(self):
        from streamframe.analysis import analyze_media

        test_media = self._create_visual_fixture(with_audio=True)
        try:
            with patch(
                "streamframe.analysis.transcribe_audio",
                return_value={
                    "text": "", "words": [], "language": None,
                    "language_probability": None, "cached": False,
                },
            ):
                result = analyze_media(
                    test_media, target_durations=(15,), max_candidates=3,
                )
            self.assertEqual(result["status"], "no_speech_visual_fallback")
            self.assertTrue(result["candidates"])
            self.assertTrue(result["warnings"])
        finally:
            test_media.unlink(missing_ok=True)

    def test_visual_cut_is_not_added_as_boundary_inside_word(self):
        cut = 20.17
        words = [{"start": 20.0, "end": 20.3, "text": "spoken"}]
        candidates = rank_candidates(
            80, words, target_durations=(15,), shot_boundaries=(cut,), max_candidates=100,
        )
        self.assertFalse(any(candidate["start"] == cut for candidate in candidates))
        self.assertFalse(any(candidate["end"] == cut for candidate in candidates))

    def test_face_boxes_unpad_and_scale_for_landscape_and_portrait(self):
        landscape = _map_padded_box_to_source(
            {"x": 100, "y": 45, "width": 60, "height": 90},
            640, 480,
        )
        self.assertEqual(landscape, {
            "x": 160.0, "y": 120.0, "width": 160.0, "height": 240.0,
        })

        portrait = _map_padded_box_to_source(
            {"x": 119, "y": 36, "width": 40, "height": 54},
            1080, 1920,
        )
        self.assertEqual(portrait, {
            "x": 106.9, "y": 384.0, "width": 427.8, "height": 576.0,
        })

        clipped = _map_padded_box_to_source(
            {"x": 105, "y": 170, "width": 250, "height": 30},
            1080, 1920,
        )
        self.assertGreaterEqual(clipped["x"], 0)
        self.assertGreaterEqual(clipped["y"], 0)
        self.assertLessEqual(clipped["x"] + clipped["width"], 1080)
        self.assertLessEqual(clipped["y"] + clipped["height"], 1920)

    def test_transcription_cache_uses_cached_word_timestamps(self):
        import numpy as np

        test_dir = Path(__file__).parent / ".analysis-test-data"
        model_dir = test_dir / "model"
        media_path = test_dir / "media.bin"
        test_dir.mkdir(exist_ok=True)
        model_dir.mkdir(exist_ok=True)
        media_path.write_bytes(b"media")
        fake_segment = SimpleNamespace(
            text=" Hello.",
            words=[SimpleNamespace(start=0.1, end=0.4, word=" Hello.", probability=0.9)],
        )
        def fake_transcribe(audio, **kwargs):
            self.assertIsInstance(audio, np.ndarray)
            return [fake_segment], SimpleNamespace(
                language="en", language_probability=0.99,
            )

        fake_model = SimpleNamespace(transcribe=fake_transcribe)
        try:
            with patch("faster_whisper.WhisperModel", return_value=fake_model, create=True):
                first = transcribe_audio(
                    media_path, model=model_dir, cache_dir=test_dir, cache_transcription=True,
                    audio=np.zeros(16_000, dtype=np.float32),
                )
                with patch("faster_whisper.WhisperModel", side_effect=AssertionError, create=True):
                    second = transcribe_audio(
                        media_path, model=model_dir, cache_dir=test_dir, cache_transcription=True,
                    )
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(second["words"][0]["text"], "Hello.")
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_cancellation_callback_raises_explicit_error(self):
        with self.assertRaises(AnalysisCancelled):
            analyze_media("unused.media", cancellation_callback=lambda: True)

    def test_missing_local_whisper_model_is_explicit(self):
        test_media = Path(__file__).parent / ".analysis-test-media"
        test_media.write_bytes(b"media")
        try:
            with self.assertRaisesRegex(AnalysisError, "will not download"):
                transcribe_audio(
                    test_media, model="model-that-is-not-cached",
                    cache_dir=Path(__file__).parent / ".analysis-test-cache",
                    cache_transcription=False,
                )
        finally:
            test_media.unlink(missing_ok=True)

    def test_facecam_without_model_has_explicit_fallback(self):
        self.assertEqual(
            discover_facecam("video.mp4", yunet_model_path=None),
            {"detected": False, "box": None, "confidence": 0.0, "reason": "model_not_provided"},
        )

    def test_ffmpeg_probe_and_decode_restrict_input_protocols_and_formats(self):
        test_media = Path(__file__).parent / ".analysis-protocol-test"
        test_media.write_bytes(b"media")
        observed = []

        def fake_process(args, **kwargs):
            observed.append(args)
            if Path(args[0]).name == "ffprobe":
                return subprocess.CompletedProcess(
                    args, 0,
                    b'{"format":{"duration":"1"},"streams":[]}', b"",
                )
            return subprocess.CompletedProcess(args, 0, bytes(320 * 180 * 3), b"")

        try:
            with patch("streamframe.analysis._resolve_binary", return_value="/usr/bin/ffprobe"), \
                    patch("streamframe.analysis._run_process", side_effect=fake_process):
                from streamframe.analysis import extract_audio, probe_media

                probe_media(test_media)
                extract_audio(test_media)
                frame = _decode_video_frame(test_media, 0, ffmpeg_path="ffmpeg")
                self.assertEqual(frame.shape, (180, 320, 3))
            self.assertEqual(len(observed), 3)
            for args in observed:
                self.assertIn("-protocol_whitelist", args)
                self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
                self.assertIn("-format_whitelist", args)
                formats = args[args.index("-format_whitelist") + 1].split(",")
                self.assertIn("mov", formats)
                self.assertNotIn("hls", formats)
                self.assertNotIn("concat", formats)
                self.assertIn("-enable_drefs", args)
                self.assertEqual(args[args.index("-enable_drefs") + 1], "0")
                self.assertIn("-use_absolute_path", args)
                self.assertEqual(args[args.index("-use_absolute_path") + 1], "0")
            self.assertEqual(observed[2][observed[2].index("-f", observed[2].index("-pix_fmt")) + 1], "rawvideo")
            self.assertEqual(observed[2][observed[2].index("-pix_fmt") + 1], "bgr24")
        finally:
            test_media.unlink(missing_ok=True)

    def test_decoders_reject_nonlocal_source_paths(self):
        from streamframe.analysis import detect_shot_changes

        with self.assertRaises(AnalysisError):
            transcribe_audio("https://127.0.0.1/private.m3u8")
        with self.assertRaises(AnalysisError):
            detect_shot_changes("https://127.0.0.1/private.m3u8")


if __name__ == "__main__":
    unittest.main()
