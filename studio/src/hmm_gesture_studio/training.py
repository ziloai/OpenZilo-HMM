# SPDX-License-Identifier: MPL-2.0
"""Train a complete portable bundle from validated recordings (no GUI required)."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from hmmlearn import hmm
import numpy as np

from hmm_gesture import GestureBundle, PipelineConfig, RejectionCalibration, SegmentationConfig
from hmm_gesture.config import integer
from hmm_gesture.preprocessing import FeatureExtractor, SignalFilter, make_pipeline, validate_samples
from hmm_gesture.segmentation import impulse_peak, prepare_recording
from .datasets import GestureDataset, load_dataset


# In standardized feature units: a state's standard deviation cannot fall below
# 10% of the training gesture's feature standard deviation. This is independent
# of the very different units of mean, variance, RMS and zero-crossing features.
_MIN_VARIANCE = 1e-2


def _standardize_features(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Estimate scale from this model's TRAINING frames only, never held-out data."""
    center = X.mean(axis=0)
    scale = X.std(axis=0)
    # Constant / numerically constant features still need positive variances.
    constant = scale <= np.finfo(float).eps * np.maximum(1.0, np.abs(center))
    scale[constant] = 1.0
    return (X - center) / scale, center, scale


def _restore_feature_scale(model: hmm.GaussianHMM, center: np.ndarray,
                           scale: np.ndarray) -> hmm.GaussianHMM:
    """Fold the affine transform into ordinary diagonal Gaussian parameters.

    Raw log likelihood = standardized log likelihood - N * sum(log(scale)).
    Each gesture has its own scale, so comparing standardized scores directly
    would be wrong. Restoring parameters includes that Jacobian automatically
    and keeps both legacy pickle models and v1 JSON exports scaler-free.
    """
    model.means_ = model.means_ * scale + center
    model.covars_ = model._covars_ * scale ** 2
    return model


def load_gesture_data(path: Path, expected_sample_rate: float | None = None) -> tuple[str, list[np.ndarray]]:
    """Compatibility API for the original training CLI."""
    dataset = load_dataset(path)
    if expected_sample_rate is not None and dataset.sample_rate_hz != expected_sample_rate:
        raise ValueError(f"{path}: sample_rate_hz={dataset.sample_rate_hz:g} does not match "
                         f"--sample-rate={expected_sample_rate:g}. Do not mix sampling rates.")
    return dataset.name, dataset.repetitions


def build_left_right_hmm(n_states: int, X: np.ndarray, lengths: list[int]) -> hmm.GaussianHMM:
    integer("n_states", n_states)
    if n_states > 512:
        raise ValueError("n_states must not exceed 512")
    if not lengths or min(lengths) < n_states or sum(lengths) != len(X):
        raise ValueError("Each sequence must have at least n_states feature frames")
    model = hmm.GaussianHMM(n_components=n_states, covariance_type="diag", n_iter=100,
                            tol=1e-4, init_params="", params="mc", random_state=0,
                            covars_prior=_MIN_VARIANCE)
    model.startprob_ = np.zeros(n_states)
    model.startprob_[0] = 1.0
    model.transmat_ = np.zeros((n_states, n_states))
    for i in range(n_states - 1):
        model.transmat_[i, i:i + 2] = [0.7, 0.3]
    model.transmat_[-1, -1] = 1.0
    state_data: list[list[np.ndarray]] = [[] for _ in range(n_states)]
    offset = 0
    for length in lengths:
        for t, frame in enumerate(X[offset:offset + length]):
            state_data[min(int(t * n_states / length), n_states - 1)].append(frame)
        offset += length
    model.means_ = np.asarray([np.mean(frames, axis=0) for frames in state_data])
    model.covars_ = np.asarray([np.var(frames, axis=0) + _MIN_VARIANCE for frames in state_data])
    return model


