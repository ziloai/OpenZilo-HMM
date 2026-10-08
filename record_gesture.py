# SPDX-License-Identifier: MPL-2.0
"""手势数据采集脚本 —— 连接 BLE 戒指实时录制 IMU 数据并保存为训练用 JSON。

支持三种模式:
1. BLE 戒指模式: 通过 OpenZilo SDK 连接戒指，用终端回车控制录制（推荐）
2. 文件模式: 从已有 CSV 文件导入（每行 6 列: ax,ay,az,gx,gy,gz）
3. 交互模式: 从标准输入粘贴数据（适合调试）

用法:
    # 连接戒指录制（推荐）
    python record_gesture.py --name snap --ring --address AA:BB:CC:DD:EE:FF --reps 5

    # 从 CSV 文件导入
    python record_gesture.py --name 打响指 --from-csv snap1.csv snap2.csv snap3.csv

    # 交互式终端录制
    python record_gesture.py --name 打响指 --interactive --reps 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from pathlib import Path

import numpy as np
import openzilo as sdk

from ring_stream import open_ring_stream
from hmm_gesture.preprocessing import validate_samples
from hmm_gesture_studio.datasets import GestureDataset, save_dataset

# Two feature windows with the default window_size=8, overlap=4.
MIN_RECORDING_SAMPLES = 12


def load_csv(path: Path) -> np.ndarray:
    """读取 CSV 文件，每行 6 个整数（加速度 xyz + 陀螺仪 xyz）。"""
    data = validate_samples(np.loadtxt(path, delimiter=",", ndmin=2))
    if not np.equal(data, np.rint(data)).all():
        raise ValueError(f"{path}: CSV must contain raw integer IMU values")
    return data.astype(np.int16)


def read_interactive_rep(rep_index: int) -> np.ndarray:
    """从标准输入读取一次重复的 IMU 数据，空行结束。"""
    print(f"\n--- 第 {rep_index + 1} 次录制 ---")
    print("请粘贴 IMU 数据（每行: ax,ay,az,gx,gy,gz），空行结束:")
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append([int(x) for x in line.strip().split(",")])
    if not lines:
        raise ValueError("没有输入数据")
    return np.array(lines, dtype=np.int16)


async def _prompt_while_receiving(prompt: str, receiver: asyncio.Task[None]) -> str:
    """Wait for terminal input without blocking BLE, and surface stream errors.

    A daemon thread is intentional: cancelling a to_thread(input) call cannot
    interrupt input(), and asyncio.run would wait for that executor at exit.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str] = loop.create_future()

    def finish(value: str, error: Exception | None) -> None:
        if not answer.done():
            if error is not None:
                answer.set_exception(error)
            else:
                answer.set_result(value)

    def read_input() -> None:
        value, error = "", None
        try:
            value = input(prompt)
        except (EOFError, OSError) as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(finish, value, error)
        except RuntimeError:
            pass  # The event loop has already closed after cancellation.

    threading.Thread(target=read_input, daemon=True).start()
    try:
        done, _ = await asyncio.wait({answer, receiver}, return_when=asyncio.FIRST_COMPLETED)
        if receiver in done:
            receiver.result()
            raise RuntimeError("IMU receiver stopped unexpectedly")
        return answer.result()
    finally:
        if answer.done() and not answer.cancelled():
            answer.exception()  # Retrieve a simultaneous input error if the receiver failed.
        else:
            answer.cancel()


