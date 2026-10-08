# SPDX-License-Identifier: MPL-2.0
"""Impulse preservation, exact contexts, safe single-label inference and v1/v2 I/O."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

from hmmlearn.hmm import GaussianHMM
import numpy as np

from hmm_gesture import (
    GestureBundle, GestureRecognizer, PipelineConfig, RejectionCalibration, SegmentationConfig,
)
from hmm_gesture.preprocessing import SignalFilter
from hmm_gesture.segmentation import MotionSegmenter, prepare_recording


def snap_stream(peaks=(50, 160), length=240, amplitude=15000):
    data = np.zeros((length, 6))
    data[:, 2] = 2000
    for index in peaks:
        data[index, 0] = amplitude
        data[index:index + 3, 3] = 2500
    return data


def impulse_settings():
    pipeline = PipelineConfig(sample_rate_hz=100, cutoff_hz=30, median_kernel=1,
                              filter_initialization="steady")
    segmentation = SegmentationConfig.for_sample_rate(100, mode="impulse", energy_threshold=8000,
                                                       min_samples=pipeline.min_samples)
    return pipeline, segmentation


def model():
    fitted = GaussianHMM(n_components=1, covariance_type="diag", init_params="")
    fitted.startprob_ = np.array([1.0])
    fitted.transmat_ = np.array([[1.0]])
    fitted.means_ = np.zeros((1, 24))
    fitted.covars_ = np.ones((1, 24))
    return fitted


def impulse_bundle():
    pipeline, segmentation = impulse_settings()
    return GestureBundle({"响指": model()}, pipeline, segmentation,
                         rejection={"响指": RejectionCalibration(-1e30, 12000, 41, 41)})


class ImpulseFilterTests(unittest.TestCase):
    def test_median_erases_single_sample_signal_but_impulse_preset_preserves_it(self):
        data = np.zeros((100, 6))
        data[50, 0] = 30000
        old = SignalFilter(100, 10, median_kernel=5).apply(data)
        new = SignalFilter(100, 30, median_kernel=1, initialization="steady").apply(data)
        self.assertEqual(np.abs(old[:, 0]).max(), 0)
        self.assertGreater(np.abs(new[:, 0]).max(), 10000)
        self.assertEqual(data[50, 0], 30000)
        self.assertTrue(np.isfinite(new).all())

    def test_steady_filter_does_not_invent_startup_motion_and_legacy_is_unchanged(self):
        data = np.tile([2000, -500, 300, 1, -20, 50], (50, 1))
        new = SignalFilter(100, 30, median_kernel=1, initialization="steady").apply(data)
        old = SignalFilter(100, 30, median_kernel=1).apply(data)
        np.testing.assert_allclose(new, data, atol=1e-10)
        self.assertFalse(np.allclose(old[0], data[0]))
        self.assertEqual(PipelineConfig().filter_initialization, "zero")


class ImpulseSegmentationTests(unittest.TestCase):
    def test_contexts_are_exact_and_batch_independent(self):
        _, config = impulse_settings()
        data = snap_stream()
        for size in (1, 7, 32, 1000):
            segmenter = MotionSegmenter(**asdict(config))
            segments = []
            for start in range(0, len(data), size):
                segments.extend(segmenter.feed_segments(data[start:start + size]))
            self.assertEqual([(s.start_sample, s.end_sample) for s in segments], [(38, 79), (148, 189)])
            for segment in segments:
                np.testing.assert_array_equal(segment.samples, data[segment.start_sample:segment.end_sample])
            segmenter.reset()
            self.assertEqual(segmenter.feed_segments(data)[0].start_sample, 38)

    def test_same_preparation_as_live_rejects_missing_or_multiple_events(self):
        _, config = impulse_settings()
        data = snap_stream(peaks=(50,), length=100)
        np.testing.assert_array_equal(prepare_recording(data, config), data[38:79])
        for invalid in (snap_stream(), snap_stream(peaks=()), data[:60],
                        snap_stream(peaks=(3,), length=100), np.empty((0, 6))):
            self.assertIsNone(prepare_recording(invalid, config))
        segmenter = MotionSegmenter(**asdict(config))
        self.assertEqual(segmenter.feed_segments(data[:60]), [])
        complete = segmenter.feed_segments(data[60:])
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0].end_sample, 79)

    def test_cooldown_uses_trigger_time_not_completion_time(self):
        _, config = impulse_settings()
        # 10 frames apart is one event/ring-down; 65 frames apart is a new event.
        data = snap_stream(peaks=(50, 60, 115), length=170)
        segments = MotionSegmenter(**asdict(config)).feed_segments(data)
        self.assertEqual([(s.start_sample, s.end_sample) for s in segments], [(38, 79), (103, 144)])

    def test_time_based_motion_limits_scale_and_old_defaults_stay_fixed(self):
        scaled = SegmentationConfig.for_sample_rate(100)
        self.assertEqual(scaled.max_gesture_len, 500)
        self.assertEqual(scaled.min_offset_frames, 20)
        self.assertEqual(scaled.cooldown_frames, 60)
        self.assertEqual(SegmentationConfig().max_gesture_len, 125)
        for rate in (25, 50, 100, 200):
            settings = SegmentationConfig.for_sample_rate(rate, mode="impulse")
            self.assertGreaterEqual(settings.pre_roll + 1 + settings.post_roll, 12)

    def test_invalid_impulse_parameters_are_rejected(self):
        for values in ({"mode": "other"}, {"mode": "impulse"},
                       {"mode": "impulse", "post_roll": 1},
                       {"post_roll": -1}, {"post_roll": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SegmentationConfig(**values)


class SafeRecognitionTests(unittest.TestCase):
    def test_single_label_never_fabricates_confidence_and_uncalibrated_feed_is_blocked(self):
        uncalibrated = GestureBundle({"only": model()})
        recognizer = GestureRecognizer(uncalibrated)
        self.assertIsNone(recognizer.predict(np.zeros((20, 6))).confidence)
        self.assertEqual(recognizer.feed([]), [])
        with self.assertRaisesRegex(ValueError, "拒识"):
            recognizer.feed(np.zeros((20, 6)))
        self.assertIsNone(GestureRecognizer(uncalibrated, min_confidence=0.1).predict(np.zeros((20, 6))))

    def test_accepted_prediction_covers_the_actual_impulse_and_matches_offline(self):
        recognizer = GestureRecognizer(impulse_bundle())
        data = snap_stream(peaks=(50,), length=100)
        offline = recognizer.predict(data)
        self.assertIsNotNone(offline)
        results = recognizer.feed(data)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0].start_sample, results[0].end_sample), (38, 79))
        self.assertIsNone(results[0].confidence)
        self.assertEqual(results[0].score, offline.score)
        self.assertEqual(results[0].scores, offline.scores)
        self.assertIsNone(offline.start_sample)

    def test_peak_score_and_duration_rejections_are_independent(self):
        data = snap_stream(peaks=(50,), length=100)
        recognizer = GestureRecognizer(impulse_bundle())
        # It passes the 8000 trigger, but not the model's 12000 acceptance floor.
        weak = snap_stream(peaks=(50,), length=100, amplitude=9000)
        self.assertIsNone(recognizer.predict(weak))
        self.assertEqual(recognizer.feed(weak), [])
        self.assertIsNone(recognizer.predict(snap_stream(peaks=(), length=100)))
        bundle = impulse_bundle()
        bundle.rejection["响指"] = replace(bundle.rejection["响指"], score_floor=0)
        self.assertIsNone(GestureRecognizer(bundle).predict(data))
        bundle.rejection["响指"] = RejectionCalibration(-1e30, 0, 50, 100)
        self.assertIsNone(GestureRecognizer(bundle).predict(data))


class BundleCompatibilityTests(unittest.TestCase):
    def test_v1_retains_original_preprocessing_and_scores(self):
        bundle = GestureBundle({"old": model()})
        samples = np.tile([1000, 500, 1500, 100, 50, -200], (24, 1))
        with tempfile.TemporaryDirectory() as folder:
            path = bundle.save(Path(folder) / "v1.json")
            raw = json.loads(path.read_text())
            self.assertEqual(raw["version"], 1)
            self.assertNotIn("filter_initialization", raw["pipeline"])
            self.assertNotIn("mode", raw["segmentation"])
            loaded = GestureBundle.load(path)
        self.assertEqual(loaded.pipeline, bundle.pipeline)
        self.assertEqual(loaded.segmentation, bundle.segmentation)
        self.assertIsNone(loaded.rejection)
        self.assertEqual(GestureRecognizer(loaded).predict(samples).scores,
                         GestureRecognizer(bundle).predict(samples).scores)

    def test_v2_roundtrip_and_validation(self):
        bundle = impulse_bundle()
        with tempfile.TemporaryDirectory() as folder:
            path = bundle.save(Path(folder) / "v2.json")
            raw = json.loads(path.read_text())
            self.assertEqual(raw["version"], 2)
            loaded = GestureBundle.load(path)
            self.assertEqual(loaded.pipeline, bundle.pipeline)
            self.assertEqual(loaded.segmentation, bundle.segmentation)
            self.assertEqual(loaded.rejection, bundle.rejection)
            self.assertEqual(GestureRecognizer(loaded).feed(snap_stream()),
                             GestureRecognizer(bundle).feed(snap_stream()))
            mutations = [lambda r: r.pop("rejection"), lambda r: r.update(rejection={}),
                         lambda r: r["pipeline"].update(filter_initialization="future"),
                         lambda r: r["segmentation"].update(mode="future"),
                         lambda r: r["rejection"]["响指"].update(score_floor=float("nan")),
                         lambda r: r["rejection"]["响指"].update(peak_floor=-1),
                         lambda r: r["rejection"]["响指"].update(min_samples=True),
                         lambda r: r["rejection"]["响指"].update(max_samples=1)]
            for mutate in mutations:
                modified = json.loads(json.dumps(raw))
                mutate(modified)
                path.write_text(json.dumps(modified))
                with self.assertRaises(ValueError):
                    GestureBundle.load(path)


if __name__ == "__main__":
    unittest.main()
