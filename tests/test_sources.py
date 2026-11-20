"""Resolver tests never contact a platform or use user credentials."""

import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from streamframe import sources


YOUTUBE_VIDEO = "https://www.youtube.com/watch?v=abcdefghijk"
TWITCH_VIDEO = "https://www.twitch.tv/videos/123456"


def video(url=YOUTUBE_VIDEO, **extra):
    return {
        "id": "abcdefghijk", "title": "Public broadcast", "webpage_url": url,
        "availability": "public", "live_status": "was_live", "is_live": False,
        "formats": [{"url": "https://cdn.example/video.m3u8", "vcodec": "avc1",
                     "protocol": "m3u8_native"}],
        **extra,
    }


class TargetTests(unittest.TestCase):
    def test_twitch_username_is_default(self):
        target = sources._target("Some_Streamer")
        self.assertEqual(target.url, "https://www.twitch.tv/some_streamer")
        self.assertEqual(target.platform, "twitch")
        self.assertTrue(target.channel)

    def test_youtube_handle_requires_explicit_platform(self):
        self.assertEqual(
            sources._target("@some_creator", "youtube").url,
            "https://www.youtube.com/@some_creator",
        )
        for value in ("@some_creator", "Some Creator", "SomeCreator"):
            with self.subTest(value=value), self.assertRaises(sources.SourceResolutionError):
                sources._target(value, "youtube" if value != "@some_creator" else None)

    def test_canonical_urls(self):
        cases = {
            "https://youtu.be/abcdefghijk?t=3": YOUTUBE_VIDEO,
            "https://m.youtube.com/shorts/abcdefghijk": YOUTUBE_VIDEO,
            "https://youtube.com/live/abcdefghijk": YOUTUBE_VIDEO,
            "https://youtube.com/@creator/live": "https://www.youtube.com/@creator",
            "https://www.youtube.com/channel/UC" + "a" * 22 + "/streams":
                "https://www.youtube.com/channel/UC" + "a" * 22,
            "https://twitch.tv/Creator/videos?filter=archives": "https://www.twitch.tv/creator",
            "https://twitch.tv/videos/123456?t=1m": TWITCH_VIDEO,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(sources._target(value).url, expected)

    def test_untrusted_or_private_network_inputs_never_reach_subprocess(self):
        invalid = [
            "--exec=touch", "-h", "http://twitch.tv/creator", "file:///etc/passwd",
            "https://127.0.0.1/videos/123", "https://[::1]/watch?v=abcdefghijk",
            "https://169.254.169.254/", "https://twitch.tv.evil.test/creator",
            "https://twitch.tv@127.0.0.1/creator", "https://user:pass@twitch.tv/creator",
            "https://twitch.tv:443/creator", "https://twitch.tv/creator#fragment",
            "https://twitch.tv\\@evil.test/creator", "https://youtube.com/c/DisplayName",
            "https://youtube.com/playlist?list=abc", "https://youtube.com/results?search_query=creator",
            "https://youtube.com/watch?v=abcdefghijk&v=12345678901",
            "https://twitch.tv/creator\n--exec=bad",
        ]
        with patch.object(sources.subprocess, "Popen") as process:
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(sources.SourceResolutionError):
                    sources.resolve_source(value)
            process.assert_not_called()

    def test_platform_mismatch_and_invalid_mode(self):
        with self.assertRaises(sources.SourceResolutionError):
            sources.resolve_source(YOUTUBE_VIDEO, platform="twitch")
        with self.assertRaises(sources.SourceResolutionError):
            sources.resolve_source("creator", mode="random")


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.proxy_patch = patch("streamframe.jobs.DownloadProxy")
        self.proxy = self.proxy_patch.start()
        self.addCleanup(self.proxy_patch.stop)
        self.proxy.return_value.__enter__.return_value = "http://127.0.0.1:1234"
        self.metadata_patch = patch.object(sources, "_metadata")
        self.metadata = self.metadata_patch.start()
        self.addCleanup(self.metadata_patch.stop)

    def resolve(self, *args, **kwargs):
        return sources.resolve_source(*args, **kwargs)

    def test_auto_prefers_live_and_reports_selected_source(self):
        self.metadata.return_value = video(
            url="https://www.twitch.tv/creator", live_status="is_live", is_live=True,
        )
        result = self.resolve("creator")
        self.assertTrue(result.is_live)
        self.assertEqual(result.selection, "live")
        self.assertEqual(result.requested_mode, "auto")
        self.assertEqual(result.url, "https://www.twitch.tv/creator")
        self.metadata.assert_called_once()
        self.proxy.assert_called_once()
        self.assertEqual(self.proxy.call_args.kwargs["limit"], sources.MAX_METADATA_BYTES)

    def test_auto_offline_uses_latest_finished_public_archive(self):
        self.metadata.side_effect = [
            sources._Offline(),
            {"entries": [
                {"url": "https://youtube.com/watch?v=11111111111", "live_status": "is_upcoming"},
                {"url": "https://youtube.com/watch?v=22222222222", "availability": "private"},
                {"url": "https://youtube.com/watch?v=33333333333"},
                {"url": YOUTUBE_VIDEO},
            ]},
            video(url="https://youtube.com/watch?v=33333333333", live_status="post_live"),
            video(),
        ]
        result = self.resolve("@creator", platform="youtube")
        self.assertEqual(result.selection, "latest")
        self.assertFalse(result.is_live)
        self.assertEqual(result.url, YOUTUBE_VIDEO)
        self.assertEqual(self.metadata.call_args_list[0].args[0], "https://www.youtube.com/@creator/live")
        self.assertEqual(self.metadata.call_args_list[1].args[0], "https://www.youtube.com/@creator/streams")

    def test_latest_skips_live_probe(self):
        self.metadata.side_effect = [{"entries": [{"url": YOUTUBE_VIDEO}]}, video()]
        result = self.resolve("https://youtube.com/@creator", mode="latest")
        self.assertEqual(result.selection, "latest")
        self.assertTrue(self.metadata.call_args_list[0].kwargs["flat"])
        self.assertEqual(self.metadata.call_count, 2)

    def test_youtube_channel_url_auto_prefers_live(self):
        self.metadata.return_value = video(live_status="is_live", is_live=True)
        result = self.resolve("https://www.youtube.com/@IShowSpeed", mode="auto")
        self.assertEqual(result.url, YOUTUBE_VIDEO)
        self.assertEqual(result.selection, "live")
        self.assertEqual(self.metadata.call_args.args[0], "https://www.youtube.com/@IShowSpeed/live")

    def test_youtube_channel_latest_skips_live_and_growing_archives(self):
        self.metadata.side_effect = [
            {"entries": [{"url": "https://youtube.com/watch?v=11111111111"},
                         {"url": "https://youtube.com/watch?v=22222222222"},
                         {"url": YOUTUBE_VIDEO}]},
            video(url="https://youtube.com/watch?v=11111111111", live_status="is_live", is_live=True),
            video(url="https://youtube.com/watch?v=22222222222", live_status="post_live"),
            video(),
        ]
        result = self.resolve("https://www.youtube.com/@IShowSpeed", mode="latest")
        self.assertEqual(result.url, YOUTUBE_VIDEO)
        self.assertEqual(result.selection, "latest")
        self.assertEqual(self.metadata.call_args_list[0].args[0], "https://www.youtube.com/@IShowSpeed/streams")

    def test_candidate_metadata_failure_preserves_safe_cause(self):
        self.metadata.side_effect = [
            {"entries": [{"url": YOUTUBE_VIDEO}]},
            sources._MetadataFailure("The provider rate-limited the metadata request; try again later"),
        ]
        with self.assertRaisesRegex(sources.SourceResolutionError, "rate-limited"):
            self.resolve("@creator", platform="youtube", mode="latest")

    def test_live_mode_offline_does_not_fall_back(self):
        self.metadata.side_effect = sources._Offline()
        with self.assertRaisesRegex(sources.SourceResolutionError, "not currently live"):
            self.resolve("creator", mode="live")
        self.metadata.assert_called_once()

    def test_live_probe_failure_is_not_evidence_channel_is_offline(self):
        self.metadata.side_effect = sources.SourceResolutionError("Network unavailable")
        with self.assertRaisesRegex(sources.SourceResolutionError, "Network unavailable"):
            self.resolve("creator")
        self.metadata.assert_called_once()

    def test_missing_live_status_is_not_assumed_offline_or_finished(self):
        self.metadata.return_value = video(live_status=None)
        with self.assertRaisesRegex(sources.SourceResolutionError, "whether the channel is live"):
            self.resolve("https://youtube.com/@creator")
        self.metadata.assert_called_once()
        with self.assertRaisesRegex(sources.SourceResolutionError, "verified finished"):
            self.resolve(YOUTUBE_VIDEO)

    def test_direct_video_modes_distinguish_live_from_finished(self):
        self.metadata.return_value = video()
        self.assertEqual(self.resolve(YOUTUBE_VIDEO).selection, "video")
        with self.assertRaises(sources.SourceResolutionError):
            self.resolve(YOUTUBE_VIDEO, mode="live")
        self.metadata.return_value = video(live_status="is_live", is_live=True)
        self.assertTrue(self.resolve(YOUTUBE_VIDEO, mode="live").is_live)
        with self.assertRaisesRegex(sources.SourceResolutionError, "still live"):
            self.resolve(YOUTUBE_VIDEO, mode="latest")

    def test_direct_video_refuses_upcoming_post_live_private_unplayable(self):
        cases = [
            video(live_status="is_upcoming"), video(live_status="post_live"),
            video(availability="private"), video(availability="subscriber_only"),
            video(formats=[]), video(formats=[{"url": "https://example.test/a", "vcodec": "none"}]),
            video(_type="playlist"),
        ]
        for info in cases:
            self.metadata.return_value = info
            with self.subTest(info=info), self.assertRaises(sources.SourceResolutionError):
                self.resolve(YOUTUBE_VIDEO)

    def test_malicious_candidate_urls_never_become_downloader_arguments(self):
        self.metadata.side_effect = [
            {"entries": [{"url": "http://127.0.0.1/private"}, {"url": "--exec=bad"},
                         {"url": "https://twitch.tv/another"}, {"url": YOUTUBE_VIDEO}]},
            video(),
        ]
        self.assertEqual(self.resolve("@creator", platform="youtube", mode="latest").url, YOUTUBE_VIDEO)
        self.assertEqual(self.metadata.call_count, 2)

    def test_returned_metadata_cannot_redirect_result_to_private_host(self):
        self.metadata.return_value = video(url="https://127.0.0.1/watch?v=abcdefghijk")
        with self.assertRaises(sources.SourceResolutionError):
            self.resolve(YOUTUBE_VIDEO)

    def test_twitch_vod_needs_completed_manifest_even_with_duration(self):
        self.metadata.return_value = video(url=TWITCH_VIDEO, live_status=None, duration=3600)
        with patch.object(sources, "_manifest_finished", return_value=False):
            with self.assertRaises(sources.SourceResolutionError):
                self.resolve(TWITCH_VIDEO)
        with patch.object(sources, "_manifest_finished", return_value=True):
            self.assertFalse(self.resolve(TWITCH_VIDEO).is_live)

    def test_latest_twitch_listing_and_finished_vod(self):
        self.metadata.side_effect = [
            {"entries": [{"url": TWITCH_VIDEO}]},
            video(url=TWITCH_VIDEO, live_status=None),
        ]
        with patch.object(sources, "_manifest_finished", return_value=True):
            result = self.resolve("creator", mode="latest")
        self.assertEqual(result.url, TWITCH_VIDEO)
        self.assertEqual(
            self.metadata.call_args_list[0].args[0],
            "https://www.twitch.tv/creator/videos?filter=archives&sort=time",
        )

    def test_cancelled_and_expired_resolution_do_not_start_proxy(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(sources.SourceResolutionCancelled):
            self.resolve("creator", cancelled=event)
        with self.assertRaisesRegex(sources.SourceResolutionError, "30-second"):
            self.resolve("creator", deadline=time.monotonic() - 1)
        self.proxy.assert_not_called()

    def test_cancellation_between_candidates_is_not_swallowed(self):
        self.metadata.side_effect = [
            {"entries": [{"url": YOUTUBE_VIDEO}]}, sources.SourceResolutionCancelled("cancelled"),
        ]
        with self.assertRaises(sources.SourceResolutionCancelled):
            self.resolve("@creator", platform="youtube", mode="latest")

    def test_metadata_limit_between_candidates_is_not_swallowed(self):
        self.metadata.side_effect = [
            {"entries": [{"url": YOUTUBE_VIDEO}]}, sources._MetadataLimit("10 MB"),
        ]
        with self.assertRaisesRegex(sources.SourceResolutionError, "10 MB"):
            self.resolve("@creator", platform="youtube", mode="latest")

    def test_bounded_candidates_and_no_accessible_archive(self):
        self.metadata.return_value = {"entries": [{"url": "--bad"}] * 100}
        with self.assertRaisesRegex(sources.SourceResolutionError, "20 most recent"):
            self.resolve("creator", mode="latest")
        self.metadata.assert_called_once()


class ManifestTests(unittest.TestCase):
    def check(self, bodies, url="https://cdn.example/media.m3u8", final_url=None):
        contexts = []
        for _ in bodies:
            context = MagicMock()
            context.__enter__.return_value = (final_url or url, object())
            contexts.append(context)
        with patch("streamframe.jobs.RemoteFetch") as factory:
            fetch = factory.return_value
            fetch.open.side_effect = contexts
            fetch.chunks.side_effect = [[body] for body in bodies]
            fetch.exhausted = False
            result = sources._manifest_finished(
                video(formats=[{"url": url, "protocol": "m3u8_native", "vcodec": "avc1"}]),
                "http://127.0.0.1:1234", threading.Event(), time.monotonic() + 10,
            )
            return result, fetch

    def test_requires_endlist_not_duration_or_playlist_type(self):
        for content, expected in [
            (b"#EXTM3U\n#EXTINF:2,\nsegment.ts\n#EXT-X-ENDLIST\n", True),
            (b"#EXTM3U\n#EXTINF:2,\nsegment.ts\n", False),
            (b"#EXTM3U\n#EXT-X-ENDLIST\n#EXTINF:2,\nsegment.ts\n", False),
            (b"#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:2,\nsegment.ts\n", False),
            (b"<html>not a manifest</html>", False),
        ]:
            with self.subTest(content=content):
                self.assertEqual(self.check([content])[0], expected)

    def test_master_playlist_checks_media_variant(self):
        result, fetch = self.check([
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000\nmedia/index.m3u8\n",
            b"#EXTM3U\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST\n",
        ], final_url="https://cdn.example/redirected/master.m3u8")
        self.assertTrue(result)
        self.assertEqual(fetch.open.call_args.args[0], "https://cdn.example/redirected/media/index.m3u8")

    def test_manifest_size_is_bounded(self):
        self.assertFalse(self.check([b"#EXTM3U\n" + b"x" * sources.MAX_MANIFEST_BYTES])[0])

    def test_real_guard_denies_private_manifest_destinations_and_credentials(self):
        for url in ("https://127.0.0.1/private", "https://[::1]/private",
                    "https://user:secret@127.0.0.1/private"):
            info = video(url=TWITCH_VIDEO, live_status=None, formats=[{
                "url": url, "protocol": "m3u8_native", "vcodec": "avc1",
            }])
            with self.subTest(url=url), patch.object(sources, "_metadata", return_value=info):
                with self.assertRaisesRegex(sources.SourceResolutionError, "verified finished"):
                    sources.resolve_source(TWITCH_VIDEO)

    def test_manifest_cancellation_is_translated(self):
        from streamframe.jobs import Cancelled

        with patch("streamframe.jobs.RemoteFetch") as factory:
            factory.return_value.open.side_effect = Cancelled()
            with self.assertRaises(sources.SourceResolutionCancelled):
                sources._manifest_finished(
                    video(), "http://127.0.0.1:1234", threading.Event(), time.monotonic() + 10,
                )


class MetadataProcessTests(unittest.TestCase):
    def run_metadata(self, script, budget=None, cancelled=None, deadline=None):
        popen = subprocess.Popen
        captured = []

        def fake(command, **kwargs):
            captured.append((command, kwargs))
            return popen([sys.executable, "-I", "-c", script], **kwargs)

        with patch.object(sources.subprocess, "Popen", side_effect=fake):
            data = sources._metadata(
                YOUTUBE_VIDEO, flat=False, proxy="http://127.0.0.1:1234",
                cancelled=cancelled or threading.Event(),
                deadline=deadline or time.monotonic() + 5,
                budget=budget if budget is not None else [sources.MAX_METADATA_BYTES],
            )
        return data, captured

    def test_subprocess_uses_argument_list_and_no_credentials_configuration_or_plugins(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://user:secret@evil.test",
                                     "PYTHONPATH": "/untrusted", "AWS_SECRET_ACCESS_KEY": "secret"}):
            data, calls = self.run_metadata("print('{\"meta\":{\"id\":\"abc\"},\"formats\":[]}')")
        self.assertEqual(data, {"id": "abc", "formats": []})
        command, kwargs = calls[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[-2:], ["--", YOUTUBE_VIDEO])
        self.assertEqual(command[:4], [sys.executable, "-I", "-m", "yt_dlp"])
        for flag in ("--ignore-config", "--no-config-locations", "--no-plugin-dirs",
                     "--no-cookies", "--no-cookies-from-browser",
                     "--no-cache-dir", "--simulate", "--skip-download"):
            self.assertIn(flag, command)
        self.assertNotIn("--netrc", command)
        self.assertNotIn("--no-netrc", command)
        import yt_dlp
        parsed = yt_dlp.parse_options(command[4:])
        self.assertFalse(parsed.options.usenetrc)
        self.assertEqual(parsed.urls, [YOUTUBE_VIDEO])
        self.assertNotIn("HTTP_PROXY", kwargs["env"])
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kwargs["env"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])

    def test_compact_template_excludes_large_and_sensitive_format_fields(self):
        import json
        import yt_dlp

        _, calls = self.run_metadata("print('{\"meta\":{},\"formats\":[]}')")
        command = calls[0][0]
        template = command[command.index("--print") + 1]
        sample = {"id": "abcdefghijk", "title": "Title", "webpage_url": YOUTUBE_VIDEO,
                  "live_status": "was_live", "formats": [{
                      "url": "https://cdn.example/video.m3u8", "vcodec": "avc1",
                      "protocol": "m3u8_native", "fragments": ["x" * 100000],
                  }]}
        output = yt_dlp.YoutubeDL({"quiet": True}).evaluate_outtmpl(template, sample)
        data = json.loads(output)
        self.assertEqual(data["meta"]["id"], "abcdefghijk")
        self.assertNotIn("fragments", data["formats"][0])
        self.assertLess(len(output), 1000)

    def test_stdout_and_stderr_share_bounded_budget(self):
        for script in ("print('x' * 10000)", "import sys; sys.stderr.write('x' * 10000)"):
            with self.subTest(script=script), self.assertRaisesRegex(sources.SourceResolutionError, "10 MB"):
                self.run_metadata(script, budget=[100])

    def test_invalid_json_and_non_object_are_rejected(self):
        for content in ("not json", "[]", "null"):
            script = "print(" + repr(content) + ")"
            with self.subTest(content=content), self.assertRaisesRegex(sources.SourceResolutionError, "invalid"):
                self.run_metadata(script)

    def test_stalled_subprocess_is_terminated_at_deadline(self):
        start = time.monotonic()
        with self.assertRaisesRegex(sources.SourceResolutionError, "30-second"):
            self.run_metadata("import time; time.sleep(20)", deadline=start + 0.15)
        self.assertLess(time.monotonic() - start, 2)

    def test_cancellation_terminates_running_subprocess(self):
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        timer.start()
        try:
            with self.assertRaises(sources.SourceResolutionCancelled):
                self.run_metadata("import time; time.sleep(20)", cancelled=event)
        finally:
            timer.cancel()
            timer.join()

    def test_offline_error_is_distinct_from_network_error(self):
        with self.assertRaises(sources._Offline):
            self.run_metadata("import sys; sys.stderr.write('The channel is offline'); sys.exit(1)")
        with self.assertRaisesRegex(sources.SourceResolutionError, "could not resolve"):
            self.run_metadata("import sys; sys.stderr.write('Network error with secret URL'); sys.exit(1)")

    def test_provider_errors_are_actionable_and_never_expose_remote_text(self):
        cases = [
            ("Sign in to confirm you’re not a bot", "sign-in or bot verification"),
            ("HTTP Error 429: Too Many Requests", "rate-limited"),
            ("HTTP Error 403: Forbidden", "HTTP 403"),
            ("network timed out", "timed out"),
            ("error: no such option: --bad", "required resolver option"),
            ("unrecognized provider failure", "could not resolve"),
        ]
        for error, expected in cases:
            stderr = error + " https://cdn.example/secret?signature=private cookie=secret"
            script = "import sys; sys.stderr.write(" + repr(stderr) + "); sys.exit(1)"
            with self.subTest(error=error), self.assertRaises(sources.SourceResolutionError) as caught:
                self.run_metadata(script)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("cdn.example", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("signature", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
