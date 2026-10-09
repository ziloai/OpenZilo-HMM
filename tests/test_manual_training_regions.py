# SPDX-License-Identifier: MPL-2.0
"""Non-destructive manual training annotations, independent of automatic triggers."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from hmm_gesture import GestureBundle, GestureRecognizer, PipelineConfig, SegmentationConfig
from hmm_gesture.segmentation import prepare_recording
from hmm_gesture_studio import evaluation, training
from hmm_gesture_studio.datasets import GestureDataset, load_dataset, save_dataset


def weak_takes(count=4):
    reps, regions = [], []
    for i in range(count):
        raw = np.tile(np.array([200, -100, 1600, i * 10, 0, 0], dtype=np.int16), (150, 1))
        start = 25 + 10 * i
        t = np.linspace(0, 2 * np.pi, 41)
        raw[start:start + 41, 4] = np.rint((6000 + 100 * i) * np.sin(t)).astype(np.int16)
        raw[start:start + 41, 0] += np.rint(500 * np.sin(t)).astype(np.int16)
        reps.append(raw)
        regions.append((start, start + 41))
    return GestureDataset("up", reps, 100, regions)


class ManualTrainingRegionTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = PipelineConfig(sample_rate_hz=100, cutoff_hz=30, median_kernel=1,
                                       filter_initialization="steady")
        self.segmentation = SegmentationConfig.for_sample_rate(100, mode="impulse", energy_threshold=8000,
                                                               min_samples=self.pipeline.min_samples)

    def test_roundtrip_preserves_full_raw_takes_and_optional_per_take_regions(self):
        dataset = weak_takes()
        dataset.training_regions[1] = None
        originals = [rep.copy() for rep in dataset.repetitions]
        with tempfile.TemporaryDirectory() as folder:
            path = save_dataset(dataset, folder)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["repetitions"][0]["training_region"], [25, 66])
            self.assertNotIn("training_region", payload["repetitions"][1])
            restored = load_dataset(path)
            self.assertEqual(restored.training_regions, dataset.training_regions)
            name, legacy_reps = training.load_gesture_data(path, expected_sample_rate=100)
            self.assertEqual(name, dataset.name)
            for actual, rep, region in zip(legacy_reps, originals, dataset.training_regions):
                expected = rep[region[0]:region[1]] if region is not None else rep
                np.testing.assert_array_equal(actual, expected)
            for actual, original in zip(restored.repetitions, originals):
                np.testing.assert_array_equal(actual, original)
            restored.training_regions = None
            save_dataset(restored, folder)
            self.assertIsNone(load_dataset(path).training_regions)
            self.assertTrue(all("training_region" not in rep
                                for rep in json.loads(path.read_text())["repetitions"]))

    def test_invalid_regions_and_misalignment_are_rejected_without_overwriting(self):
        dataset = weak_takes(1)
        with tempfile.TemporaryDirectory() as folder:
            path = save_dataset(dataset, folder)
            original = path.read_bytes()
            for region in ((0, 0), (-1, 20), (30, 10), (0, 151), (False, 20),
                           (0, 20.0), (0,), "0,20", {"start": 0, "end": 20}):
                with self.subTest(region=region):
                    dataset.training_regions = [region]
                    with self.assertRaisesRegex(ValueError, "training_region"):
                        save_dataset(dataset, folder)
                    self.assertEqual(path.read_bytes(), original)
                    payload = json.loads(original)
                    payload["repetitions"][0]["training_region"] = region
                    bad = Path(folder) / "bad.json"
                    bad.write_text(json.dumps(payload))
                    with self.assertRaisesRegex(ValueError, "invalid gesture dataset"):
                        load_dataset(bad)
            for regions in ([], [None, None], "bad"):
                with self.subTest(regions=regions), self.assertRaisesRegex(ValueError, "one entry"):
                    GestureDataset("bad", dataset.repetitions, 100, regions)

    def test_manual_weak_takes_train_and_calibrate_exact_crops_without_auto_retriggering(self):
        dataset = weak_takes(3)
        originals = [rep.copy() for rep in dataset.repetitions]
        self.assertTrue(all(prepare_recording(rep, self.segmentation) is None for rep in originals))
        crops = [rep[start:end] for rep, (start, end) in zip(originals, dataset.training_regions)]
        messages = []
        with patch.object(training, "_fit_gesture", wraps=training._fit_gesture) as fit, \
                patch.object(training, "prepare_recording", wraps=prepare_recording) as prepare:
            bundle = training.train_datasets([dataset], self.pipeline, segmentation=self.segmentation,
                                             calibrate_rejection=True, progress=messages.append)
        prepare.assert_not_called()
        self.assertEqual(fit.call_count, 4)
        expected = [[rep for i, rep in enumerate(crops) if i != held] for held in range(3)] + [crops]
        for call, subset in zip(fit.call_args_list, expected):
            for actual, original in zip(call.args[1], subset):
                np.testing.assert_array_equal(actual, original)
        for rep, original in zip(dataset.repetitions, originals):
            np.testing.assert_array_equal(rep, original)
        self.assertEqual(bundle.metadata["training"]["up"]["manual_training_regions"], 3)
        self.assertIn("实时识别仍", "\n".join(messages))
        # An annotation does not silently lower runtime thresholds or pretend
        # the detector can find this weak acceleration/strong gyro movement.
        self.assertEqual(bundle.segmentation, self.segmentation)
        self.assertIsNone(GestureRecognizer(bundle).predict(originals[0]))
        with tempfile.TemporaryDirectory() as folder:
            path = bundle.save(Path(folder) / "manual.gesture.json")
            loaded = GestureBundle.load(path)
            self.assertEqual(loaded.segmentation, self.segmentation)
            self.assertEqual(loaded.rejection, bundle.rejection)

    def test_mixed_auto_and_manual_crops_preserve_indices_in_both_modes(self):
        dataset = weak_takes(2)
        dataset.repetitions[1][70, 0] = 20000
        dataset.training_regions[1] = None
        expected = prepare_recording(dataset.repetitions[1], self.segmentation)
        prepared = training._prepare_repetitions(dataset, self.pipeline, self.segmentation)
        self.assertEqual([index for index, _ in prepared], [0, 1])
        np.testing.assert_array_equal(prepared[0][1], dataset.repetitions[0][25:66])
        np.testing.assert_array_equal(prepared[1][1], expected)
        motion = training._prepare_repetitions(dataset, self.pipeline, SegmentationConfig())
        np.testing.assert_array_equal(motion[0][1], dataset.repetitions[0][25:66])
        np.testing.assert_array_equal(motion[1][1], dataset.repetitions[1])

    def test_short_manual_crop_fails_before_training_or_evaluation_in_either_mode(self):
        dataset = weak_takes()
        dataset.training_regions[1] = (20, 25)
        for config in (self.segmentation, SegmentationConfig()):
            for run in (training.train_datasets, evaluation.evaluate_leave_one_out):
                with self.subTest(mode=config.mode, run=run.__name__), \
                        patch.object(training, "_fit_gesture") as fit, \
                        self.assertRaisesRegex(ValueError, "第 2 次手动训练框过短.*12"):
                    run([dataset], self.pipeline, segmentation=config, calibrate_rejection=True)
                fit.assert_not_called()

    def test_evaluation_keeps_fold_annotations_but_tests_automatic_detection_on_whole_takes(self):
        dataset = weak_takes()
        original_train = training.train_datasets
        original_predict = GestureRecognizer.predict
        folds, tested, messages = [], [], []

        def train(split, pipeline, n_states, **kwargs):
            fold = len(folds)
            expected = [i for i in range(4) if i != fold]
            self.assertEqual(split[0].training_regions, [dataset.training_regions[i] for i in expected])
            for rep, i in zip(split[0].repetitions, expected):
                np.testing.assert_array_equal(rep, dataset.repetitions[i])
            folds.append(expected)
            return original_train(split, pipeline, n_states, **kwargs)

        def predict(recognizer, rep, **kwargs):
            index = len(tested)
            np.testing.assert_array_equal(rep, dataset.repetitions[index])
            tested.append(index)
            return original_predict(recognizer, rep, **kwargs)

        with patch.object(evaluation, "train_datasets", side_effect=train), \
                patch.object(GestureRecognizer, "predict", predict):
            result = evaluation.evaluate_leave_one_out([dataset], self.pipeline, segmentation=self.segmentation,
                                                       calibrate_rejection=True, progress=messages.append)
        self.assertEqual(len(folds), 4)
        self.assertEqual(tested, list(range(4)))
        self.assertEqual((result["correct"], result["total"]), (0, 4))
        self.assertEqual([row["recording_index"] for row in result["predictions"]], list(range(4)))
        self.assertIn("未触发也计为未识别", "\n".join(messages))


if __name__ == "__main__":
    unittest.main()
