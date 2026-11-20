"""Loopback E2E backend with deterministic or actual analysis and real rendering.

Set STREAMFRAME_BROWSER_DIST to an isolated build directory to test new assets
without changing frontend/dist or restarting an existing StreamFrame server.
"""

import os
from pathlib import Path

import uvicorn
from starlette.staticfiles import StaticFiles

from streamframe.server import create_app


def analyze(path, progress, cancelled):
    root = Path(os.environ["STREAMFRAME_BROWSER_WORKSPACE"]).resolve()
    if (root / "hold-analysis").exists():
        progress(0.1, "Waiting for browser cancellation check")
        cancelled.wait(30)
        progress(0.2, "Cancellation checked")
    if cancelled.is_set():
        raise RuntimeError("Cancelled")
    if os.environ.get("STREAMFRAME_BROWSER_REAL_ANALYSIS") == "1":
        from streamframe.analysis import analyze_media
        progress(0.1, "Real local Whisper / visual analysis")
        result = analyze_media(
            path,
            whisper_model=os.environ.get("STREAMFRAME_WHISPER_MODEL", "small"),
            cache_dir=root / "cache",
            cache_transcription=False,
            yunet_model_path=Path(__file__).resolve().parents[2] / "models" / "face_detection_yunet_2023mar.onnx",
            cancellation_callback=cancelled.is_set,
        )
        words = [
            {key: word[key] for key in ("start", "end", "text")}
            for word in result["transcript"]["words"]
        ]
        progress(1, "Real local analysis complete")
        return {
            "candidates": [
                {
                    "id": f"real-{index}",
                    "start": item["start"], "end": item["end"], "score": item["score"],
                    "evidence": [f"{key}: {value}" for key, value in item["evidence"].items()],
                    "words": [word for word in words if word["start"] < item["end"] and word["end"] > item["start"]],
                    "face_crop": None, "content_crop": None,
                }
                for index, item in enumerate(result["candidates"])
            ],
            "source_words": words,
        }
    progress(1, "Deterministic browser-test analysis complete")
    return [
        {
            "id": "browser-first",
            "start": 0.25,
            "end": 1.75,
            "score": 7.5,
            "evidence": ["Synthetic fixture; analysis is deterministic, not a quality claim."],
            "words": [{"start": 0.4, "end": 0.9, "text": "Original"}],
            "face_crop": None,
            "content_crop": None,
        },
        {
            "id": "browser-second",
            "start": 0.1,
            "end": 1.5,
            "score": 3,
            "evidence": ["Second fixture candidate for export ownership checks."],
            "words": [],
            "face_crop": None,
            "content_crop": None,
        },
    ]


if __name__ == "__main__":
    root = Path(os.environ["STREAMFRAME_BROWSER_WORKSPACE"]).resolve()
    app = create_app(
        data_dir=root / "jobs",
        import_roots=[root / "inputs"],
        analyzer=analyze,
    )
    if os.environ.get("STREAMFRAME_BROWSER_DIST"):
        app.router.routes[:] = [
            route for route in app.router.routes if getattr(route, "name", "") != "frontend"
        ]
        app.mount("/", StaticFiles(directory=os.environ["STREAMFRAME_BROWSER_DIST"], html=True),
                  name="frontend")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["STREAMFRAME_BROWSER_PORT"]),
                proxy_headers=False, log_level="warning")
