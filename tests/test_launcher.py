import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "streamframe"


@unittest.skipUnless((ROOT / ".venv" / "bin" / "python").is_file(),
                     "project-local virtual environment is not installed")
class LauncherTests(unittest.TestCase):
    def test_help_runs_from_another_directory_without_path_setup(self):
        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"
        env.pop("STREAMFRAME_FFMPEG_PATH", None)
        env.pop("STREAMFRAME_FFMPEG", None)
        with tempfile.TemporaryDirectory() as workdir:
            result = subprocess.run([str(LAUNCHER), "--help"], cwd=workdir,
                                    env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("submit", result.stdout)
        self.assertIn("serve", result.stdout)


if __name__ == "__main__":
    unittest.main()
