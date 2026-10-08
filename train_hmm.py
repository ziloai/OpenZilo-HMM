# SPDX-License-Identifier: MPL-2.0
"""Legacy pickle-training CLI. New integrations should use hmm-gesture-train.

The algorithm lives in hmm_gesture_studio.training; this entry point only
preserves the original per-gesture .pkl output format for existing users.
"""
from __future__ import annotations

import argparse
import pickle
from pathlib import Path

from hmm_gesture.preprocessing import FeatureExtractor, SignalFilter
from hmm_gesture_studio.training import build_left_right_hmm, load_gesture_data, train_gesture


def main():
    parser = argparse.ArgumentParser(description="训练 HMM 手势模型（旧版 pickle 输出）")
    parser.add_argument("--data", default="gestures", help="手势数据目录")
    parser.add_argument("--output", default="models", help="模型输出目录")
    parser.add_argument("--n-states", type=int, default=6, help="HMM 隐状态数")
    parser.add_argument("--sample-rate", type=float, default=25.0, help="IMU 采样率")
    parser.add_argument("--cutoff-hz", type=float, default=10.0, help="低通截止频率")
    parser.add_argument("--window-size", type=int, default=8, help="特征窗口大小")
    parser.add_argument("--window-overlap", type=int, default=4, help="特征窗口重叠")
    args = parser.parse_args()
    if not 1 <= args.n_states <= 512:
        parser.error("--n-states must be between 1 and 512")
    try:
        gesture_files = sorted(Path(args.data).glob("*.json"))
        if not gesture_files:
            raise ValueError(f"{args.data} 下没有手势数据文件")
        signal_filter = SignalFilter(sample_rate=args.sample_rate, cutoff_hz=args.cutoff_hz)
        extractor = FeatureExtractor(window_size=args.window_size, overlap=args.window_overlap)
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        trained = 0
        for path in gesture_files:
            name, reps = load_gesture_data(path, expected_sample_rate=args.sample_rate)
            model = train_gesture(name, reps, args.n_states, signal_filter, extractor)
            if model is not None:
                with (output_dir / f"{path.stem}.pkl").open("wb") as stream:
                    pickle.dump(model, stream)
                trained += 1
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(f"训练完成: {trained}/{len(gesture_files)} 个模型已保存到 {output_dir}")


if __name__ == "__main__":
    main()
