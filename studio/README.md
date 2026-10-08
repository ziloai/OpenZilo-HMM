# HMM Gesture Studio / 手势训练平台

独立于推理包的 Tkinter 桌面应用：扫描/连接 OpenZilo 戒指，显示实时六轴曲线、录制手势、导入 JSON/CSV、训练、导出和试识别，也可接收/下载戒指录音。使用 uv 管理环境与依赖，需要 Python 3.10+、Tkinter、Git 和蓝牙权限；IMU 采集需要戒指处于手势模式，录音模式也可以保持连接。

从仓库根目录安装和启动：

```bash
uv sync --locked --all-packages
uv run --locked --all-packages hmm-gesture-studio
# 或 uv run --locked --package hmm-gesture-studio python -m hmm_gesture_studio
```

工作区共用根目录的 `uv.lock` 和 `.venv/`，默认开发解释器为 Python 3.12，无需手动激活环境。在 `studio/` 目录内也可直接运行 `uv run --locked hmm-gesture-studio`，但默认数据目录相对于当前工作目录。

Linux 可能需要 `sudo apt install python3-tk`；macOS 请使用带 Tk 的 Python，Homebrew Python 需安装匹配版本的 `python-tk`。Tkinter 由 Python/系统提供，不能用 `uv add` 安装；可用 `uv sync --locked --all-packages --python /path/to/python3.12` 选择解释器。

## 使用流程

1. 选择工作目录（默认 `gestures/`），扫描选择戒指或直接输入地址，点击连接。macOS 地址通常是 UUID。BLE 连接状态与 IMU 上报状态分别显示；只有实际收到新的 IMU 数据才允许采集手势，不会自动切换设备模式。
2. 输入手势名称，点击开始录制，做一次动作后停止；重复至少两次，建议五次以上。默认每次至少 12 个采样点。保存时可以确认追加到同名、同采样率数据，未完成的录制在断连时丢弃。
3. 也可导入已有 JSON 或多个无表头 CSV（每个文件一次重复，六列 `ax,ay,az,gx,gy,gz` 原始 int16 值）。CSV 采样率取界面中的采样率设置。旧 JSON 未标注采样率时按 25 Hz 处理。
4. 设置训练采样率，使其匹配数据实际采样率；低通截止频率必须小于采样率一半。点击“训练全部数据”，后台训练不阻塞界面。不同采样率不能混训。
5. 导出 `*.gesture.json`，包含手势名称、HMM 参数、预处理和分段参数。数据或训练配置修改后需重新训练才能导出。
6. 可加载已导出的模型包，连接同采样率戒指，开启实时试识别。先静止建立基线，每次手势后恢复静止。置信度不是概率，单一模型不提供可靠的未知手势拒识。

## 实时曲线与模式切换

- “实时六轴曲线”页显示加速度 `ax/ay/az` 和陀螺仪 `gx/gy/gz` 两组曲线，自动缩放，默认显示最近 10 秒。横轴按采样率估算，不是设备时间戳；所有值均为原始 int16。
- “暂停显示”只冻结图形，不停止收数或录制；“恢复显示”回到最新数据。“清空”清除图形缓存，不删除训练数据。
- 从手势模式切到录音模式时，IMU 无数据或 `DEVICE_BUSY` 不再被当成 BLE 断连。界面显示等待状态，取消未完成的手势，保留已完成的重复。
- 切回手势模式后自动重新启用上报，收到新的数据后刷新数值与曲线。也可点击“重试 IMU 上报”；只有开始上报的 ACK 并不算恢复成功。
- 如果 BLE 本身断开，Studio 会退避重连；点击“断开 / 取消连接”或退出可终止重连。协议错误等异常仍会明确报告。
- **开启录音接收/下载会暂停 IMU**。结束音频操作后再回到手势模式；若自动接收仍开启，请先点击“停止接收 / 下载”。

## 戒指录音

当前 SDK 没有电脑端开始/停止录音或设置设备模式的命令。“开启自动接收”不是打开电脑麦克风，也不是远程启动戒指录音。

1. 连接戒指，进入“戒指录音”页，选择保存目录（默认 `audio/`）。
2. 点击“开启自动接收”，将戒指切到录音模式，**长按戒指按键录音，松开后推送文件**。接收完成会保存文件；可连续录制多段。
3. 进度显示的是文件传输字节数，不是正在录音的时长。只有 WAV 解码完成后才显示可验证的音频时长。
4. 先保存完整的原始 `.bin`，再尝试生成 `.wav`。WAV 解码依赖系统 `ffmpeg`（macOS 可 `brew install ffmpeg`，Ubuntu/Debian 可 `sudo apt install ffmpeg`，Windows 请将 ffmpeg 加入 PATH）。未安装或解码失败仍保留 `.bin`，日志说明原因。
5. 可打开已保存文件播放，或打开录音目录。重复下载同一录音使用新文件名，不覆盖旧文件，不删除戒指中的原文件。
6. 漏收、模式切换或断连导致传输不完整时，先停止接收，点击“查询戒指历史录音”，选择索引并下载。停止接收/下载不能停止戒指硬件录音；未传输完整的文件不会假装保存成功。

请在目标戒指上验证模式切换、长按/松开推送、下载与重连行为；自动测试只模拟 SDK，不代表全部固件已实测。

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
