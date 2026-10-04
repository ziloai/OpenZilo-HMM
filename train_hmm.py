# SPDX-License-Identifier: MPL-2.0
"""HMM 手势模型训练脚本。

从 gestures/*.json 读取录制数据，对每个手势训练一个 Left-Right GaussianHMM，
输出 pickle 模型到 models/。

用法:
    python train_hmm.py [--data gestures] [--output models] [--n-states 6]
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
from hmmlearn import hmm

from signal_filter import SignalFilter
from feature_extractor import FeatureExtractor


def load_gesture_data(
    path: Path, expected_sample_rate: float | None = None,
) -> tuple[str, list[np.ndarray]]:
    """加载手势 JSON 文件，返回 (名称, 重复列表)，检查采样率。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (expected_sample_rate is not None
            and raw.get("sample_rate_hz", expected_sample_rate) != expected_sample_rate):
        raise ValueError(
            f"{path}: sample_rate_hz={raw['sample_rate_hz']} does not match "
            f"--sample-rate={expected_sample_rate:g}. Do not mix sampling rates."
        )
    name = raw["name"]
    reps = [np.array(rep["data"], dtype=np.int16) for rep in raw["repetitions"]]
    return name, reps


def build_left_right_hmm(n_states: int, X: np.ndarray, lengths: list[int]) -> hmm.GaussianHMM:
    """构建 Left-Right 拓扑 HMM 并用数据初始化参数。

    Left-Right 拓扑: 状态只能保持或前进，不能回退。
    startprob = [1, 0, 0, ...] 表示必须从状态 0 开始。
    transmat[i][i] = 0.7 (自环), transmat[i][i+1] = 0.3 (前进)。
    """
    model = hmm.GaussianHMM(
        n_components=n_states,
        covariance_type="diag",
        n_iter=100,
        tol=1e-4,
        init_params="",
        params="mc",
    )
    startprob = np.zeros(n_states)
    startprob[0] = 1.0
    model.startprob_ = startprob

    transmat = np.zeros((n_states, n_states))
    for i in range(n_states):
        if i < n_states - 1:
            transmat[i, i] = 0.7
            transmat[i, i + 1] = 0.3
        else:
            transmat[i, i] = 1.0
    model.transmat_ = transmat

    # 按时间顺序将数据分段，初始化每个状态的均值和方差
    n_features = X.shape[1]
    means = np.zeros((n_states, n_features))
    covars = np.zeros((n_states, n_features))
    offset = 0
    state_data: list[list[np.ndarray]] = [[] for _ in range(n_states)]
    for length in lengths:
        seq = X[offset : offset + length]
        segment_size = length / n_states
        for t in range(length):
            state_idx = min(int(t / segment_size), n_states - 1)
            state_data[state_idx].append(seq[t])
        offset += length
    for s in range(n_states):
        frames = np.array(state_data[s])
        means[s] = frames.mean(axis=0)
        covars[s] = frames.var(axis=0) + 1e-2
    model.means_ = means
    model.covars_ = covars

    return model


def train_gesture(
    name: str,
    reps: list[np.ndarray],
    n_states: int,
    signal_filter: SignalFilter,
    extractor: FeatureExtractor,
) -> hmm.GaussianHMM | None:
    """训练单个手势的 HMM 模型。"""
    all_features = []
    lengths = []

    for rep in reps:
        filtered = signal_filter.apply(rep)
        features = extractor.extract(filtered)
        if features.shape[0] < 2:
            continue
        all_features.append(features)
        lengths.append(features.shape[0])

    if len(all_features) < 2:
        print(f"  [跳过] {name}: 有效样本不足 (需要>=2, 仅有 {len(all_features)})")
        return None

    X = np.concatenate(all_features)
    actual_states = min(n_states, max(2, min(lengths) // 3))

    model = build_left_right_hmm(actual_states, X, lengths)
    try:
        model.fit(X, lengths)
        # 修复训练后 transmat 行和为 0 的问题（数据太短导致某些状态未被访问）
        row_sums = model.transmat_.sum(axis=1)
        zero_rows = row_sums == 0
        if zero_rows.any():
            for i in np.where(zero_rows)[0]:
                model.transmat_[i, i] = 1.0
    except Exception as e:
        print(f"  [失败] {name}: 训练异常 - {e}")
        return None

    try:
        score = model.score(X, lengths) / sum(lengths)
    except Exception:
        score = float("nan")
    print(f"  [完成] {name}: states={actual_states}, samples={len(reps)}, "
          f"frames={sum(lengths)}, avg_ll={score:.4f}")
    return model


def main():
    parser = argparse.ArgumentParser(description="训练 HMM 手势模型")
    parser.add_argument("--data", default="gestures", help="手势数据目录")
    parser.add_argument("--output", default="models", help="模型输出目录")
    parser.add_argument("--n-states", type=int, default=6, help="HMM 隐状态数")
    parser.add_argument("--sample-rate", type=float, default=25.0, help="IMU 采样率")
    parser.add_argument("--cutoff-hz", type=float, default=10.0, help="低通截止频率")
    parser.add_argument("--window-size", type=int, default=8, help="特征窗口大小")
    parser.add_argument("--window-overlap", type=int, default=4, help="特征窗口重叠")
    args = parser.parse_args()

    data_dir = Path(args.data)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not data_dir.exists():
        print(f"错误: 数据目录不存在 {data_dir}")
        sys.exit(1)

    gesture_files = list(data_dir.glob("*.json"))
    if not gesture_files:
        print(f"错误: {data_dir} 下没有手势数据文件")
        sys.exit(1)

    signal_filter = SignalFilter(
        sample_rate=args.sample_rate,
        cutoff_hz=args.cutoff_hz,
    )
    extractor = FeatureExtractor(
        window_size=args.window_size,
        overlap=args.window_overlap,
    )

    print(f"训练参数: n_states={args.n_states}, cutoff={args.cutoff_hz}Hz, "
          f"window={args.window_size}, overlap={args.window_overlap}")
    print(f"数据目录: {data_dir} ({len(gesture_files)} 个手势)")
    print("-" * 50)

    trained = 0
    for gf in gesture_files:
        try:
            name, reps = load_gesture_data(gf, expected_sample_rate=args.sample_rate)
        except ValueError as exc:
            parser.exit(1, f"Error: {exc}\n")
        model = train_gesture(name, reps, args.n_states, signal_filter, extractor)
        if model is not None:
            out_path = output_dir / f"{gf.stem}.pkl"
            with open(out_path, "wb") as f:
                pickle.dump(model, f)
            trained += 1

    print("-" * 50)
    print(f"训练完成: {trained}/{len(gesture_files)} 个模型已保存到 {output_dir}")


if __name__ == "__main__":
    main()
