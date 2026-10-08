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
    ├──── plotting.py                 recognizer.py
    └──── device.py                        └── segmentation.py
           ├── audio.py
           └── OpenZilo SDK

legacy CLI ── ring_stream.py ── OpenZilo SDK
```

Studio 的 `device.py` 把 BLE 连接与 IMU 就绪状态分开。只有收到新 IMU 批次才报告可采集；停报或 DEVICE_BUSY 保留 BLE，等待重试，实际断连才退避重连。数据间断会通知 GUI 取消未完成手势并重置分段。原 `ring_stream.py` 继续服务旧命令行，不改变其一次性采集语义。

自动音频监听是被动订阅，没有音频帧时不占用 IMU 消费者；检测到音频队列有帧后，由同一业务消费者串行暂停 IMU、接收一个完整文件，再恢复探测。列表查询、下载仍串行处理，SDK 请求/分片写入额外加锁；取消不会中断半条发送。录音由戒指长按/松开触发，Studio 无远程录音控制接口。`audio.py` 先保存完整原始文件，再用有时限的外部 ffmpeg 转 WAV；解码失败不丢原数据。`plotting.py` 只在 Tk 主线程绘制有界滚动数据及识别片段框，录制预览复用训练的 `SignalFilter` 并保留浮点输出，不修改原始录制。`device_history.py` 以原子 JSON 写入本机用户配置目录，仅保存成功连接的最近 20 台设备，不依赖工作目录。不新增运行时依赖，也不影响训练/推理包边界。详情见 [Studio 指南](../studio/README.md)。

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
- `predict`：连续模式对整段分类；短促模式调用与训练相同的 `prepare_recording`，要求恰好一个完整事件。过短、无完整窗口、多事件或未通过拒识/置信度检查时返回 `None`。
- `feed`：维护分段状态，返回所有通过检查的 `Prediction`，分批大小不改变窗口与位置。连续模式以尾部静止结束动作；短促模式按原始相邻加速度差触发，收齐固定 post-roll 后结束。**单类且无 `rejection` 标定时，非空 `feed()` 抛出 `ValueError`**，不再把所有候选都接受成该类。
- `reset`：清除片段、冷却、基线和样本位置计数，适合断连重连或切换设备后调用。
- `sample_rate_hz` 可选，但建议每次传入真实来源的采样率。省略表示调用者保证采样率匹配；包不会从数据自动估计或重采样。
- 同一识别器有状态，不可由多个线程或不同设备同时使用。

`Prediction` 字段：

| 字段 | 含义 |
| --- | --- |
| `name` | 训练数据中的原始名称，不是净化后的文件名 |
| `confidence` | 多模型的分数差经验指标，范围 0–1；单模型为 `None`，没有可比较的次优类 |
| `score` | 最优 HMM 的每特征帧平均 log-likelihood |
| `scores` | 各模型的每帧平均 log-likelihood 字典 |
| `start_sample` / `end_sample` | `feed()` 实际分类片段的半开区间 `[start, end)`，从最近一次 `reset()` 后第 0 帧计数。含 pre-roll；连续模式不含结束静止帧，短促模式含固定 post-roll。`predict()` 为 `None` |

多模型置信度仍为 `1 - exp(-(best - second) / 10)`，不是校准概率。单类为 `None`；若要求 `min_confidence > 0`，单类结果不能满足此阈值。不要对 `None` 使用浮点格式化。

带拒识标定时，先选出得分最高类，再检查其每特征帧分数下界、原始相邻加速度差峰值下界及样本长度范围；未通过就返回 `None`，不会改选其他类。仅由正样本推得的接受区域仍可能接收未知动作，不能代替负样本/独立测试；无标定的多类旧模型仍只做相对排名。

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
    calibrate_rejection=True,
)
bundle.save("exports/my-gestures.gesture.json")
```

训练前检查重复名称、采样率和有效重复数。兼容 API 默认 `calibrate_rejection=False`，每类至少两次；上例及 GUI 开启拒识标定时每类至少三次。最短长度为 `2 * window_size - window_overlap`，短录制不会静默算成有效。短促模式以 `prepare_recording` 统一裁剪后拟合及标定，无完整窗口或多个事件则报错，原 JSON 不改写。任何一类失败不返回部分模型包。训练分数仅作诊断，不是测试集准确率。

