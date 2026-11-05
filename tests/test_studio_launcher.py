# SPDX-License-Identifier: MPL-2.0
"""UI selection: no display, Tk or Qt window is required."""
from pathlib import Path
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None
import unittest
from unittest.mock import patch

from hmm_gesture_studio import launcher


class LauncherTests(unittest.TestCase):
    def test_defaults_to_new_ui(self):
        self.assertEqual(launcher.parse_args([]).ui, "new")

    def test_rejects_unknown_ui(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            launcher.parse_args(["--ui", "other"])

    def test_legacy_flag_runs_tk_entry_point(self):
        with patch("hmm_gesture_studio.gui.main") as legacy:
            launcher.main(["--ui", "legacy"])
        legacy.assert_called_once_with()

    def test_missing_qt_library_explains_fallback(self):
        with patch.dict(sys.modules, {"hmm_gesture_studio.qt.app": None}):
            with self.assertRaises(SystemExit) as caught:
                launcher.main([])
        self.assertIn("--ui legacy", str(caught.exception))

    @unittest.skipIf(tomllib is None, "tomllib requires Python 3.11")
    def test_console_scripts_expose_both_interfaces(self):
        root = Path(__file__).resolve().parents[1]
        scripts = tomllib.loads((root / "studio" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
        self.assertEqual(scripts["hmm-gesture-studio"], "hmm_gesture_studio.launcher:main")
        self.assertEqual(scripts["hmm-gesture-studio-legacy"], "hmm_gesture_studio.gui:main")

    def test_selecting_legacy_never_imports_qt(self):
        code = ("import sys\nfrom unittest.mock import patch\nfrom hmm_gesture_studio import launcher\n"
                "with patch('hmm_gesture_studio.gui.main'):\n    launcher.main(['--ui', 'legacy'])\n"
                "assert 'PySide6' not in sys.modules and 'tkinter' not in sys.modules")
        subprocess.run([sys.executable, "-c", code], check=True)


if __name__ == "__main__":
    unittest.main()
