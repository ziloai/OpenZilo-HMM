# SPDX-License-Identifier: MPL-2.0
"""HMM 手势识别引擎 —— 加载训练好的模型，流式识别。

可作为独立脚本运行，支持:
1. BLE 戒指实时识别（推荐）
2. 从 CSV/JSON 文件离线识别

用法:
    # 连接戒指实时识别（推荐）
    python recognize.py --models pretrained_models/ --ring --address AA:BB:CC:DD:EE:FF

    # 从 JSON 数据文件离线测试
    python recognize.py --models models/ --input test_data.json

    # 从 CSV 文件离线测试
    python recognize.py --models models/ --input test.csv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import openzilo as sdk

from feature_extractor import FeatureExtractor
from ring_stream import open_ring_stream
from signal_filter import SignalFilter


# Legacy pickle reader retained for existing scripts. New applications should
# use hmm_gesture.GestureRecognizer.load() with a portable Studio export.
from hmm_gesture.segmentation import MotionSegmenter


class HMMRecognizer:
    """基于 Left-Right GaussianHMM 的手势识别器。

    工作流程:
    1. feed() 接收 IMU 数据帧
    2. MotionSegmenter 检测动作片段
    3. SignalFilter 滤波
    4. FeatureExtractor 提取滑窗特征
    5. 对每个已加载模型计算 log-likelihood，选最优
    6. 根据最优与次优的分数差计算置信度
    """

    def __init__(
        self,
        model_dir: str | Path,
        signal_filter: SignalFilter,
        feature_extractor: FeatureExtractor,
        min_confidence: float = 0.0,
    ):
        self.model_dir = Path(model_dir)
        self.filter = signal_filter
        self.extractor = feature_extractor
        self.min_confidence = min_confidence
        self.segmenter = MotionSegmenter()
        self.enabled = True
        self._cooldown_remaining = 0
        self._models: dict[str, object] = {}
        self._load_models()

    def _load_models(self) -> None:
        self._models = {}
        if not self.model_dir.exists():
            return
        for pkl_file in self.model_dir.glob("*.pkl"):
            name = pkl_file.stem
            with open(pkl_file, "rb") as f:
                self._models[name] = pickle.load(f)
        print(f"已加载 {len(self._models)} 个模型: {list(self._models.keys())}")

    def feed(self, samples: list[list[int]]) -> tuple[str, float] | None:
        """喂入 IMU 数据帧，有识别结果时返回 (手势名, 置信度)。"""
        if not self.enabled or not self._models:
            return None

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= len(samples)
            for s in samples:
                self.segmenter._feed_one(s)
            return None

        segment = self.segmenter.feed(samples)
        if segment is None:
            return None

        return self._classify_segment(segment)

    def _classify_segment(self, segment: np.ndarray) -> tuple[str, float] | None:
        filtered = self.filter.apply(segment)
        features = self.extractor.extract(filtered)
        if features.shape[0] < 2:
            return None

        best_name = ""
        best_score = -np.inf
        scores: list[float] = []

        for name, model in self._models.items():
            try:
                score = model.score(features)  # type: ignore[union-attr]
                scores.append(score)
                if score > best_score:
                    best_score = score
                    best_name = name
            except Exception:
                continue

        if not best_name or len(scores) < 1:
            return None

        if len(scores) == 1:
            confidence = 0.8
        else:
            sorted_scores = sorted(scores, reverse=True)
            gap = sorted_scores[0] - sorted_scores[1]
            confidence = min(1.0, max(0.0, 1.0 - np.exp(-gap / 10.0)))

        if confidence < self.min_confidence:
            return None

        self._cooldown_remaining = 25
        return (best_name, confidence)


async def run_realtime(model_dir: Path, address: str, sample_rate: float, cutoff_hz: float,
                      window_size: int, window_overlap: int) -> None:
    """实时识别: 通过 OpenZilo SDK 接收 IMU，在电脑端进行 HMM 分类。"""
    signal_filter = SignalFilter(sample_rate=sample_rate, cutoff_hz=cutoff_hz)
    extractor = FeatureExtractor(window_size=window_size, overlap=window_overlap)
    recognizer = HMMRecognizer(model_dir, signal_filter, extractor)

    if not recognizer._models:
        raise ValueError(f"No models found in {model_dir}")

    async with open_ring_stream(address) as (ring, start_info):
        if start_info.sample_rate_hz != sample_rate:
            raise ValueError(
                f"Device streams at {start_info.sample_rate_hz} Hz, but --sample-rate "
                f"is {sample_rate:g} Hz. Use data and models trained at the device "
                "rate, and pass that rate to both training and recognition. "
                "--sample-rate does not configure the device."
            )

        print("\n" + "=" * 50)
        print("实时手势识别已启动，做手势动作即可识别")
        print("按 Ctrl+C 退出")
        print("=" * 50 + "\n")

        count = 0
        try:
            while True:
                try:
                    batch = await sdk.wait_sensor_data(ring, timeout_s=5.0)
                    samples = [
                        [s.accel_x, s.accel_y, s.accel_z, s.gyro_x, s.gyro_y, s.gyro_z]
                        for s in batch.samples
                    ]
                    result = recognizer.feed(samples)
                    if result:
                        name, conf = result
                        count += 1
                        print(f"  [{count:3d}] 识别到: {name}  (置信度={conf:.3f})")
                except sdk.TimeoutError:
                    print("No IMU data for 5 s. Check the connection and gesture mode.")
        finally:
            print(f"\n已停止，共识别 {count} 次手势")


def run_offline(model_dir: Path, input_path: Path, sample_rate: float, cutoff_hz: float,
                window_size: int, window_overlap: int) -> None:
    """离线测试: 从文件加载数据并直接分类。"""
    signal_filter = SignalFilter(sample_rate=sample_rate, cutoff_hz=cutoff_hz)
    extractor = FeatureExtractor(window_size=window_size, overlap=window_overlap)
    recognizer = HMMRecognizer(model_dir, signal_filter, extractor)

    if not recognizer._models:
        raise ValueError(f"No models found in {model_dir}")

    if input_path.suffix == ".json":
        raw = json.loads(input_path.read_text(encoding="utf-8"))
        if raw.get("sample_rate_hz", sample_rate) != sample_rate:
            raise ValueError(
                f"{input_path}: sample_rate_hz={raw['sample_rate_hz']} does not "
                f"match --sample-rate={sample_rate:g}. Use matching data and models."
            )
        if "repetitions" in raw:
            sequences = [rep["data"] for rep in raw["repetitions"]]
        else:
            sequences = [raw["data"]]
    elif input_path.suffix == ".csv":
        data = np.loadtxt(input_path, delimiter=",", dtype=int).tolist()
        sequences = [data]
    else:
        print(f"不支持的文件格式: {input_path.suffix}")
        sys.exit(1)

    print(f"输入文件: {input_path.name}, 共 {len(sequences)} 段数据")
    print("-" * 50)

    for i, seq in enumerate(sequences):
        segment = np.array(seq, dtype=np.int16)
        result = recognizer._classify_segment(segment)
        if result:
            name, conf = result
            print(f"  段 {i}: 识别为 [{name}] 置信度={conf:.3f}")
        else:
            print(f"  段 {i}: 未识别到手势")

    print("-" * 50)


def main():
    parser = argparse.ArgumentParser(description="HMM 手势识别")
    parser.add_argument("--models", default="models", help="模型目录")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ring", action="store_true", help="通过 OpenZilo SDK 进行电脑端实时识别")
    parser.add_argument("--address", help="扫描返回的 BLE 地址（macOS 使用 UUID）")
    source.add_argument("--input", help="输入文件 (.json 或无表头 .csv)，离线模式")
    parser.add_argument("--sample-rate", type=float, default=25.0, help="采样率")
    parser.add_argument("--cutoff-hz", type=float, default=10.0, help="低通截止频率")
    parser.add_argument("--window-size", type=int, default=8, help="特征窗口")
    parser.add_argument("--window-overlap", type=int, default=4, help="窗口重叠")
    args = parser.parse_args()

    model_dir = Path(args.models)
    if not model_dir.exists():
        print(f"错误: 模型目录不存在 {model_dir}")
        sys.exit(1)

    if args.ring:
        if not args.address:
            print("错误: 使用 --ring 模式需要指定 --address")
            sys.exit(1)
        try:
            asyncio.run(run_realtime(model_dir, args.address, args.sample_rate, args.cutoff_hz,
                                     args.window_size, args.window_overlap))
        except KeyboardInterrupt:
            pass
        except (sdk.OpenZiloError, ValueError, OSError) as exc:
            parser.exit(1, f"Error: {exc}\n")
    elif args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"错误: 输入文件不存在 {input_path}")
            sys.exit(1)
        try:
            run_offline(model_dir, input_path, args.sample_rate, args.cutoff_hz,
                        args.window_size, args.window_overlap)
        except (ValueError, OSError) as exc:
            parser.exit(1, f"Error: {exc}\n")
    else:
        print("错误: 请指定 --ring (实时识别) 或 --input (离线识别)")
        sys.exit(1)


if __name__ == "__main__":
    main()
