# SPDX-License-Identifier: MPL-2.0
"""Data compatibility and CLI smoke tests using the shipped, trusted examples."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import openzilo as sdk

from feature_extractor import FeatureExtractor
from recognize import HMMRecognizer, run_offline
from record_gesture import load_csv, save_gesture
from signal_filter import SignalFilter
from train_hmm import load_gesture_data


ROOT = Path(__file__).resolve().parents[1]


class TestData(unittest.TestCase):
    def test_save_and_load_non_default_rate(self):
        reps = [np.ones((16, 6), dtype=np.int16) for _ in range(2)]
        with tempfile.TemporaryDirectory() as directory:
            path = save_gesture("test", reps, Path(directory), sample_rate_hz=50)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["sample_rate_hz"], 50)
            self.assertEqual(data["num_repetitions"], 2)
            name, loaded = load_gesture_data(path, expected_sample_rate=50)
            self.assertEqual(name, "test")
            np.testing.assert_array_equal(loaded[0], reps[0])
            with self.assertRaisesRegex(ValueError, "does not match"):
                load_gesture_data(path, expected_sample_rate=25)
            for rate in (0, -1, float("nan"), float("inf")):
                with self.subTest(rate=rate), self.assertRaises(ValueError):
                    save_gesture("invalid", reps, Path(directory), sample_rate_hz=rate)

    def test_default_rate_and_legacy_json_compatibility(self):
        for path in (ROOT / "sample_data").glob("*.json"):
            with self.subTest(path=path.name):
                _, reps = load_gesture_data(path, expected_sample_rate=25)
                self.assertEqual(len(reps), 5)
                self.assertTrue(all(rep.shape[1] == 6 for rep in reps))
                self.assertTrue(all(rep.dtype == np.int16 for rep in reps))

    def test_headerless_csv_axis_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text("1,2,3,4,5,6\n7,8,9,10,11,12\n", encoding="utf-8")
            np.testing.assert_array_equal(load_csv(path), [[1, 2, 3, 4, 5, 6],
                                                         [7, 8, 9, 10, 11, 12]])
            path.write_text("1,2,3\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_csv(path)

    def test_feature_window_minimum(self):
        extractor = FeatureExtractor()
        self.assertEqual(extractor.extract(np.zeros((11, 6))).shape, (1, 24))
        self.assertEqual(extractor.extract(np.zeros((12, 6))).shape, (2, 24))

    def test_offline_sample_rate_mismatch(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "does not match"):
                run_offline(ROOT / "pretrained_models", ROOT / "sample_data/向上.json",
                            50, 10, 8, 4)


class TestPipeline(unittest.TestCase):
    def run_cli(self, *args):
        result = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=60,
                                env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_cli_help_and_official_sdk(self):
        required_exports = {
            "OpenZiloClient", "OpenZiloError", "TimeoutError", "SensorDataSample",
            "SensorDataBatch", "get_system_info", "start_sensor_report",
            "wait_sensor_data", "stop_sensor_report",
        }
        self.assertTrue(required_exports.issubset(sdk.__all__))
        for script in ("record_gesture.py", "train_hmm.py", "recognize.py"):
            with self.subTest(script=script):
                self.run_cli(script, "--help")
        self.run_cli("-m", "openzilo", "--help")

    def test_pretrained_models_classify_all_example_repetitions(self):
        with redirect_stdout(io.StringIO()):
            recognizer = HMMRecognizer(ROOT / "pretrained_models", SignalFilter(),
                                       FeatureExtractor())
        self.assertEqual(len(recognizer._models), 6)
        count = 0
        for path in (ROOT / "sample_data").glob("*.json"):
            _, reps = load_gesture_data(path)
            for index, rep in enumerate(reps):
                with self.subTest(file=path.name, repetition=index):
                    result = recognizer._classify_segment(rep)
                    self.assertIsNotNone(result)
                    self.assertIn(result[0], recognizer._models)
                    self.assertTrue(np.isfinite(result[1]))
                    self.assertGreaterEqual(result[1], 0)
                    self.assertLessEqual(result[1], 1)
                    count += 1
        self.assertEqual(count, 30)

    def test_training_and_offline_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory) / "models"
            self.run_cli("train_hmm.py", "--data", "sample_data", "--output", str(models))
            self.assertEqual(len(list(models.glob("*.pkl"))), 6)
            result = self.run_cli("recognize.py", "--models", str(models),
                                  "--input", "sample_data/向上.json")
            self.assertIn("向上", result.stdout)

    def test_csv_import_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("first.csv", "second.csv"):
                np.savetxt(root / name, np.ones((16, 6), dtype=np.int16),
                           delimiter=",", fmt="%d")
            self.run_cli("record_gesture.py", "--name", "test", "--from-csv",
                         str(root / "first.csv"), str(root / "second.csv"),
                         "--sample-rate", "50", "--output", str(root / "gestures"))
            saved = json.loads((root / "gestures/test.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["sample_rate_hz"], 50)
            self.assertEqual(saved["num_repetitions"], 2)


if __name__ == "__main__":
    unittest.main()
