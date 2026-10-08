# HMM Gesture Studio / 手势训练平台

独立于推理包的 Tkinter 桌面应用：扫描/连接 OpenZilo 戒指，录制六轴手势、导入 JSON/CSV、训练、导出和实时试识别。使用 uv 管理环境与依赖，需要 Python 3.10+、Tkinter、Git；BLE 功能需要蓝牙权限与处于手势模式的戒指。

从仓库根目录安装和启动：

```bash
uv sync --locked --all-packages
uv run --locked --all-packages hmm-gesture-studio
# 或 uv run --locked --package hmm-gesture-studio python -m hmm_gesture_studio
```

工作区共用根目录的 `uv.lock` 和 `.venv/`，默认开发解释器为 Python 3.12，无需手动激活环境。在 `studio/` 目录内也可直接运行 `uv run --locked hmm-gesture-studio`，但默认数据目录相对于当前工作目录。

Linux 可能需要 `sudo apt install python3-tk`；macOS 请使用带 Tk 的 Python，Homebrew Python 需安装匹配版本的 `python-tk`。Tkinter 由 Python/系统提供，不能用 `uv add` 安装；可用 `uv sync --locked --all-packages --python /path/to/python3.12` 选择解释器。

## 使用流程

1. 选择工作目录（默认 `gestures/`），扫描选择戒指或直接输入地址，点击连接。macOS 地址通常是 UUID。连接后显示实际采样率与量程，不会自动切换设备模式。
2. 输入手势名称，点击开始录制，做一次动作后停止；重复至少两次，建议五次以上。默认每次至少 12 个采样点。保存时可以确认追加到同名、同采样率数据，未完成的录制在断连时丢弃。
3. 也可导入已有 JSON 或多个无表头 CSV（每个文件一次重复，六列 `ax,ay,az,gx,gy,gz` 原始 int16 值）。CSV 采样率取界面中的采样率设置。旧 JSON 未标注采样率时按 25 Hz 处理。
4. 设置训练采样率，使其匹配数据实际采样率；低通截止频率必须小于采样率一半。点击“训练全部数据”，后台训练不阻塞界面。不同采样率不能混训。
5. 导出 `*.gesture.json`，包含手势名称、HMM 参数、预处理和分段参数。数据或训练配置修改后需重新训练才能导出。
6. 可加载已导出的模型包，连接同采样率戒指，开启实时试识别。先静止建立基线，每次手势后恢复静止。置信度不是概率，单一模型不提供可靠的未知手势拒识。

## 无 GUI 训练

```bash
uv run --locked --package hmm-gesture-studio hmm-gesture-train --data sample_data --output exports/demo.gesture.json
```

这会训练仓库六类示例数据。只验证流程，不是独立准确率评测。旧 `*.pkl` 不会直接导入；请用录制 JSON 重新训练导出，避免 pickle 的执行风险和缺少预处理参数的问题。

导出后，使用端只需识别包：在仓库根目录用 `uv sync --locked` 仅同步 `hmm-gesture`，或在其他 uv 应用中用 `uv add /path/to/OpenZilo-HMM` 添加依赖。无需此平台、Tkinter 或 OpenZilo SDK：

```python
from hmm_gesture import GestureRecognizer
recognizer = GestureRecognizer.load("exports/demo.gesture.json")
result = recognizer.predict(samples, sample_rate_hz=25)  # (N, 6) 原始值
for result in recognizer.feed(batch, sample_rate_hz=25):
    print(result.name, result.confidence)
```

项目采用 MPL-2.0。SDK 固定为 OpenZilo 0.5.0 的提交 `e9a861dd5c82a154fba0f1bafb628cd04fbd9555`。本平台不向戒指上传模型；训练与推理均在电脑端运行。