async def record_from_ring(address: str, name: str, reps: int, output_dir: Path) -> Path:
    """Record raw six-axis batches with the official OpenZilo SDK.

    One consumer runs throughout the session, discarding samples between takes
    so terminal prompts do not block BLE or accumulate stale idle data.
    """
    if reps < 2:
        raise ValueError("At least two repetitions are required")

    repetitions: list[np.ndarray] = []
    async with open_ring_stream(address) as (ring, start_info):
        buffer: list[list[int]] | None = None

        async def collect_data() -> None:
            while True:
                try:
                    batch = await sdk.wait_sensor_data(ring, timeout_s=5.0)
                except sdk.TimeoutError:
                    print("\nNo IMU data for 5 s. Check the connection and gesture mode.")
                    continue
                if buffer is not None:
                    buffer.extend([
                        s.accel_x, s.accel_y, s.accel_z,
                        s.gyro_x, s.gyro_y, s.gyro_z,
                    ] for s in batch.samples)

        receiver = asyncio.create_task(collect_data())
        try:
            print(f"\nGesture: {name}; repetitions: {reps}")
            print("Press Enter to start, perform the gesture, then Enter to stop.\n")
            while len(repetitions) < reps:
                await _prompt_while_receiving(
                    f"--- Take {len(repetitions) + 1}/{reps}: Enter to start ---", receiver
                )
                buffer = []
                await _prompt_while_receiving("  Recording... Enter to stop ", receiver)
                captured, buffer = buffer, None
                if len(captured) < MIN_RECORDING_SAMPLES:
                    print(f"  Too short ({len(captured)} samples; need at least "
                          f"{MIN_RECORDING_SAMPLES}). Retrying the same take.")
                    continue
                repetitions.append(np.array(captured, dtype=np.int16))
                print(f"  Recorded: {len(captured)} samples "
                      f"({len(captured) / start_info.sample_rate_hz:.1f} s)")
        finally:
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass

    return save_gesture(name, repetitions, output_dir,
                        sample_rate_hz=start_info.sample_rate_hz)


def save_gesture(name: str, repetitions: list[np.ndarray], output_dir: Path,
                 sample_rate_hz: float = 25) -> Path:
    """保存手势数据，BLE 模式使用设备返回的实际采样率。"""
    return save_dataset(GestureDataset(name, repetitions, sample_rate_hz), output_dir)


def main():
    parser = argparse.ArgumentParser(description="HMM 手势数据采集")
    parser.add_argument("--name", required=True, help="手势名称")
    parser.add_argument("--output", default="gestures", help="输出目录 (默认: gestures/)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ring", action="store_true", help="通过 OpenZilo SDK 连接 BLE 戒指录制")
    parser.add_argument("--address", help="扫描返回的 BLE 地址（macOS 使用 UUID）")
    source.add_argument("--from-csv", nargs="+", metavar="FILE", help="从无表头 CSV 导入多次重复")
    source.add_argument("--interactive", action="store_true", help="交互式从终端输入")
    parser.add_argument("--reps", type=int, default=5, help="录制重复次数 (默认 5，至少 2)")
    parser.add_argument("--sample-rate", type=float, default=25.0,
                        help="CSV/终端数据采样率 Hz；BLE 模式始终使用设备返回值")
    args = parser.parse_args()
    if (args.ring or args.interactive) and args.reps < 2:
        parser.error("--reps must be at least 2")
    if not np.isfinite(args.sample_rate) or args.sample_rate <= 0:
        parser.error("--sample-rate must be finite and positive")

    output_dir = Path(args.output)
    repetitions: list[np.ndarray] = []

    if args.ring:
        if not args.address:
            print("错误: 使用 --ring 模式需要指定 --address")
            sys.exit(1)
        try:
            path = asyncio.run(record_from_ring(args.address, args.name, args.reps, output_dir))
        except KeyboardInterrupt:
            print("\nRecording cancelled; no dataset saved.")
            return
        except (sdk.OpenZiloError, ValueError, EOFError, OSError) as exc:
            parser.exit(1, f"Error: {exc}\n")
        print(f"\n保存成功: {path}")
        return

    elif args.from_csv:
        for csv_path in args.from_csv:
            p = Path(csv_path)
            if not p.exists():
                print(f"错误: 文件不存在 {p}")
                sys.exit(1)
            rep = load_csv(p)
            repetitions.append(rep)
            print(f"  已加载: {p.name} ({len(rep)} 帧)")

    elif args.interactive:
        print(f"手势名称: {args.name}")
        print(f"需要录制 {args.reps} 次重复")
        for i in range(args.reps):
            rep = read_interactive_rep(i)
            repetitions.append(rep)
            print(f"  已录制: {len(rep)} 帧")

    else:
        print("错误: 请指定 --ring, --from-csv 或 --interactive")
        sys.exit(1)

    if len(repetitions) < 2:
        print(f"错误: 至少需要 2 次重复，当前仅有 {len(repetitions)} 次")
        sys.exit(1)

    path = save_gesture(args.name, repetitions, output_dir, sample_rate_hz=args.sample_rate)
    print(f"\n保存成功: {path} ({len(repetitions)} 次重复)")


if __name__ == "__main__":
    main()
