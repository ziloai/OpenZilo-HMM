# 两部分架构 / Two-component architecture

[中文入门](../README.zh-CN.md) · [English quick start](../README.md)

## 包边界

- 根目录 `pyproject.toml` 构建 `hmm-gesture`，只打包 `src/hmm_gesture/`。生产应用仅安装这个包即可。
- `studio/pyproject.toml` 构建 `hmm-gesture-studio`，只打包 `studio/src/hmm_gesture_studio/`，单向依赖同版本的识别包及固定提交的官方 SDK。
- GUI 使用 Python 自带的 Tkinter。它不是识别包的依赖；导入 `hmm_gesture` 不加载 Tk、OpenZilo、Bleak 或训练平台。
- `SignalFilter` / `FeatureExtractor` 只有一个实现，训练与推理共用。训练算法放在 Studio；原脚本保留兼容入口。
- GUI 只在主线程操作 Tk。BLE 拥有独立线程与 asyncio loop，训练在单独工作线程执行，使用队列把状态交回 GUI。

```text
hmm_gesture_studio                     hmm_gesture
  gui.py ─── datasets.py                 config.py
    │          │                            │
    ├──── training.py ────────────────> preprocessing.py
    │          └─────────────────────> bundle.py
    │                                       │
    └──── device.py                    recognizer.py
           └── ring_stream.py              └── segmentation.py
                 └── OpenZilo SDK
```

## uv 工作区与依赖管理

根项目与 `studio/` 是同一个 uv workspace，使用根目录唯一的 `uv.lock` 和 `.venv/`。Studio 通过 `tool.uv.sources` 中的 `workspace = true` 引用本地识别包，开发时不会误装注册表中的同名包。构建出的 wheel 仍是两个独立发行包，不含机器相关的本地路径。

在仓库根目录运行：

```bash
uv sync --locked --all-packages                   # 安装训练平台 + 识别包
uv run --locked --all-packages hmm-gesture-studio  # 启动 GUI
uv run --locked --all-packages python -m unittest discover -s tests -v
uv build --all-packages --wheel                   # 两个独立 wheel -> dist/
```

仅需要识别包时用 `uv sync --locked`，不带 `--all-packages`；这会移除共用环境中的平台及 BLE 依赖。下次使用平台时，带 `--all-packages` 或 `--package hmm-gesture-studio` 的 `uv run` 会重新同步所需依赖。

- `.python-version` 默认选择开发用 Python 3.12；包元数据仍支持 Python 3.10+。CI 通过 `--python` 显式覆盖，测试不同版本。
- `uv add 包名` 修改识别包依赖；`uv add --package hmm-gesture-studio 包名` 修改平台依赖。
- 手动修改 `pyproject.toml` 后运行 `uv lock`；依赖升级使用 `uv lock --upgrade-package 包名`。依赖声明与锁文件需一并提交，再同步环境、运行测试。
- 日常启动与 CI 使用 `--locked`，依赖声明过期时直接报错，不默默改变锁定版本；不要手工编辑 `uv.lock`。
- Tkinter 属于 Python/系统环境，不是 uv 包依赖。选择带 Tk 的 Python 后即可运行 GUI；纯推理无需 Tk。

## 公开识别 API

```python
from hmm_gesture import GestureRecognizer

r = GestureRecognizer.load("gestures.gesture.json", min_confidence=0.0)
# r.gesture_names: tuple[str, ...]
# r.sample_rate_hz: float
prediction = r.predict(samples, sample_rate_hz=25)
results = r.feed(batch, sample_rate_hz=25)
r.reset()
```

- 输入：按 `ax,ay,az,gx,gy,gz` 排列的二维数值序列，`shape=(N,6)`，使用原始 int16 传感器单位；形状、有限值、范围会验证。
- `predict`：将整段数据视为一次手势，不做运动分段；不足两个窗口或低于置信度阈值返回 `None`。
- `feed`：维护运动分段状态，返回 `list[Prediction]`；返回空列表或同批次内的所有结果。分批大小不会丢掉已完成动作。先喂静止数据建立基线，尾部静止用于结束动作。
- `reset`：清除片段、冷却和基线，适合断连重连或切换设备后调用。
- `sample_rate_hz` 可选，但建议每次传入真实来源的采样率。省略表示调用者保证采样率匹配；包不会从数据自动估计或重采样。
- 同一识别器有状态，不可由多个线程或不同设备同时使用。

`Prediction` 字段：

