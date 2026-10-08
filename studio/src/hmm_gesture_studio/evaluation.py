# SPDX-License-Identifier: MPL-2.0
"""Opt-in recording-level validation, without GUI or training-set accuracy.

Usage: python -m hmm_gesture_studio.evaluation --data sample_data
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, TypedDict

from hmm_gesture import GestureRecognizer, PipelineConfig, SegmentationConfig
from hmm_gesture.config import integer
from .datasets import GestureDataset, load_dataset
from .training import _prepare_repetitions, train_datasets


class HeldOutPrediction(TypedDict):
    """Zero-based fold and original recording index (before short filtering)."""

    fold: int
    expected: str
    recording_index: int
    predicted: str | None


class EvaluationResult(TypedDict):
    """Plain dict: accuracy is 0..1; confusion rows are true labels, sparse columns
    are predicted labels (None means no prediction). Per-class counts contain
    'correct' and 'total'. Short motion recordings are not included in totals.
    For a single class, 'accuracy' is only the positive acceptance rate; 'metric'
    makes this explicit. No unknown/negative recordings are evaluated here.
    """

    correct: int
    total: int
    accuracy: float
    per_class: dict[str, dict[str, int]]
    confusion: dict[str, dict[str | None, int]]
    skipped_recordings: dict[str, int]
    predictions: list[HeldOutPrediction]
    metric: str


def evaluate_leave_one_out(datasets: list[GestureDataset], pipeline: PipelineConfig,
                           n_states: int = 6, *,
                           progress: Callable[[str], None] | None = None,
                           segmentation: SegmentationConfig | None = None,
                           calibrate_rejection: bool = False) -> EvaluationResult:
    """Hold out one *whole recording per class* in each fold and train afresh.

    Five usable recordings per class produce five folds, each training on four
    and testing on one per class. Every usable recording is tested exactly once.
    For unequal counts, exhausted classes use all their recordings for training
    in later folds (and contribute no tests). Short recordings are excluded and
    reported in motion mode; malformed impulse takes are errors. Every class
    needs at least three usable recordings, or four with rejection calibration
    (three remain for the trainer's inner leave-one-out). Preflight uses the same
    fixed runtime crop policy as training; it estimates no thresholds.

    Splitting precedes feature extraction, scale estimation and rejection
    calibration. No held-out frames, lengths, peaks or scores enter a fold's
    training/calibration. This function never runs implicitly during normal
    training. Single-class results are positive acceptance rates, not unknown
    accuracy. This is recording-level, not subject/session-level validation,
    and is not an unbiased final test if used to choose a model.
    """
    integer("n_states", n_states)
    if n_states > 512:
        raise ValueError("n_states must not exceed 512")
    minimum_recordings = 4 if calibrate_rejection else 3
    if not datasets:
        raise ValueError(f"没有评估数据，请提供每类至少 {minimum_recordings} 次有效录制")
    crop_config = segmentation or SegmentationConfig()
    # Leave the legacy call unchanged when neither option is requested.
    training_options = {}
    if segmentation is not None:
        training_options["segmentation"] = segmentation
    if calibrate_rejection:
        training_options["calibrate_rejection"] = True
    validated = []
    skipped = {}
    for source in datasets:
        # Work with validated copies: training must not mutate caller arrays,
        # and changes to the source during a progress callback cannot leak in.
        dataset = GestureDataset(source.name, source.repetitions, source.sample_rate_hz)
        if dataset.name in skipped:
            raise ValueError(f"重复手势名称: {dataset.name}")
        if dataset.sample_rate_hz != pipeline.sample_rate_hz:
            raise ValueError(f"{dataset.name}: sample_rate_hz={dataset.sample_rate_hz:g} does not match "
                             f"evaluation rate {pipeline.sample_rate_hz:g}; 不支持混合采样率")
        prepared = _prepare_repetitions(dataset, pipeline, crop_config)
        # Retain the original raw takes. Fold training and predict each use the
        # runtime helper themselves; do not re-crop already shortened windows.
        usable = [(index, dataset.repetitions[index]) for index, _ in prepared]
        skipped[dataset.name] = len(dataset.repetitions) - len(usable)
        if len(usable) < minimum_recordings:
            purpose = "拒识标定" if calibrate_rejection else "训练"
            raise ValueError(f"{dataset.name}: 留出评估需要至少 {minimum_recordings} 次有效录制，每次至少 "
                             f"{pipeline.min_samples} 个采样点（留出 1 次后{purpose}仍需 "
                             f"{minimum_recordings - 1} 次）；只有 {len(usable)} 次有效录制")
        validated.append((dataset, usable))

    predictions: list[HeldOutPrediction] = []
    folds = max(len(usable) for _, usable in validated)
    notify = progress or (lambda message: None)
    metric = "positive_acceptance_rate" if len(validated) == 1 else "classification_accuracy"
    if len(validated) == 1:
        notify("单类留出结果仅为正样本通过率，不代表未知动作拒识准确率。")
    for fold in range(folds):
        training = []
        held_out = []
        for dataset, usable in validated:
            training.append(GestureDataset(dataset.name,
                            [rep for position, (_, rep) in enumerate(usable) if position != fold],
                            dataset.sample_rate_hz))
            if fold < len(usable):
                index, rep = usable[fold]
                held_out.append((dataset.name, index, rep))
        notify(f"留出评估 [{fold + 1}/{folds}]")
        # Only the training split is passed to the normal training/scale path.
        bundle = train_datasets(training, pipeline, n_states, **training_options)
        recognizer = GestureRecognizer(bundle)
        for name, index, rep in held_out:
            prediction = recognizer.predict(rep, sample_rate_hz=pipeline.sample_rate_hz)
            predictions.append(HeldOutPrediction(fold=fold, expected=name, recording_index=index,
                                                  predicted=prediction.name if prediction is not None else None))
    confusion: dict[str, dict[str | None, int]] = {name: {} for name in skipped}
    for row in predictions:
        counts = confusion[row["expected"]]
        counts[row["predicted"]] = counts.get(row["predicted"], 0) + 1
    correct = sum(row["expected"] == row["predicted"] for row in predictions)
    return EvaluationResult(correct=correct, total=len(predictions), accuracy=correct / len(predictions),
                            per_class={name: {"correct": counts.get(name, 0), "total": sum(counts.values())}
                                       for name, counts in confusion.items()},
                            confusion=confusion, skipped_recordings=skipped, predictions=predictions,
                            metric=metric)


def main() -> None:
    parser = argparse.ArgumentParser(description="逐次留出评估（每类至少 3 次录制；开启拒识标定需 4 次；不导出模型）")
    parser.add_argument("--data", default="gestures", help="录制数据目录")
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
                        help="嵌套留出标定；每类至少 4 次有效录制（非未知动作准确率）")
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
        result = evaluate_leave_one_out(datasets, pipeline, args.n_states, segmentation=segmentation,
                                       calibrate_rejection=args.calibrate_rejection)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    label = "留出正样本通过率" if result["metric"] == "positive_acceptance_rate" else "留出分类准确率"
    print(f"{label}: {result['correct']}/{result['total']} ({result['accuracy']:.1%})")
    for name, counts in result["per_class"].items():
        print(f"  {name}: {counts['correct']}/{counts['total']}"
              f"，跳过过短录制 {result['skipped_recordings'][name]} 次")
    print("仅代表这些正样本录制的留出结果，不是未知动作准确率；请用独立会话和负样本验证泛化。")


if __name__ == "__main__":
    main()
