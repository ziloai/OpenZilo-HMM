# SPDX-License-Identifier: MPL-2.0
"""Train a complete portable bundle from validated recordings (no GUI required)."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Callable

from hmmlearn import hmm
import numpy as np

from hmm_gesture import GestureBundle, PipelineConfig, SegmentationConfig
from hmm_gesture.config import integer
from hmm_gesture.preprocessing import FeatureExtractor, SignalFilter, make_pipeline, validate_samples
from .datasets import GestureDataset, load_dataset


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
                            tol=1e-4, init_params="", params="mc", random_state=0)
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
    model.covars_ = np.asarray([np.var(frames, axis=0) + 1e-2 for frames in state_data])
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
    model = build_left_right_hmm(actual_states, X, lengths)
    model.fit(X, lengths)
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


def train_datasets(datasets: list[GestureDataset], pipeline: PipelineConfig,
                   n_states: int = 6, segmentation: SegmentationConfig | None = None,
                   progress: Callable[[str], None] | None = None) -> GestureBundle:
    """Train all or raise; never silently export an incomplete gesture set.

    In-sample scores are diagnostics, not validation accuracy. Record additional
    held-out gestures to evaluate recognition in the intended environment.
    """
    integer("n_states", n_states)
    if n_states > 512:
        raise ValueError("n_states must not exceed 512")
    if not datasets:
        raise ValueError("没有训练数据，请先录制或导入手势")
    names = set()
    for dataset in datasets:
        dataset.__post_init__()
        if dataset.name in names:
            raise ValueError(f"重复手势名称: {dataset.name}")
        names.add(dataset.name)
        if dataset.sample_rate_hz != pipeline.sample_rate_hz:
            raise ValueError(f"{dataset.name}: sample_rate_hz={dataset.sample_rate_hz:g} does not match "
                             f"training rate {pipeline.sample_rate_hz:g}; 不支持混合采样率")
        if sum(len(rep) >= pipeline.min_samples for rep in dataset.repetitions) < 2:
            raise ValueError(f"{dataset.name}: 需要至少 2 次有效录制，每次至少 {pipeline.min_samples} 个采样点")
    signal_filter, extractor = make_pipeline(pipeline)
    notify = progress or (lambda message: None)
    models = {}
    summaries = {}
    for index, dataset in enumerate(datasets, 1):
        notify(f"[{index}/{len(datasets)}] 正在训练 {dataset.name} …")
        try:
            model, score, count = _fit_gesture(dataset.name, dataset.repetitions,
                                               n_states, signal_filter, extractor)
        except (ValueError, FloatingPointError) as exc:
            raise ValueError(f"{dataset.name}: 训练失败: {exc}") from exc
        models[dataset.name] = model
        summaries[dataset.name] = {"recordings": len(dataset.repetitions), "used_recordings": count,
                                    "states": model.n_components, "training_score_per_frame": score}
        notify(f"完成 {dataset.name}: {count}/{len(dataset.repetitions)} 次有效录制，"
               f"{model.n_components} 状态，训练分数 {score:.4f}")
    return GestureBundle(models, pipeline, segmentation or SegmentationConfig(), {
        "created_at": datetime.now().astimezone().isoformat(), "trainer": "hmm-gesture-studio/0.2.0",
        "training": summaries,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="训练并导出可供 hmm_gesture 加载的手势文件")
    parser.add_argument("--data", default="gestures", help="录制数据目录")
    parser.add_argument("--output", required=True, help="输出文件，例如 gestures.gesture.json")
    parser.add_argument("--n-states", type=int, default=6)
    parser.add_argument("--sample-rate", type=float, default=25.0)
    parser.add_argument("--cutoff-hz", type=float, default=10.0)
    parser.add_argument("--window-size", type=int, default=8)
    parser.add_argument("--window-overlap", type=int, default=4)
    parser.add_argument("--energy-threshold", type=float, default=1500.0)
    args = parser.parse_args()
    try:
        pipeline = PipelineConfig(sample_rate_hz=args.sample_rate, cutoff_hz=args.cutoff_hz,
                                  window_size=args.window_size, window_overlap=args.window_overlap)
        datasets = [load_dataset(path) for path in sorted(Path(args.data).glob("*.json"))]
        bundle = train_datasets(datasets, pipeline, args.n_states,
                                SegmentationConfig(energy_threshold=args.energy_threshold), print)
        bundle.save(args.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(f"已导出 {len(bundle.gesture_names)} 个手势到 {args.output}")


if __name__ == "__main__":
    main()