| 字段 | 含义 |
| --- | --- |
| `name` | 训练数据中的原始名称，不是净化后的文件名 |
| `confidence` | 分数差经验指标，范围 0–1；单模型固定 0.8 |
| `score` | 最优 HMM 的每特征帧平均 log-likelihood |
| `scores` | 各模型的每帧平均 log-likelihood 字典 |

模型选择与置信度使用整段总分：`1 - exp(-(best - second) / 10)`。这不是校准概率，不提供可靠的未知动作拒识。静止或未见过的动作，若直接调用 `predict`，仍可能被归入某个类别。

## 训练 API（仅平台需要）

```python
from pathlib import Path
from hmm_gesture import PipelineConfig, SegmentationConfig
from hmm_gesture_studio.datasets import load_dataset
from hmm_gesture_studio.training import train_datasets

datasets = [load_dataset(p) for p in sorted(Path("gestures").glob("*.json"))]
bundle = train_datasets(
    datasets,
    PipelineConfig(sample_rate_hz=25, cutoff_hz=10, window_size=8, window_overlap=4),
    n_states=6,
    segmentation=SegmentationConfig(energy_threshold=1500),
    progress=print,
)
bundle.save("exports/my-gestures.gesture.json")
```

训练前检查重复名称、采样率和有效重复数。每类至少两次有效录制，最短长度为 `2 * window_size - window_overlap`。短录制不计入训练，日志与导出统计给出使用数。任何一类失败则不返回部分模型包。训练分数仅作诊断，不是测试集准确率。

训练数据保留旧 JSON 的 `name`、`sample_rate_hz`、`repetitions[].data` 结构；新数据额外标记 `axes` / `units`。旧文件缺少采样率时按 25 Hz 解释。数据保存校验原始整数范围，不会因强制转为 int16 而静默溢出；写入采用原子替换，并拒绝净化文件名碰撞。GUI 对已有同名数据先询问追加。

## 导出格式 v1

扩展名建议为 `.gesture.json`；加载依据内容而不是扩展名。一个文件包含多个手势，不包含原始训练录制，不需要附带数据目录或 pickle 文件。

```json
{
  "format": "hmm-gesture",
  "version": 1,
  "axes": ["ax", "ay", "az", "gx", "gy", "gz"],
  "units": "raw_int16",
  "pipeline": {
    "sample_rate_hz": 25.0,
    "cutoff_hz": 10.0,
    "filter_order": 2,
    "median_kernel": 5,
    "window_size": 8,
    "window_overlap": 4
  },
  "segmentation": {
    "energy_threshold": 1500.0,
    "min_onset_frames": 3,
    "min_offset_frames": 5,
    "min_gesture_len": 12,
    "max_gesture_len": 125,
    "pre_roll": 3,
    "cooldown_frames": 15
  },
  "gestures": [
    {"name": "挥手", "model": {"type": "gaussian-hmm", "covariance_type": "diag"}}
  ],
  "metadata": {}
}
```

上述示例仅展示结构，`model` 中还**必须**有以下完整数值数组才能加载：

- `startprob`：`(states,)` 初始概率；非负、总和为 1。
- `transmat`：`(states, states)` 转移概率；非负、每行总和为 1。
- `means`：`(states, 24)` 状态均值。
- `covars`：`(states, 24)` 对角方差；全部大于 0。

24 维特征顺序为均值(6)、方差(6)、RMS(6)、过零率(6)，与原工程一致。每段先中值滤波，再因果 Butterworth 低通；滤波状态在段间重置。

加载拒绝未知版本、错误轴序/单位、缺失配置、重复标签、形状错误和非有限参数。不调用 `pickle.load`。文件限制为 32 MiB，写出前也检查实际编码大小；失败不损坏已有文件。格式版本与 Python 包版本独立，后续不兼容格式需要新版本，不能默默按默认值解释。

元数据含训练时间、平台版本、各手势总/有效录制数、实际状态数和训练分数。量程与佩戴方向没有自动校准；使用者仍需保证这些条件与训练相同。

## 旧版本与验证

原 `train_hmm.py` 仍输出 pickle，原 `recognize.py` 仍加载它们。新平台不提供不安全的隐式 pickle 转换：旧 JSON 可以导入并重新训练，中文标签完整保留。`pretrained_models/` 只是旧格式的兼容示例。

测试覆盖导出/加载前后分数一致、无 GUI/SDK 的独立推理、格式错误拒绝、批次拆分一致、GUI 录制边界/追加/过期结果管理，以及后台 BLE 扫描、单消费者、超时、取消、断连清理。真实硬件效果和识别准确率仍需另行评测。