拒识标定逐次留出同类录制，拟合其他录制后计算该次的每帧分数下界；全量模型随后用于推理。它是保守的正样本经验限值，并未使用负样本验证。

每个手势在其训练特征的标准化空间进行 EM，最终每维状态方差至少为标准化单位的 `0.01`。均值/方差随后通过逆仿射变换还原，模型打分包含正确的尺度 Jacobian，不需要给 v1 导出添加 scaler，也不改变旧模型的预处理。常量特征使用单位尺度。新正则化只影响重新训练的模型。

可调用 `evaluate_leave_one_out(datasets, pipeline, n_states=6, segmentation=..., calibrate_rejection=True, progress=print)`（`hmm_gesture_studio.evaluation`），或点击 GUI“留出评估”。开启拒识时每类至少 4 次：外层留出的测试录制不进入内层训练、尺度估计或拒识标定；不开启的兼容模式仍需至少 3 次。每次完整录制恰好测试一次。返回 `correct`、`total`、`accuracy`、`per_class`、`confusion`、`skipped_recordings` 及原录制索引的 `predictions`。单类的数值只是正样本通过率，不是未知动作准确率。评估不替换当前模型；连续流误触和跨会话泛化需另测。

训练数据保留旧 JSON 的 `name`、`sample_rate_hz`、`repetitions[].data` 结构；新数据额外标记 `axes` / `units`。旧文件缺少采样率时按 25 Hz 解释。数据保存校验原始整数范围，不会因强制转为 int16 而静默溢出；写入采用原子替换，并拒绝净化文件名碰撞。GUI 对已有同名数据先询问追加。

## 导出格式 v1 / v2

读取兼容 v1 和 v2；使用稳态滤波、短促模式或拒识参数时写 v2。只使用旧参数且无拒识时仍可写 v1。

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

24 维特征顺序为均值(6)、方差(6)、RMS(6)、过零率(6)。每段先中值滤波（`median_kernel=1` 关闭），再因果 Butterworth 低通；段间重置。

**v2** 保持 HMM 数组及 24 维特征不变，增加并严格校验：

- `pipeline.filter_initialization`：`zero` 或 `steady`。`steady` 使用段首值的滤波稳态，避免从零启动造成假运动；v1 始终按 `zero` 解释，不改变旧模型分数。
- `segmentation.mode`：`motion` 或 `impulse`；`post_roll` 为非负整数，impulse 必须正数。impulse 按原始相邻三轴加速度差的范数对比 `energy_threshold`，保留 `pre_roll + 1 + post_roll` 帧；无需连续多帧超过阈值或回到旧姿态。冷却从触发帧算起。训练和整段 `predict` 要求恰好一个完整窗口，流式窗口不因传入批次拆分而改变。
- 顶层 `rejection` 必须显式存在，可为 `null`（未标定），或覆盖所有手势名称的字典。每类含有限的 `score_floor`、非负 `peak_floor`、正整数 `min_samples`/`max_samples`。部分标签、未知字段、非法数值均拒绝加载；不要手写或用训练集分数冒充留出标定。

`SegmentationConfig.for_sample_rate(rate, mode=..., energy_threshold=..., min_samples=...)` 供 Studio 明确应用时间预设，不从传感器数据猜测采样率；旧 `SegmentationConfig()` 的帧数默认值不变。

加载拒绝未知版本、错误轴序/单位、缺失配置、重复标签、形状错误和非有限参数。不调用 `pickle.load`。文件限制为 32 MiB，写出前也检查实际编码大小；失败不损坏已有文件。格式版本与 Python 包版本独立，后续不兼容格式需要新版本，不能默默按默认值解释。

元数据含训练时间、平台版本、各手势总/有效录制数、实际状态数和训练分数。量程与佩戴方向没有自动校准；使用者仍需保证这些条件与训练相同。

## 旧版本与验证

原 `train_hmm.py` 仍输出 pickle，原 `recognize.py` 仍加载它们。新平台不提供不安全的隐式 pickle 转换：旧 JSON 可以导入并重新训练，中文标签完整保留。`pretrained_models/` 只是旧格式的兼容示例。

测试覆盖导出/加载前后分数一致、无 GUI/SDK 的独立推理、格式错误拒绝、批次拆分一致、GUI 录制边界/追加/过期结果管理，以及后台 BLE 扫描、单消费者、超时、取消、断连清理。真实硬件效果和识别准确率仍需另行评测。
