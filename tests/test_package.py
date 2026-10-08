# SPDX-License-Identifier: MPL-2.0
"""Public package and Studio export contract tests; no display or BLE required."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from hmm_gesture import GestureBundle, GestureRecognizer, PipelineConfig, SegmentationConfig
from hmm_gesture.preprocessing import FeatureExtractor, SignalFilter
from hmm_gesture.segmentation import MotionSegmenter
from hmm_gesture_studio.datasets import GestureDataset, load_dataset, save_dataset
from hmm_gesture_studio.training import train_datasets

ROOT = Path(__file__).resolve().parents[1]


def recordings(name="挥手", axis=0, rate=25):
    rng = np.random.default_rng(axis)
    reps = []
    for _ in range(3):
        rep = rng.integers(-50, 50, size=(48, 6), dtype=np.int16)
        rep[:, axis] += (3000 * np.sin(np.linspace(0, np.pi, 48))).astype(np.int16)
        reps.append(rep)
    return GestureDataset(name, reps, rate)


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datasets = [recordings("向上 / 原名", 0, 50), recordings("向右", 1, 50)]
        cls.config = PipelineConfig(sample_rate_hz=50, cutoff_hz=9, filter_order=3,
                                    median_kernel=3, window_size=6, window_overlap=2)
        cls.segmentation = SegmentationConfig(energy_threshold=600, cooldown_frames=3)
        cls.bundle = train_datasets(cls.datasets, cls.config, 3, cls.segmentation)

    def test_export_load_preserves_predictions_and_all_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.bundle.save(Path(folder) / "nested" / "export.gesture.json")
            with patch("pickle.load", side_effect=AssertionError("must not deserialize pickle")):
                loaded = GestureRecognizer.load(path)
            self.assertEqual(loaded.bundle.pipeline, self.config)
            self.assertEqual(loaded.bundle.segmentation, self.segmentation)
            self.assertEqual(loaded.gesture_names, ("向上 / 原名", "向右"))
            self.assertEqual(loaded.sample_rate_hz, 50)
            before = GestureRecognizer(self.bundle)
            for dataset in self.datasets:
                for rep in dataset.repetitions:
                    a, b = before.predict(rep), loaded.predict(rep, sample_rate_hz=50)
                    self.assertEqual(a.name, b.name)
                    self.assertEqual(b.name, dataset.name)
                    self.assertAlmostEqual(a.confidence, b.confidence, places=12)
                    self.assertAlmostEqual(a.score, b.score, places=10)
                    self.assertEqual(a.scores, b.scores)
            self.assertIn("training", loaded.bundle.metadata)

    def mutate_bundle(self, mutate):
        with tempfile.TemporaryDirectory() as folder:
            path = self.bundle.save(Path(folder) / "export.json")
            raw = json.loads(path.read_text(encoding="utf-8"))
            mutate(raw)
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                GestureBundle.load(path)

    def test_reject_invalid_exports(self):
        mutations = [
            lambda r: r.update(version=2),
            lambda r: r.update(version=True),
            lambda r: r.update(format="pickle"),
            lambda r: r.update(axes=["gx", "gy", "gz", "ax", "ay", "az"]),
            lambda r: r.update(units="g"),
            lambda r: r.update(gestures=[]),
            lambda r: r["gestures"].append(r["gestures"][0]),
            lambda r: r["pipeline"].pop("cutoff_hz"),
            lambda r: r["pipeline"].update(window_overlap=6),
            lambda r: r["segmentation"].update(energy_threshold=-1),
            lambda r: r["gestures"][0].update(name=""),
            lambda r: r["gestures"][0]["model"].update(covariance_type="full"),
            lambda r: r["gestures"][0]["model"].update(means=[[0.0]]),
            lambda r: r["gestures"][0]["model"].update(startprob=[-1, 1, 1]),
            lambda r: r["gestures"][0]["model"].update(transmat=[[1, 0, 0]] * 2),
            lambda r: r["gestures"][0]["model"]["covars"][0].__setitem__(0, 0),
            lambda r: r["gestures"][0]["model"]["means"][0].__setitem__(0, float("nan")),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.mutate_bundle(mutate)

    def test_export_is_atomic_on_write_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.bundle.save(Path(folder) / "export.json")
            original = path.read_bytes()
            with patch("hmm_gesture.bundle.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    self.bundle.save(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_runtime_requires_neither_studio_nor_bluetooth_nor_tk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.bundle.save(Path(folder) / "model.json")
            script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'openzilo','bleak','tkinter','hmm_gesture_studio'}:
            raise AssertionError('runtime imported '+fullname)
sys.meta_path.insert(0, Block())
from hmm_gesture import GestureRecognizer
recognizer = GestureRecognizer.load(sys.argv[1])
assert recognizer.predict([[1,2,3,4,5,6]]*24) is not None
print(recognizer.gesture_names)
"""
            result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=folder,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_prediction_validation_and_rejection(self):
        recognizer = GestureRecognizer(self.bundle)
        self.assertIsNone(recognizer.predict([]))
        self.assertIsNone(recognizer.predict(np.zeros((9, 6))))
        with self.assertRaisesRegex(ValueError, "Expected 50"):
            recognizer.predict(np.zeros((24, 6)), sample_rate_hz=25)
        with self.assertRaises(ValueError):
            recognizer.feed([], sample_rate_hz=25)
        for data in ([[1, 2, 3]], np.zeros((6,)), [[float("inf")] * 6], [[40000] * 6]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                recognizer.predict(data)
        for confidence in (-0.1, 1.1, float("nan")):
            with self.assertRaises(ValueError):
                GestureRecognizer(self.bundle, confidence)
        single = GestureBundle({"one": next(iter(self.bundle.models.values()))}, self.config)
        result = GestureRecognizer(single).predict(self.datasets[0].repetitions[0])
        self.assertEqual(result.confidence, 0.8)
        self.assertIsNone(GestureRecognizer(single, min_confidence=0.9).predict(self.datasets[0].repetitions[0]))


class SegmentationTests(unittest.TestCase):
    def setUp(self):
        idle = np.zeros((30, 6))
        moving = np.tile([5000, 0, 0, 0, 0, 0], (20, 1))
        self.stream = np.concatenate([idle, moving, idle, moving, idle])

    def test_batch_partition_does_not_drop_gestures(self):
        expected = MotionSegmenter().feed_all(self.stream)
        self.assertEqual(len(expected), 2)
        for size in (1, 5, 32, 1000):
            segmenter = MotionSegmenter()
            actual = []
            for offset in range(0, len(self.stream), size):
                actual.extend(segmenter.feed_all(self.stream[offset:offset + size]))
            self.assertEqual(len(actual), 2)
            for a, b in zip(expected, actual):
                np.testing.assert_array_equal(a, b)

    def test_public_feed_returns_every_completed_prediction(self):
        bundle = train_datasets([recordings()], PipelineConfig())
        recognizer = GestureRecognizer(bundle)
        self.assertEqual(len(recognizer.feed(self.stream)), 2)
        self.assertEqual(recognizer.feed([]), [])
        recognizer.reset()
        self.assertEqual(len(recognizer.feed(self.stream)), 2)

    def test_reset_relearns_baseline(self):
        segmenter = MotionSegmenter()
        segmenter.feed_all([[5000] * 6] * 5)
        self.assertTrue(segmenter._baseline_initialized)
        segmenter.reset()
        self.assertFalse(segmenter._baseline_initialized)
        self.assertEqual(len(segmenter.feed_all(self.stream)), 2)

    def test_long_motion_is_discarded_and_short_is_ignored(self):
        segmenter = MotionSegmenter(max_gesture_len=40)
        self.assertEqual(segmenter.feed_all(np.concatenate([
            np.zeros((10, 6)), np.ones((100, 6)) * 5000,
        ])), [])
        self.assertEqual(MotionSegmenter().feed_all([[0] * 6] * 10 + [[5000] * 6] * 3 + [[0] * 6] * 10), [])


class DatasetTrainingTests(unittest.TestCase):
    def test_round_trip_and_collision_protection(self):
        dataset = recordings("a/b")
        with tempfile.TemporaryDirectory() as folder:
            path = save_dataset(dataset, folder)
            self.assertEqual(path.parent, Path(folder))
            loaded = load_dataset(path)
            self.assertEqual(loaded.name, dataset.name)
            np.testing.assert_array_equal(loaded.repetitions, dataset.repetitions)
            with self.assertRaisesRegex(ValueError, "collision"):
                save_dataset(recordings("a\\b"), folder)
            self.assertEqual(load_dataset(path).name, "a/b")

    def test_raw_validation_does_not_wrap_int16(self):
        for rep in (np.full((12, 6), 32768), np.full((12, 6), 0.5), np.zeros((12, 5)),
                    np.full((12, 6), np.nan), np.empty((0, 6))):
            with self.subTest(rep=rep), self.assertRaises(ValueError):
                GestureDataset("test", [rep])
        for rate in (0, -1, float("inf"), True):
            with self.assertRaises(ValueError):
                GestureDataset("test", [np.ones((12, 6))], rate)

    def test_preflight_training_validation(self):
        good = recordings()
        short = GestureDataset("短", [np.ones((11, 6))] * 2)
        for datasets in ([], [short], [good, good], [good, recordings("rate", rate=50)]):
            with self.subTest(names=[d.name for d in datasets]):
                with patch("hmm_gesture_studio.training._fit_gesture") as fit:
                    with self.assertRaises(ValueError):
                        train_datasets(datasets, PipelineConfig())
                    fit.assert_not_called()

    def test_short_repetitions_are_reported_not_counted(self):
        dataset = recordings()
        dataset.repetitions.append(np.ones((8, 6), dtype=np.int16))
        progress = []
        bundle = train_datasets([dataset], PipelineConfig(), progress=progress.append)
        self.assertEqual(bundle.metadata["training"][dataset.name]["used_recordings"], 3)
        self.assertTrue(any("3/4" in text for text in progress))

    def test_new_cli_and_all_shipped_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.gesture.json"
            result = subprocess.run([sys.executable, "-m", "hmm_gesture_studio.training",
                                     "--data", str(ROOT / "sample_data"), "--output", str(path)],
                                    cwd=folder, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            recognizer = GestureRecognizer.load(path)
            self.assertEqual(len(recognizer.gesture_names), 6)
            for source in (ROOT / "sample_data").glob("*.json"):
                for rep in load_dataset(source).repetitions:
                    prediction = recognizer.predict(rep)
                    self.assertIsNotNone(prediction)
                    self.assertTrue(np.isfinite(prediction.score))

    def test_config_validation(self):
        for kwargs in ({"sample_rate_hz": 0}, {"cutoff_hz": 12.5}, {"window_overlap": 8},
                       {"window_size": 0}, {"median_kernel": 4}, {"filter_order": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PipelineConfig(**kwargs)
        with self.assertRaises(ValueError):
            SignalFilter(sample_rate=10, cutoff_hz=10)
        with self.assertRaises(ValueError):
            FeatureExtractor(window_size=8, overlap=8)
        for kwargs in ({"min_onset_frames": 0}, {"cooldown_frames": -1},
                       {"min_gesture_len": 130}, {"energy_threshold": float("nan")}):
            with self.assertRaises(ValueError):
                SegmentationConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
