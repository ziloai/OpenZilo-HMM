# SPDX-License-Identifier: MPL-2.0
"""Training-space regularization, portable scoring and recording-level validation."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hmmlearn.hmm import GaussianHMM
import numpy as np

from hmm_gesture import GestureBundle, GestureRecognizer, PipelineConfig
from hmm_gesture.preprocessing import make_pipeline
from hmm_gesture_studio import evaluation, training
from hmm_gesture_studio.datasets import GestureDataset, load_dataset, save_dataset

ROOT = Path(__file__).resolve().parents[1]


def marked_dataset(name, markers, *, length=24, rate=25):
    return GestureDataset(name, [np.full((length, 6), marker, dtype=np.int16)
                                 for marker in markers], rate)


def legacy_raw_fit(name, reps, n_states, signal_filter, extractor):
    """Frozen pre-regularization baseline, for an actual held-out comparison.

    Do not call the new initializer/scaler here: that would silently change the
    baseline. These test recordings all meet the original length requirements.
    """
    features = [extractor.extract(signal_filter.apply(rep)) for rep in reps]
    lengths = [len(row) for row in features]
    X = np.concatenate(features)
    states = min(n_states, max(2, min(lengths) // 3))
    model = GaussianHMM(n_components=states, covariance_type="diag", n_iter=100,
                        tol=1e-4, init_params="", params="mc", random_state=0)
    model.startprob_ = np.zeros(states)
    model.startprob_[0] = 1.0
    model.transmat_ = np.zeros((states, states))
    for state in range(states - 1):
        model.transmat_[state, state:state + 2] = [0.7, 0.3]
    model.transmat_[-1, -1] = 1.0
    state_data = [[] for _ in range(states)]
    for sequence in features:
        for t, frame in enumerate(sequence):
            state_data[min(int(t * states / len(sequence)), states - 1)].append(frame)
    model.means_ = np.asarray([np.mean(frames, axis=0) for frames in state_data])
    model.covars_ = np.asarray([np.var(frames, axis=0) + 1e-2 for frames in state_data])
    model.fit(X, lengths)
    return model, float(model.score(X, lengths) / len(X)), len(features)


class TrainingQualityTests(unittest.TestCase):
    def test_restore_scale_preserves_likelihood_and_cross_model_ranking(self):
        rng = np.random.default_rng(32)
        feature_units = np.geomspace(1e-4, 1e6, 24)
        X = rng.normal(0, 0.02, (9, 24)) * feature_units
        standardized_scores, corrected_scores, raw_scores = {}, {}, {}
        for name, width, offset in (("wide", 10.0, 0.0), ("narrow", 0.1, 0.1)):
            scale = feature_units * width
            center = feature_units * offset
            model = GaussianHMM(n_components=2, covariance_type="diag", init_params="")
            model.startprob_ = np.array([1.0, 0.0])
            model.transmat_ = np.array([[0.7, 0.3], [0.0, 1.0]])
            model.means_ = np.zeros((2, 24))
            model.covars_ = np.ones((2, 24))
            standardized_scores[name] = model.score((X - center) / scale)
            corrected_scores[name] = standardized_scores[name] - len(X) * np.log(scale).sum()
            original = deepcopy(model)
            raw = training._restore_feature_scale(model, center, scale)
            raw_scores[name] = raw.score(X)
            self.assertAlmostEqual(raw_scores[name], corrected_scores[name], places=9)
            np.testing.assert_allclose(raw.means_, original.means_ * scale + center)
            np.testing.assert_allclose(raw._covars_, original._covars_ * scale ** 2)
            np.testing.assert_array_equal(raw.transmat_, original.transmat_)
            np.testing.assert_array_equal(raw.startprob_, original.startprob_)
        # Without the per-model Jacobian, the wider model would incorrectly win.
        self.assertEqual(max(standardized_scores, key=standardized_scores.get), "wide")
        self.assertEqual(max(raw_scores, key=raw_scores.get), "narrow")
        self.assertEqual(sorted(raw_scores, key=raw_scores.get),
                         sorted(corrected_scores, key=corrected_scores.get))

    def test_actual_fit_scores_are_in_original_feature_units(self):
        pipeline = PipelineConfig()
        dataset = marked_dataset("varying", [30, 100, 500])
        signal_filter, extractor = make_pipeline(pipeline)
        X = np.concatenate([extractor.extract(signal_filter.apply(rep)) for rep in dataset.repetitions])
        lengths = [len(extractor.extract(signal_filter.apply(rep))) for rep in dataset.repetitions]
        Z, center, scale = training._standardize_features(X)
        before_restore = []
        restore = training._restore_feature_scale

        def capture(model, actual_center, actual_scale):
            before_restore.append(deepcopy(model))
            np.testing.assert_allclose(actual_center, center)
            np.testing.assert_allclose(actual_scale, scale)
            return restore(model, actual_center, actual_scale)

        original_fit = GaussianHMM.fit

        def capture_fit(model, actual_X, actual_lengths):
            np.testing.assert_allclose(actual_X, Z)
            self.assertEqual(actual_lengths, lengths)
            return original_fit(model, actual_X, actual_lengths)

        with patch.object(training, "_restore_feature_scale", side_effect=capture), \
                patch.object(GaussianHMM, "fit", capture_fit):
            bundle = training.train_datasets([dataset], pipeline, n_states=1)
        expected_score = (before_restore[0].score(Z, lengths) / len(X)) - np.log(scale).sum()
        raw_model = bundle.models[dataset.name]
        self.assertAlmostEqual(raw_model.score(X, lengths) / len(X), expected_score, places=9)
        self.assertAlmostEqual(bundle.metadata["training"][dataset.name]["training_score_per_frame"],
                               expected_score, places=9)
        self.assertTrue((before_restore[0]._covars_ >= training._MIN_VARIANCE).all())

    def test_constant_features_have_a_real_variance_floor_even_with_two_frames(self):
        pipeline = PipelineConfig()
        for requested_states in (1, 6, 512):
            with self.subTest(states=requested_states):
                dataset = marked_dataset("constant", [0, 0], length=pipeline.min_samples)
                bundle = training.train_datasets([dataset], pipeline, requested_states)
                model = bundle.models[dataset.name]
                self.assertIs(type(model), GaussianHMM)  # No custom class/scaler needed by legacy pickle.
                self.assertEqual(model.n_components, min(requested_states, 2))
                self.assertTrue(np.isfinite(model.means_).all())
                self.assertTrue(np.isfinite(model._covars_).all())
                self.assertTrue((model._covars_ > 0).all())
                # With only the old .01 prior, EM divides by occupancy and gives
                # a smaller variance. This checks regularization, not just scaling.
                self.assertTrue((model._covars_ >= training._MIN_VARIANCE).all())
                self.assertAlmostEqual(model._covars_.min(), training._MIN_VARIANCE)
                np.testing.assert_array_equal(model.means_, 0)
                self.assertTrue(np.isfinite(GestureRecognizer(bundle).predict(dataset.repetitions[0]).score))

    def test_short_extreme_recordings_keep_finite_left_right_models(self):
        rng = np.random.default_rng(7)
        reps = [rng.integers(-32768, 32768, (12, 6), dtype=np.int16) for _ in range(2)]
        for n_states in (1, 6):
            with self.subTest(states=n_states):
                model = training.train_datasets([GestureDataset("short", reps)], PipelineConfig(),
                                                n_states).models["short"]
                for array in (model.means_, model._covars_, model.transmat_, model.startprob_):
                    self.assertTrue(np.isfinite(array).all())
                self.assertTrue((model._covars_ > 0).all())
                np.testing.assert_allclose(model.transmat_.sum(axis=1), 1)
                allowed = np.eye(model.n_components, dtype=bool) | np.eye(model.n_components, k=1, dtype=bool)
                self.assertTrue((model.transmat_[~allowed] == 0).all())
                np.testing.assert_array_equal(model.startprob_, np.eye(model.n_components)[0])

    def test_v1_export_preserves_raw_scores_without_new_fields(self):
        datasets = [marked_dataset("small", [1, 2, 3]), marked_dataset("large", [1000, 2000, 3000])]
        bundle = training.train_datasets(datasets, PipelineConfig(), n_states=1)
        with tempfile.TemporaryDirectory() as folder:
            path = bundle.save(Path(folder) / "model.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = GestureBundle.load(path)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(set(payload), {"format", "version", "axes", "units", "pipeline", "segmentation",
                                        "gestures", "metadata"})
        self.assertEqual(set(bundle.metadata), {"created_at", "trainer", "training"})
        for entry in payload["gestures"]:
            self.assertEqual(set(entry["model"]), {"type", "covariance_type", "startprob", "transmat", "means", "covars"})
            summary = bundle.metadata["training"][entry["name"]]
            self.assertEqual(set(summary), {"recordings", "used_recordings", "states", "training_score_per_frame"})
            self.assertEqual(summary["recordings"], 3)
            self.assertEqual(summary["used_recordings"], 3)
        before, after = GestureRecognizer(bundle), GestureRecognizer(loaded)
        for dataset in datasets:
            for rep in dataset.repetitions:
                self.assertEqual(before.predict(rep), after.predict(rep))
        self.assertEqual(bundle.metadata, loaded.metadata)

    def test_nonfinite_parameters_are_not_returned(self):
        def invalid_fit(model, X, lengths):
            model.means_[:] = np.nan
            return model

        with patch.object(GaussianHMM, "fit", invalid_fit):
            with self.assertRaisesRegex(ValueError, "非有限模型参数"):
                training.train_datasets([marked_dataset("bad", [1, 2])], PipelineConfig())

    def test_legacy_train_gesture_still_logs_and_skips_failures(self):
        signal_filter, extractor = make_pipeline(PipelineConfig())
        with redirect_stdout(io.StringIO()) as output:
            model = training.train_gesture("ok", marked_dataset("ok", [0, 0]).repetitions,
                                            1, signal_filter, extractor)
            failed = training.train_gesture("short", [np.zeros((8, 6))] * 2,
                                             1, signal_filter, extractor)
        self.assertIs(type(model), GaussianHMM)
        self.assertIsNone(failed)
        self.assertIn("[完成]", output.getvalue())
        self.assertIn("[失败]", output.getvalue())


class HeldOutEvaluationTests(unittest.TestCase):
    def test_split_has_no_test_recordings_in_training_and_tests_each_once(self):
        a = marked_dataset("a", [1, 2, 3])
        a.repetitions.insert(1, np.full((8, 6), 99, dtype=np.int16))
        b = marked_dataset("b", [11, 12, 13, 14])
        datasets = [a, b]
        originals = [[rep.copy() for rep in dataset.repetitions] for dataset in datasets]
        all_ids = {1, 2, 3, 11, 12, 13, 14}
        tests_by_fold = [{1, 11}, {2, 12}, {3, 13}, {14}]
        trained, tested = [], []

        def fake_train(split, pipeline, n_states):
            ids = {int(rep[0, 0]) for dataset in split for rep in dataset.repetitions}
            self.assertEqual(ids, all_ids - tests_by_fold[len(trained)])
            self.assertTrue(all(len(dataset.repetitions) >= 2 for dataset in split))
            self.assertEqual(n_states, 3)
            trained.append(ids)
            return ids

        def fake_recognizer(training_ids):
            def predict(rep, *, sample_rate_hz):
                marker = int(rep[0, 0])
                self.assertNotIn(marker, training_ids)
                self.assertEqual(sample_rate_hz, 25)
                tested.append(marker)
                return SimpleNamespace(name="a" if marker < 10 else "b")
            return SimpleNamespace(predict=predict)

        with patch.object(evaluation, "train_datasets", side_effect=fake_train) as fit, \
                patch.object(evaluation, "GestureRecognizer", side_effect=fake_recognizer):
            result = evaluation.evaluate_leave_one_out(datasets, PipelineConfig(), n_states=3)
        self.assertEqual(fit.call_count, 4)
        self.assertEqual(sorted(tested), sorted(all_ids))
        self.assertIsInstance(result, dict)
        self.assertEqual((result["correct"], result["total"], result["accuracy"]), (7, 7, 1.0))
        self.assertEqual(result["skipped_recordings"], {"a": 1, "b": 0})
        self.assertEqual(result["per_class"], {"a": {"correct": 3, "total": 3}, "b": {"correct": 4, "total": 4}})
        self.assertEqual([row["recording_index"] for row in result["predictions"] if row["expected"] == "a"], [0, 2, 3])
        for dataset, original in zip(datasets, originals):
            for rep, old in zip(dataset.repetitions, original):
                np.testing.assert_array_equal(rep, old)

    def test_real_training_scaler_sees_only_training_recordings(self):
        dataset = marked_dataset("outlier", [1, 2, 30000])
        pipeline = PipelineConfig()
        signal_filter, extractor = make_pipeline(pipeline)
        features = [extractor.extract(signal_filter.apply(rep)) for rep in dataset.repetitions]
        seen = []
        standardize = training._standardize_features

        def capture(X):
            seen.append(X.copy())
            return standardize(X)

        with patch.object(training, "_standardize_features", side_effect=capture):
            result = evaluation.evaluate_leave_one_out([dataset], pipeline, n_states=1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(seen), 3)
        for fold, X in enumerate(seen):
            expected = np.concatenate([f for i, f in enumerate(features) if i != fold])
            np.testing.assert_array_equal(X, expected)
        # The fold testing the outlier cannot use its mean/scale even indirectly.
        self.assertLess(np.abs(seen[2]).max(), np.abs(features[2]).max() / 100)

    def test_preflight_rejects_insufficient_data_before_any_fit(self):
        good = marked_dataset("a", [1, 2, 3])
        cases = [[], [marked_dataset("few", [1, 2])],
                 [good, marked_dataset("short", [1, 2, 3], length=8)],
                 [good, marked_dataset("wrong rate", [1, 2, 3], rate=50)], [good, good]]
        for datasets in cases:
            with self.subTest(names=[dataset.name for dataset in datasets]):
                with patch.object(evaluation, "train_datasets") as fit, self.assertRaises(ValueError):
                    evaluation.evaluate_leave_one_out(datasets, PipelineConfig())
                fit.assert_not_called()
        for n_states in (0, True, 513):
            with self.subTest(n_states=n_states), patch.object(evaluation, "train_datasets") as fit:
                with self.assertRaises(ValueError):
                    evaluation.evaluate_leave_one_out([good], PipelineConfig(), n_states)
                fit.assert_not_called()

    def test_rejections_and_wrong_labels_are_counted_as_incorrect(self):
        recognizer = SimpleNamespace(predict=Mock(side_effect=[
            SimpleNamespace(name="a"), None, SimpleNamespace(name="other")]))
        with patch.object(evaluation, "train_datasets"), \
                patch.object(evaluation, "GestureRecognizer", return_value=recognizer):
            result = evaluation.evaluate_leave_one_out([marked_dataset("a", [1, 2, 3])], PipelineConfig())
        self.assertEqual((result["correct"], result["total"]), (1, 3))
        self.assertAlmostEqual(result["accuracy"], 1 / 3)
        self.assertEqual(result["confusion"], {"a": {"a": 1, None: 1, "other": 1}})

    def test_normal_training_does_not_implicitly_cross_validate(self):
        with patch.object(evaluation, "evaluate_leave_one_out", side_effect=AssertionError("not opt-in")):
            training.train_datasets([marked_dataset("a", [1, 2])], PipelineConfig())

    def test_shipped_recordings_do_not_regress_against_raw_feature_baseline(self):
        datasets = [load_dataset(path) for path in sorted((ROOT / "sample_data").glob("*.json"))]
        with patch.object(training, "_fit_gesture", side_effect=legacy_raw_fit):
            baseline = evaluation.evaluate_leave_one_out(datasets, PipelineConfig())
        candidate = evaluation.evaluate_leave_one_out(datasets, PipelineConfig())
        self.assertEqual(baseline["total"], 30)
        self.assertEqual(candidate["total"], 30)
        self.assertEqual({row["fold"] for row in candidate["predictions"]}, set(range(5)))
        self.assertTrue(all(counts["total"] == 5 for counts in candidate["per_class"].values()))
        self.assertGreaterEqual(candidate["correct"], baseline["correct"])

    def test_standalone_cli_reports_counts_and_errors_on_too_few_recordings(self):
        with tempfile.TemporaryDirectory() as folder:
            command = [sys.executable, "-m", "hmm_gesture_studio.evaluation", "--data"]
            result = subprocess.run([*command, str(ROOT / "sample_data")], cwd=folder,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("/30", result.stdout)
            self.assertIn("向上:", result.stdout)
            save_dataset(marked_dataset("few", [1, 2]), folder)
            failed = subprocess.run([*command, folder], cwd=folder, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("至少 3 次", failed.stderr)
            self.assertNotIn("Traceback", failed.stderr)


if __name__ == "__main__":
    unittest.main()
