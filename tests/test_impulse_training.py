# SPDX-License-Identifier: MPL-2.0
"""Shared impulse crops, positive-only calibration and nested recording holdout.

Fixtures are synthetic controls, not a claim of real-world/unknown accuracy.
Personal recordings are deliberately not required by this test suite.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from hmm_gesture import GestureBundle, GestureRecognizer, PipelineConfig, SegmentationConfig
from hmm_gesture.preprocessing import make_pipeline
from hmm_gesture.segmentation import impulse_peak, prepare_recording
from hmm_gesture_studio import evaluation, training
from hmm_gesture_studio.datasets import GestureDataset, save_dataset


def impulse_take(marker=10, *, amplitude=18000, onset=70, length=180):
    """A short raw acceleration transient surrounded by a nonzero idle baseline."""
    raw = np.tile(np.array([240, -120, 1600, marker, 80, -30], dtype=np.int16), (length, 1))
    shape = np.array([1.0, -0.6, 0.35, -0.2, 0.1, -0.05])
    axes = np.array([1.0, 0.3, -0.2, 0.12, 0.05, -0.04])
    available = min(len(shape), length - onset)
    raw[onset:onset + available] += np.rint(amplitude * shape[:available, None] * axes).astype(np.int16)
    return raw


def impulse_dataset(count=3):
    return GestureDataset("snap", [impulse_take(10 * (i + 1), amplitude=18000 + 1000 * i,
                                               onset=50 + 25 * i, length=180 + 10 * i)
                                   for i in range(count)], 100)


class ImpulseTrainingTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = PipelineConfig(sample_rate_hz=100, cutoff_hz=30, median_kernel=1,
                                       window_size=8, window_overlap=4, filter_initialization="steady")
        self.segmentation = SegmentationConfig.for_sample_rate(100, mode="impulse", energy_threshold=8000,
                                                               min_samples=self.pipeline.min_samples)

    def test_training_fits_only_exact_runtime_windows_without_changing_takes(self):
        for calibrate in (False, True):
            with self.subTest(calibrate=calibrate):
                dataset = impulse_dataset()
                original = [rep.copy() for rep in dataset.repetitions]
                expected = [rep[50 + 25 * i - 12:50 + 25 * i + 29] for i, rep in enumerate(original)]
                messages = []
                with patch.object(training, "_fit_gesture", wraps=training._fit_gesture) as fit, \
                        patch.object(training, "prepare_recording", wraps=prepare_recording) as prepare:
                    bundle = training.train_datasets([dataset], self.pipeline, segmentation=self.segmentation,
                        progress=messages.append, calibrate_rejection=calibrate)
                self.assertEqual(prepare.call_count, 3)
                subsets = [[rep for i, rep in enumerate(expected) if i != held] for held in range(3)] if calibrate else []
                subsets.append(expected)
                self.assertEqual(fit.call_count, len(subsets))
                for call, subset in zip(fit.call_args_list, subsets):
                    actual = call.args[1]
                    self.assertEqual(len(actual), len(subset))
                    for crop, wanted in zip(actual, subset):
                        self.assertEqual(crop.shape, (41, 6))
                        np.testing.assert_array_equal(crop, wanted)
                for rep, saved in zip(dataset.repetitions, original):
                    np.testing.assert_array_equal(rep, saved)
                summary = bundle.metadata["training"][dataset.name]
                self.assertEqual((summary["recordings"], summary["used_recordings"]), (3, 3))
                if calibrate:
                    self.assertEqual((bundle.rejection["snap"].min_samples, bundle.rejection["snap"].max_samples), (41, 41))
                    self.assertAlmostEqual(bundle.rejection["snap"].peak_floor,
                                           0.25 * min(impulse_peak(rep) for rep in expected))
                    self.assertTrue(summary["rejection_calibration"]["positive_only"])
                    self.assertIn("非校准概率", "\n".join(messages))
                else:
                    self.assertIsNone(bundle.rejection)
                    self.assertEqual(set(summary), {"recordings", "used_recordings", "states", "training_score_per_frame"})

    def test_malformed_impulse_takes_fail_before_any_fit_in_training_or_evaluation(self):
        good = impulse_take()
        multiple = np.concatenate([good, good])
        cases = {"no event": np.tile(good[0], (180, 1)), "multiple": multiple,
                 "missing pre": impulse_take(onset=3),
                 "missing post": impulse_take(length=70 + self.segmentation.post_roll),
                 "complete then incomplete": multiple[:-100]}
        for label, bad in cases.items():
            with self.subTest(take=label):
                self.assertIsNone(prepare_recording(bad, self.segmentation))
                dataset = GestureDataset("snap", [good, bad, good, good], 100)
                message = r"snap: 第 2 次.*恰好一个动作.*完整前后文"
                with patch.object(training, "_fit_gesture") as fit, self.assertRaisesRegex(ValueError, message):
                    training.train_datasets([dataset], self.pipeline, segmentation=self.segmentation,
                                            calibrate_rejection=True)
                fit.assert_not_called()
                with patch.object(evaluation, "train_datasets") as fit, self.assertRaisesRegex(ValueError, message):
                    evaluation.evaluate_leave_one_out([dataset], self.pipeline, segmentation=self.segmentation,
                                                      calibrate_rejection=True)
                fit.assert_not_called()

    def test_training_needs_three_and_outer_evaluation_needs_four_for_calibration(self):
        for run, count, minimum, target, method in (
                (training.train_datasets, 2, 3, training, "_fit_gesture"),
                (evaluation.evaluate_leave_one_out, 3, 4, evaluation, "train_datasets")):
            with self.subTest(minimum=minimum), patch.object(target, method) as fit:
                with self.assertRaisesRegex(ValueError, f"snap: .*至少 {minimum} 次有效录制"):
                    run([impulse_dataset(count)], self.pipeline, segmentation=self.segmentation,
                        calibrate_rejection=True)
                fit.assert_not_called()

    def test_impulse_context_cannot_be_shorter_than_feature_minimum(self):
        segmentation = SegmentationConfig(mode="impulse", energy_threshold=8000,
                                          pre_roll=2, post_roll=3, min_gesture_len=6)
        with patch.object(training, "_fit_gesture") as fit, self.assertRaisesRegex(ValueError, "裁剪后至少 12"):
            training.train_datasets([impulse_dataset()], self.pipeline, segmentation=segmentation,
                                    calibrate_rejection=True)
        fit.assert_not_called()

    def test_motion_calibration_uses_only_usable_lengths_and_no_peak_limit(self):
        pipeline = PipelineConfig()
        reps = [np.full((length, 6), marker, dtype=np.int16)
                for length, marker in ((12, 1), (25, 2), (42, 3), (8, 30000))]
        dataset = GestureDataset("motion", reps)
        with patch.object(training, "_fit_gesture", wraps=training._fit_gesture) as fit:
            bundle = training.train_datasets([dataset], pipeline, n_states=1, calibrate_rejection=True)
        self.assertEqual(fit.call_count, 4)
        self.assertTrue(all(len(rep) >= pipeline.min_samples for call in fit.call_args_list for rep in call.args[1]))
        limits = bundle.rejection["motion"]
        self.assertEqual((limits.min_samples, limits.max_samples, limits.peak_floor), (12, 84, 0))
        summary = bundle.metadata["training"]["motion"]
        self.assertEqual((summary["recordings"], summary["used_recordings"]), (4, 3))
        self.assertEqual(len(summary["rejection_calibration"]["held_out_scores_per_frame"]), 3)
        with self.assertRaisesRegex(ValueError, "至少 3 次有效录制"):
            training.train_datasets([GestureDataset("motion", [reps[0], reps[1], reps[-1]])], pipeline,
                                    calibrate_rejection=True)

    def test_calibration_uses_real_heldout_scores_per_feature_frame_and_independent_scalers(self):
        pipeline = PipelineConfig()
        dataset = GestureDataset("marked", [np.full((length, 6), marker, dtype=np.int16)
                                            for length, marker in ((24, 1), (28, 3), (32, 3000))])
        signal_filter, extractor = make_pipeline(pipeline)
        features = [extractor.extract(signal_filter.apply(rep)) for rep in dataset.repetitions]
        standardized_inputs, fitted = [], []
        original_standardize, original_fit = training._standardize_features, training._fit_gesture

        def capture_standardize(X):
            standardized_inputs.append(X.copy())
            return original_standardize(X)

        def capture_fit(*args):
            result = original_fit(*args)
            fitted.append(result[0])
            return result

        with patch.object(training, "_standardize_features", side_effect=capture_standardize), \
                patch.object(training, "_fit_gesture", side_effect=capture_fit):
            bundle = training.train_datasets([dataset], pipeline, n_states=1, calibrate_rejection=True)
        self.assertEqual(len(fitted), 4)
        expected_scores = []
        for held in range(3):
            np.testing.assert_array_equal(standardized_inputs[held],
                                          np.concatenate([row for i, row in enumerate(features) if i != held]))
            expected_scores.append(fitted[held].score(features[held]) / len(features[held]))
        np.testing.assert_array_equal(standardized_inputs[-1], np.concatenate(features))
        self.assertIs(bundle.models["marked"], fitted[-1])
        diagnostics = bundle.metadata["training"]["marked"]["rejection_calibration"]
        np.testing.assert_allclose(diagnostics["held_out_scores_per_frame"], expected_scores)
        expected_margin = max(5.0, 0.5 * np.ptp(expected_scores))
        self.assertAlmostEqual(bundle.rejection["marked"].score_floor, min(expected_scores) - expected_margin)
        self.assertEqual(diagnostics["score_unit"], "log_likelihood_per_feature_frame")

    def test_nonfinite_heldout_score_aborts_calibration(self):
        model = SimpleNamespace(score=lambda features: float("nan"))
        with patch.object(training, "_fit_gesture", return_value=(model, 0, 2)) as fit:
            with self.assertRaisesRegex(ValueError, "拒识标定第 1 次留出分数非有限"):
                training.train_datasets([impulse_dataset()], self.pipeline, segmentation=self.segmentation,
                                        calibrate_rejection=True)
        self.assertEqual(fit.call_count, 1)

    def test_nested_evaluation_never_uses_outer_heldout_in_scaling_or_calibration(self):
        dataset = impulse_dataset(4)
        originals = [rep.copy() for rep in dataset.repetitions]
        signal_filter, extractor = make_pipeline(self.pipeline)
        crops = [prepare_recording(rep, self.segmentation) for rep in originals]
        features = [extractor.extract(signal_filter.apply(crop)) for crop in crops]
        indices = {int(rep[0, 3]): index for index, rep in enumerate(originals)}
        fits, scaled, peaks, bundles = [], [], [], []
        original_fit = training._fit_gesture
        original_scale = training._standardize_features
        original_train = training.train_datasets

        def capture_fit(name, reps, *args):
            ids = [indices[int(rep[0, 3])] for rep in reps]
            for index, rep in zip(ids, reps):
                np.testing.assert_array_equal(rep, crops[index])
            result = original_fit(name, reps, *args)
            fits.append((ids, result[0]))
            return result

        def capture_scale(X):
            scaled.append(X.copy())
            return original_scale(X)

        def capture_peak(raw):
            index = indices[int(raw[0, 3])]
            peaks.append(index)
            np.testing.assert_array_equal(raw, crops[index])
            return impulse_peak(raw)

        def capture_train(split, pipeline, n_states, **options):
            outer = len(bundles)
            self.assertEqual(options, {"segmentation": self.segmentation, "calibrate_rejection": True})
            self.assertEqual([indices[int(rep[0, 3])] for rep in split[0].repetitions],
                             [index for index in range(4) if index != outer])
            # The fold receives full raw takes; cropping is not silently applied twice.
            self.assertTrue(all(len(rep) > 41 for rep in split[0].repetitions))
            bundle = original_train(split, pipeline, n_states, **options)
            bundles.append(bundle)
            return bundle

        with patch.object(evaluation, "train_datasets", side_effect=capture_train), \
                patch.object(training, "_fit_gesture", side_effect=capture_fit), \
                patch.object(training, "_standardize_features", side_effect=capture_scale), \
                patch.object(training, "impulse_peak", side_effect=capture_peak):
            result = evaluation.evaluate_leave_one_out([dataset], self.pipeline, segmentation=self.segmentation,
                                                      calibrate_rejection=True)
        self.assertEqual((result["total"], result["metric"]), (4, "positive_acceptance_rate"))
        self.assertEqual([row["recording_index"] for row in result["predictions"]], list(range(4)))
        self.assertEqual(len(fits), 16)
        self.assertEqual(len(scaled), 16)
        self.assertEqual(len(peaks), 12)
        for outer, bundle in enumerate(bundles):
            training_ids = [index for index in range(4) if index != outer]
            expected_subsets = [[index for index in training_ids if index != held] for held in training_ids]
            expected_subsets.append(training_ids)
            expected_scores = []
            for inner, expected in enumerate(expected_subsets):
                ids, model = fits[outer * 4 + inner]
                self.assertEqual(ids, expected)
                self.assertNotIn(outer, ids)
                np.testing.assert_array_equal(scaled[outer * 4 + inner], np.concatenate([features[i] for i in ids]))
                if inner < 3:
                    held_features = features[training_ids[inner]]
                    expected_scores.append(model.score(held_features) / len(held_features))
            self.assertIs(bundle.models["snap"], fits[outer * 4 + 3][1])
            self.assertEqual(peaks[outer * 3:outer * 3 + 3], training_ids)
            limits = bundle.rejection["snap"]
            self.assertAlmostEqual(limits.peak_floor, 0.25 * min(impulse_peak(crops[i]) for i in training_ids))
            self.assertEqual((limits.min_samples, limits.max_samples), (41, 41))
            self.assertAlmostEqual(limits.score_floor, min(expected_scores) - max(5.0, 0.5 * np.ptp(expected_scores)))
            np.testing.assert_allclose(bundle.metadata["training"]["snap"]["rejection_calibration"]["held_out_scores_per_frame"],
                                       expected_scores)
        for rep, original in zip(dataset.repetitions, originals):
            np.testing.assert_array_equal(rep, original)

    def test_single_class_controls_and_v2_roundtrip_match_offline_and_streaming(self):
        # Low controls still trigger at 1000; rejection must not rely only on
        # the detector ignoring them. The normal 8000 preset is tested above.
        segmentation = SegmentationConfig.for_sample_rate(100, mode="impulse", energy_threshold=1000,
                                                          min_samples=self.pipeline.min_samples)
        dataset = impulse_dataset()
        bundle = training.train_datasets([dataset], self.pipeline, segmentation=segmentation,
                                         calibrate_rejection=True)
        with tempfile.TemporaryDirectory() as folder:
            path = bundle.save(Path(folder) / "snap.gesture.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = GestureBundle.load(path)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["pipeline"], asdict(self.pipeline))
        self.assertEqual(payload["segmentation"], asdict(segmentation))
        self.assertEqual(bundle.rejection, loaded.rejection)
        self.assertEqual(bundle.metadata, loaded.metadata)
        before, after = GestureRecognizer(bundle), GestureRecognizer(loaded)
        positives = [*dataset.repetitions, impulse_take(15, amplitude=18500, onset=83)]
        for raw in positives:
            positive = before.predict(raw)
            self.assertIsNotNone(positive)
            self.assertIsNone(positive.confidence)
            self.assertEqual(positive, after.predict(raw))
            before.reset()
            after.reset()
            predictions = []
            for start in range(0, len(raw), 13):
                predictions.extend(before.feed(raw[start:start + 13]))
            self.assertEqual(len(predictions), 1)
            self.assertEqual(predictions, after.feed(raw))
            streamed = predictions[0]
            self.assertAlmostEqual(streamed.score, positive.score)
            np.testing.assert_array_equal(raw[streamed.start_sample:streamed.end_sample],
                                          prepare_recording(raw, segmentation))
            baseline = raw[0].astype(float)
            low = np.rint(baseline + 0.1 * (raw - baseline)).astype(np.int16)
            idle = np.tile(raw[0], (len(raw), 1))
            self.assertIsNotNone(prepare_recording(low, segmentation))
            self.assertLess(impulse_peak(low), bundle.rejection["snap"].peak_floor)
            for control in (low, idle):
                self.assertIsNone(before.predict(control))
                self.assertIsNone(after.predict(control))
                before.reset()
                after.reset()
                self.assertEqual(before.feed(control), [])
                self.assertEqual(after.feed(control), [])

    def test_cli_exposes_impulse_filter_calibration_and_labels_positive_rate(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "data"
            save_dataset(impulse_dataset(4), directory)
            output = Path(folder) / "model.gesture.json"
            options = ["--data", str(directory), "--sample-rate", "100", "--cutoff-hz", "30",
                       "--median-kernel", "1", "--filter-initialization", "steady", "--mode", "impulse",
                       "--energy-threshold", "8000", "--calibrate-rejection"]
            with patch.object(sys, "argv", ["train", *options, "--output", str(output)]), \
                    redirect_stdout(io.StringIO()) as log:
                training.main()
            bundle = GestureBundle.load(output)
            self.assertEqual(bundle.pipeline, self.pipeline)
            self.assertEqual(bundle.segmentation, self.segmentation)
            self.assertIsNotNone(bundle.rejection)
            self.assertIn("非校准概率", log.getvalue())
            with patch.object(sys, "argv", ["evaluate", *options]), redirect_stdout(io.StringIO()) as log:
                evaluation.main()
            self.assertIn("留出正样本通过率:", log.getvalue())
            self.assertIn("/4", log.getvalue())
            self.assertIn("不是未知动作准确率", log.getvalue())


if __name__ == "__main__":
    unittest.main()