def _fit_gesture(name: str, reps: list[np.ndarray], n_states: int,
                 signal_filter: SignalFilter, extractor: FeatureExtractor) -> tuple[hmm.GaussianHMM, float, int]:
    integer("n_states", n_states)
    if n_states > 512:
        raise ValueError("n_states must not exceed 512")
    all_features = []
    minimum = 2 * extractor.window_size - extractor.overlap
    for rep in reps:
        data = validate_samples(rep)
        if len(data) < minimum:
            continue
        all_features.append(extractor.extract(signal_filter.apply(data)))
    if len(all_features) < 2:
        raise ValueError(f"{name}: 有效样本不足，需要至少 2 次、每次至少 {minimum} 个采样点")
    lengths = [len(features) for features in all_features]
    X = np.concatenate(all_features)
    actual_states = min(n_states, max(2, min(lengths) // 3))
    standardized, center, scale = _standardize_features(X)
    model = build_left_right_hmm(actual_states, standardized, lengths)
    model.fit(standardized, lengths)
    # The prior now acts in comparable units during EM. Also floor the final
    # emissions: hmmlearn's min_covar only affects automatic initialization,
    # not fitted covariances. Scaling without regularization is not an upgrade.
    model.covars_ = np.maximum(model._covars_, _MIN_VARIANCE)
    model = _restore_feature_scale(model, center, scale)
    if not np.isfinite(model.means_).all() or not np.isfinite(model._covars_).all():
        raise ValueError(f"{name}: 训练得到非有限模型参数，请检查录制数据")
    score = float(model.score(X, lengths) / sum(lengths))
    if not np.isfinite(score):
        raise ValueError(f"{name}: 训练得到非有限分数，请检查录制数据")
    return model, score, len(all_features)


def train_gesture(name: str, reps: list[np.ndarray], n_states: int,
                  signal_filter: SignalFilter, extractor: FeatureExtractor) -> hmm.GaussianHMM | None:
    """Legacy CLI API: log and skip a failed gesture instead of aborting a bundle."""
    try:
        model, score, count = _fit_gesture(name, reps, n_states, signal_filter, extractor)
    except (ValueError, FloatingPointError) as exc:
        print(f"  [失败] {name}: {exc}")
        return None
    print(f"  [完成] {name}: states={model.n_components}, samples={count}, avg_ll={score:.4f}")
    return model


def _prepare_repetitions(dataset: GestureDataset, pipeline: PipelineConfig,
                         segmentation: SegmentationConfig) -> list[tuple[int, np.ndarray]]:
    """Keep original indices and use the runtime's raw-recording crop policy.

    Only legacy motion recordings may be skipped for being short. An impulse
    take with zero/multiple/incomplete events is an error, not a choice of which
    event or waiting time to train on. Neither crops nor thresholds are learned
    here, so evaluation can use this same preflight without fitting held-out data.
    """
    usable = []
    for index, rep in enumerate(dataset.repetitions):
        prepared = prepare_recording(rep, segmentation)
        if segmentation.mode == "impulse" and (prepared is None or len(prepared) < pipeline.min_samples):
            raise ValueError(f"{dataset.name}: 第 {index + 1} 次录制无效；impulse 模式需要恰好一个动作和完整前后文"
                             f"（触发前 {segmentation.pre_roll} 点、后 {segmentation.post_roll} 点，"
                             f"裁剪后至少 {pipeline.min_samples} 个采样点），请重新录制或检查上下文设置")
        if prepared is not None and len(prepared) >= pipeline.min_samples:
            usable.append((index, prepared))
    return usable


def _calibrate_rejection(name: str, reps: list[np.ndarray], n_states: int,
                         signal_filter: SignalFilter, extractor: FeatureExtractor,
                         pipeline: PipelineConfig, segmentation: SegmentationConfig,
                         notify: Callable[[str], None]) -> tuple[RejectionCalibration, dict]:
    """Conservative positive-only limits, NOT probabilities or unknown accuracy.

    Each held-out score comes from a fresh model/scaler fitted on the other
    recordings only. The final all-recording model is fitted separately. The
    fixed margin is deliberately permissive for small positive-only datasets;
    it does not establish how well arbitrary unknown movements are rejected.
    """
    scores = []
    for index, rep in enumerate(reps):
        model, _, _ = _fit_gesture(name, reps[:index] + reps[index + 1:],
                                   n_states, signal_filter, extractor)
        features = extractor.extract(signal_filter.apply(rep))
        score = float(model.score(features) / len(features))
        if not np.isfinite(score):
            raise ValueError(f"{name}: 拒识标定第 {index + 1} 次留出分数非有限，请检查录制数据")
        scores.append(score)
        notify(f"拒识标定 {name} [{index + 1}/{len(reps)}]: 留出每特征帧分数 {score:.4f}")
    margin = max(5.0, 0.5 * float(np.ptp(scores)))
    lengths = [len(rep) for rep in reps]
    if segmentation.mode == "impulse":
        minimum = maximum = segmentation.pre_roll + 1 + segmentation.post_roll
        peak_floor = 0.25 * min(impulse_peak(rep) for rep in reps)
    else:
        minimum = max(pipeline.min_samples, min(lengths) // 2)
        maximum = max(pipeline.min_samples, 2 * max(lengths))
        peak_floor = 0.0
    limits = RejectionCalibration(min(scores) - margin, peak_floor, minimum, maximum)
    diagnostics = {"method": "leave_one_recording_out", "positive_only": True,
                   "score_unit": "log_likelihood_per_feature_frame",
                   "held_out_scores_per_frame": scores, "score_margin": margin, **asdict(limits)}
    notify(f"{name}: 正样本保守拒识门限（非校准概率，未评估未知动作）："
           f"score >= {limits.score_floor:.4f}, peak >= {limits.peak_floor:.1f}, "
           f"长度 {limits.min_samples}..{limits.max_samples}")
    return limits, diagnostics


def train_datasets(datasets: list[GestureDataset], pipeline: PipelineConfig,
                   n_states: int = 6, segmentation: SegmentationConfig | None = None,
                   progress: Callable[[str], None] | None = None, *,
                   calibrate_rejection: bool = False) -> GestureBundle:
    """Train all or raise; never silently export an incomplete gesture set.

    In-sample scores are diagnostics, not validation accuracy. Opt-in rejection
    uses at least three usable recordings per class, with inner recording-level
    leave-one-out scores. These are positive-only acceptance limits, not a
    calibrated probability or a validation of unknown-gesture rejection.

    Run evaluation.evaluate_leave_one_out explicitly for outer recording-level
    validation; normal training does not run it. Collect independent sessions
    and negative controls to assess recognition in the intended environment.
    """
    integer("n_states", n_states)
    if n_states > 512:
        raise ValueError("n_states must not exceed 512")
    if not datasets:
        raise ValueError("没有训练数据，请先录制或导入手势")
    segmentation = segmentation or SegmentationConfig()
    minimum_recordings = 3 if calibrate_rejection else 2
    names = set()
    prepared_datasets = []
    for dataset in datasets:
        dataset.__post_init__()
        if dataset.name in names:
            raise ValueError(f"重复手势名称: {dataset.name}")
        names.add(dataset.name)
        if dataset.sample_rate_hz != pipeline.sample_rate_hz:
            raise ValueError(f"{dataset.name}: sample_rate_hz={dataset.sample_rate_hz:g} does not match "
                             f"training rate {pipeline.sample_rate_hz:g}; 不支持混合采样率")
        prepared = _prepare_repetitions(dataset, pipeline, segmentation)
        if len(prepared) < minimum_recordings:
            purpose = "拒识标定" if calibrate_rejection else "训练"
            raise ValueError(f"{dataset.name}: {purpose}需要至少 {minimum_recordings} 次有效录制，"
                             f"每次至少 {pipeline.min_samples} 个采样点")
        prepared_datasets.append((dataset, [rep for _, rep in prepared]))
    signal_filter, extractor = make_pipeline(pipeline)
    notify = progress or (lambda message: None)
    models = {}
    summaries = {}
    rejection = {} if calibrate_rejection else None
    for index, (dataset, reps) in enumerate(prepared_datasets, 1):
        notify(f"[{index}/{len(datasets)}] 正在训练 {dataset.name} …")
        diagnostics = None
        try:
            if calibrate_rejection:
                limits, diagnostics = _calibrate_rejection(dataset.name, reps, n_states,
                    signal_filter, extractor, pipeline, segmentation, notify)
                rejection[dataset.name] = limits
            model, score, count = _fit_gesture(dataset.name, reps, n_states, signal_filter, extractor)
        except (ValueError, FloatingPointError) as exc:
            raise ValueError(f"{dataset.name}: 训练失败: {exc}") from exc
        models[dataset.name] = model
        summaries[dataset.name] = {"recordings": len(dataset.repetitions), "used_recordings": count,
                                    "states": model.n_components, "training_score_per_frame": score}
        if diagnostics is not None:
            summaries[dataset.name]["rejection_calibration"] = diagnostics
        notify(f"完成 {dataset.name}: {count}/{len(dataset.repetitions)} 次有效录制，"
               f"{model.n_components} 状态，训练分数 {score:.4f}")
    return GestureBundle(models, pipeline, segmentation, {
        "created_at": datetime.now().astimezone().isoformat(), "trainer": "hmm-gesture-studio/0.2.0",
        "training": summaries,
    }, rejection=rejection)


def main() -> None:
    parser = argparse.ArgumentParser(description="训练并导出可供 hmm_gesture 加载的手势文件")
    parser.add_argument("--data", default="gestures", help="录制数据目录")
    parser.add_argument("--output", required=True, help="输出文件，例如 gestures.gesture.json")
    parser.add_argument("--n-states", type=int, default=6)
    parser.add_argument("--sample-rate", type=float, default=25.0)
    parser.add_argument("--cutoff-hz", type=float, default=10.0)
    parser.add_argument("--median-kernel", type=int, default=5, help="奇数；1 禁用中值滤波")
    parser.add_argument("--filter-initialization", choices=("zero", "steady"), default="zero")
    parser.add_argument("--window-size", type=int, default=8)
    parser.add_argument("--window-overlap", type=int, default=4)
    parser.add_argument("--mode", choices=("motion", "impulse"), default="motion")
    parser.add_argument("--energy-threshold", type=float, default=1500.0,
                        help="motion: 基线距离；impulse: 相邻加速度差范数（原始单位）")
    parser.add_argument("--calibrate-rejection", action="store_true",
                        help="至少 3 次有效录制；标定正样本拒识门限（非概率）")
    args = parser.parse_args()
    try:
        pipeline = PipelineConfig(sample_rate_hz=args.sample_rate, cutoff_hz=args.cutoff_hz,
                                  median_kernel=args.median_kernel,
                                  filter_initialization=args.filter_initialization,
                                  window_size=args.window_size, window_overlap=args.window_overlap)
        segmentation = (SegmentationConfig.for_sample_rate(pipeline.sample_rate_hz, mode="impulse",
                            energy_threshold=args.energy_threshold, min_samples=pipeline.min_samples)
                        if args.mode == "impulse" else SegmentationConfig(energy_threshold=args.energy_threshold))
        datasets = [load_dataset(path) for path in sorted(Path(args.data).glob("*.json"))]
        bundle = train_datasets(datasets, pipeline, args.n_states, segmentation, print,
                                calibrate_rejection=args.calibrate_rejection)
        bundle.save(args.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(f"已导出 {len(bundle.gesture_names)} 个手势到 {args.output}")


if __name__ == "__main__":
    main()
