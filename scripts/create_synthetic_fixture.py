"""Generate an original test video with a synthetic face panel and spoken audio."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    text = (
        "Welcome to the stream. Watch this move carefully. "
        "That was a close call, but we made it. Let's try one more time."
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        speech = Path(directory) / "speech.aiff"
        if shutil.which("say"):
            subprocess.run(["say", "-o", str(speech), text], check=True)
        elif shutil.which("espeak"):
            speech = Path(directory) / "speech.wav"
            subprocess.run(["espeak", "-w", str(speech), text], check=True)
        else:
            parser.error("install 'espeak' or use macOS 'say' to synthesize speech")
        # A distinct synthetic facecam occupies the top-left quarter; the rest
        # is animated generated video, not footage from a game or a creator.
        video = (
            "testsrc2=size=1280x720:rate=30,"
            "drawbox=x=0:y=0:w=320:h=240:color=0x203040:t=fill,"
            "drawbox=x=100:y=20:w=120:h=170:color=0xe7b68e:t=fill,"
            "drawbox=x=120:y=70:w=20:h=15:color=black:t=fill,"
            "drawbox=x=180:y=70:w=20:h=15:color=black:t=fill,"
            "drawbox=x=135:y=140:w=50:h=8:color=black:t=fill"
        )
        subprocess.run(
            [
                args.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", video, "-i", str(speech),
                "-t", "12", "-vf", "format=yuv420p",
                "-af", "apad", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-movflags", "+faststart", str(args.output),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
