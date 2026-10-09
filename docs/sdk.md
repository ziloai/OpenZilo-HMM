# OpenZilo SDK integration / SDK 接入说明

[English README](../README.md) · [中文 README](../README.zh-CN.md)

## English

### Dependency and source of truth

This project uses the official [`OpenZilo`](https://github.com/ziloai/OpenZilo) distribution, imported as `openzilo`. `studio/pyproject.toml` pins SDK **0.5.0** to commit [`e9a861dd5c82a154fba0f1bafb628cd04fbd9555`](https://github.com/ziloai/OpenZilo/commit/e9a861dd5c82a154fba0f1bafb628cd04fbd9555). Install it with uv, Python 3.10+ and Git (commands run from the workspace root):

```bash
uv sync --locked --all-packages
uv run --locked --all-packages python -c "import openzilo; print(openzilo.__version__)"
uv run --locked --all-packages python -m openzilo scan --timeout 10
```

Only the training platform and legacy ring CLIs depend on the SDK. The standalone `hmm-gesture` package takes application-provided IMU arrays and does not import OpenZilo or BLE. See [the two-component architecture](architecture.md).

BLE is supplied by the SDK's `bleak` dependency. `ffmpeg` is not needed for IMU data, training, or recognition. Studio uses system ffmpeg to decode ring Speex recordings to WAV, preserving raw `.bin` files even when decoding fails.

Use the upstream documents for protocol details rather than keeping another copy here:

- [SDK API guide at the pinned revision](https://github.com/ziloai/OpenZilo/blob/e9a861dd5c82a154fba0f1bafb628cd04fbd9555/docs/python-sdk.zh-CN.md)
- [Protocol v4](https://github.com/ziloai/OpenZilo/blob/e9a861dd5c82a154fba0f1bafb628cd04fbd9555/docs/protocol.zh-CN.md)
- [Architecture and firmware behavior](https://github.com/ziloai/OpenZilo/blob/e9a861dd5c82a154fba0f1bafb628cd04fbd9555/docs/architecture.zh-CN.md)
- [Public exports (`__all__`)](https://github.com/ziloai/OpenZilo/blob/e9a861dd5c82a154fba0f1bafb628cd04fbd9555/src/openzilo.py)

The upstream behavior baseline is firmware `V2.000.0001.0015`; verify other firmware on hardware.

### Migrating from the bundled SDK

| Previous integration | Current integration |
| --- | --- |
| Bundled `ring_sdk/ring_sound.py` (0.4.1) | Installed official OpenZilo SDK (0.5.0) |
| `sys.path.insert(..., "ring_sdk")` | No path manipulation |
| `import ring_sound as sdk` | `import openzilo as sdk` |
| `sdk.RingSoundClient(...)` | `sdk.OpenZiloClient(...)` |
| `sdk.RingSoundError` | `sdk.OpenZiloError` |
| Local SDK/protocol manuals | Version-linked upstream documentation above |

The high-level IMU functions and six-axis fields remain compatible: `get_system_info`, `start_sensor_report`, `wait_sensor_data`, and `stop_sensor_report`. The `sdk.TimeoutError` type is an SDK exception, not Python's built-in `TimeoutError`.

The old bundled SDK and its manuals have been removed. `studio/src/hmm_gesture_studio/ring_stream.py` is application lifecycle code, **not another SDK** (the root module is now a compatibility import). It connects, starts reports, and attempts to stop them before disconnecting; it does not switch modes. Recording keeps a single receiver alive between takes, retries short takes, saves the reported sampling rate, and propagates transport/protocol failures.

Existing JSON datasets and pickle models are not rewritten by this migration. Check sampling rate, raw units, axis order, sensor ranges, and preprocessing settings before reusing a model. Pickle compatibility also depends on the Python libraries; retrain when needed.

### IMU usage

The example below applies to both language versions. Replace the address with the scanned identifier (a UUID on macOS) and put the ring in gesture mode first.

```python
import asyncio
import openzilo as sdk


async def main() -> None:
    async with sdk.OpenZiloClient(address="AA:BB:CC:DD:EE:FF") as ring:
        info = await sdk.get_system_info(ring)
        print(info.model, info.firmware_version, info.battery_percent)
        start = await sdk.start_sensor_report(ring)
        try:
            print("Sample rate:", start.sample_rate_hz)
            batch = await sdk.wait_sensor_data(ring, timeout_s=5.0)
            for s in batch.samples:
                print(s.accel_x, s.accel_y, s.accel_z,
                      s.gyro_x, s.gyro_y, s.gyro_z)
        finally:
            if ring.is_connected:
                await sdk.stop_sensor_report(ring)


asyncio.run(main())
```

Important boundaries:

- `start_sensor_report()` enables BLE `0x0605` reports; it does not start the local IMU or set the device mode. In recording mode, the documented firmware returns `DEVICE_BUSY` (2).
- A `0x0704` single-press event contains a timestamp, not a mode or success result. Do not toggle a ring that is already in gesture mode just to satisfy a script.
- `SensorStartInfo` returns sampling rate and accelerometer/gyroscope ranges. The application does not configure these parameters.
- `SensorDataBatch.samples` holds raw signed 16-bit values. The recorder stores axes in `ax, ay, az, gx, gy, gz` order, without timestamps or sequence numbers.
- Only one consumer should read `wait_sensor_data()` on a connection. Keep consuming between takes to avoid stale data accumulating.
- Stop reporting on normal exit or cancellation when still connected. Disconnect/protocol failures should surface, not be swallowed in a retry loop.
- Device-side `wait_sensor_gesture_event()` (`0x0702`) is separate from the host-side HMM pipeline in this repository. No model-upload API is used or provided here.

### Studio mode recovery and recordings

Studio now manages BLE independently of `open_ring_stream`, which is retained for legacy CLIs. `DEVICE_BUSY` or an IMU timeout is not evidence of BLE disconnection. Studio waits/retries reporting while keeping the link, verifies recovery with fresh data rather than an ACK alone, and reconnects with backoff on actual transport loss. Manual disconnect cancels retries. Reissuing START resets sensor sequence numbers, so it is a recovery operation, not a continuous heartbeat.

Studio displays `SystemInfo.battery_percent` and `battery_charging` from the initial connection query, then refreshes via `get_system_info` every 60 seconds with a 2-second SDK response timeout. This session-owned task shares the client's request/write locks, does not stop or consume IMU reports, and defers queries during audio transfers. Optional telemetry failures clear the displayed battery value and warn without terminating the session; disconnect cancels the task and clears the UI.

The SDK/firmware contract is **physical-button recording**: hold the ring button in recording mode, then release to save and push `0x0505` frames. There is no host start/stop recording command. `receive_auto_audio_file` assembles a pushed file; `get_audio_file_count` enumerates indices; `download_audio_file` retrieves missed files. `get_audio_file_info` starts extraction, so it is not used merely to populate a list. Passive automatic listening leaves IMU probing/consumption running. Only when audio frames are queued does the single owner pause IMU to receive a file; it resumes afterwards while keeping the listener armed. Explicit queries/downloads remain exclusive with the automatic listener and pause IMU commands. Stopping reception does not stop the ring microphone; incomplete transfers are not reported as saved and can be downloaded again.

The SDK has no request/write lock. Studio serializes sends and cleans up extraction on cancellation. GUI state distinguishes listening, receiving, saving and disconnected states. Audio conversion uses SDK parsing/container helpers plus a bounded ffmpeg subprocess, off the BLE/Tk threads. Raw files are saved before decoding with unique filenames; durations come from decoded WAV headers. These are application recovery policies; mode transitions and reconnect timing still require hardware validation.

### Updating the dependency later

Review upstream public exports and release changes, change the full commit hash in `studio/pyproject.toml`, and update the SDK version/baseline in both READMEs and this document. Run `uv lock`, commit the updated `uv.lock` alongside the manifest, sync with `uv sync --locked --all-packages`, and run `uv run --locked --all-packages python -m unittest discover -s tests -v`. Then validate scan, recording, recognition, cancellation, and disconnection on a real ring. Do not replace the pinned revision with a moving branch for a release.

This project and the SDK use MPL-2.0; see the root [LICENSE](../LICENSE). The README hero image is copied unchanged from `docs/assets/openzilo-hero.png` at the same upstream revision and is used with OpenZilo's authorization. OpenZilo branding remains with its owner. See [NOTICE.md](../NOTICE.md) for license scope and provenance.

## 中文

### 依赖与文档来源

项目直接依赖官方 [`OpenZilo`](https://github.com/ziloai/OpenZilo) 包，通过 `import openzilo as sdk` 导入。`studio/pyproject.toml` 固定为 SDK **0.5.0**，提交为 **`e9a861dd5c82a154fba0f1bafb628cd04fbd9555`**。使用 uv 管理安装，需要 Python 3.10+ 和 Git，在工作区根目录运行上文命令。

仅训练平台及旧戒指命令行依赖 SDK；独立的 `hmm-gesture` 包只接收应用提供的 IMU 数据，不导入 OpenZilo 或 BLE。见[两部分架构](architecture.md)。

Bleak 由 SDK 自动安装。手势功能不需要 `ffmpeg`；Studio 戒指录音转 WAV 需要系统 `ffmpeg`，缺失或解码失败时仍保留原始 `.bin`。上文链接均指向固定提交的 SDK 手册、协议、架构和公开 API，不在本仓库重复维护一份可能过时的协议文档。上游固件说明基线为 `V2.000.0001.0015`，其他固件需要真机确认。

### 从旧 SDK 迁移

| 旧接入方式 | 当前接入方式 |
| --- | --- |
| 内置 `ring_sdk/ring_sound.py`（0.4.1） | 安装官方 OpenZilo SDK（0.5.0） |
| 修改 `sys.path` 加载 SDK | 不再修改模块搜索路径 |
| `import ring_sound as sdk` | `import openzilo as sdk` |
| `sdk.RingSoundClient(...)` | `sdk.OpenZiloClient(...)` |
| `sdk.RingSoundError` | `sdk.OpenZiloError` |
| 仓库内的旧 SDK/协议手册 | 上文固定版本的官方文档 |

`get_system_info`、`start_sensor_report`、`wait_sensor_data`、`stop_sensor_report` 及六轴数据字段保持兼容。注意 `sdk.TimeoutError` 是 SDK 自己的异常类型，不是 Python 内置的 `TimeoutError`。

旧 SDK 及其手册已移除。`studio/src/hmm_gesture_studio/ring_stream.py` 只管理应用侧的连接、开启上报、退出前停止上报（根目录同名文件现为兼容导入），**不是另一份 SDK**，也不负责切换设备模式。采集器在两次录制之间继续消费数据，短录制会真正重试，保存设备实际采样率，并向调用方传递传输或协议错误。

此次迁移不会改写原有 JSON 数据和 pickle 模型。复用模型前需核对采样率、原始单位、轴顺序、传感器量程和预处理设置；pickle 兼容性也受 Python 依赖版本影响，必要时重新训练。

### 使用边界

上文的最小 IMU 示例可直接使用。先将地址替换为扫描结果（macOS 通常为 UUID），再确保戒指处于手势模式。

- `start_sensor_report()` 只开启 BLE `0x0605` 上报，不启动本地 IMU，也不设置设备模式。文档对应的固件在录音模式下会返回 `DEVICE_BUSY`（2）。
- `0x0704` 单击事件只有时间戳，不包含模式或切换结果。如果戒指已经在手势模式，不要为了让脚本继续而再次切换。
- `SensorStartInfo` 返回采样率及加速度/陀螺仪量程，本项目不会修改这些硬件参数。
- `SensorDataBatch.samples` 是有符号 16 位原始值。采集器按 `ax, ay, az, gx, gy, gz` 顺序保存，不保存时间戳和序号。
- 同一连接只用一个消费者读取 `wait_sensor_data()`；两次录制之间也要持续消费，避免积压旧数据。
- 正常退出或取消时，若仍连接，应停止上报；不要把断连和协议异常当成普通超时无限重试。
- 戒指端 `wait_sensor_gesture_event()`（`0x0702`）与本仓库的电脑端 HMM 是两条独立链路，本项目不使用也不提供模型上传接口。

### Studio 模式恢复与录音

Studio 的 BLE 生命周期已与 IMU 上报解耦；`open_ring_stream` 保留给旧命令行。`DEVICE_BUSY` 或收数超时只说明 IMU 暂不可用，不直接断开 BLE；恢复时重新开启上报并等待新数据。真正的传输断连才退避重连，手动断开停止重试。START 会重置序号，因此不作为连续心跳。单击事件不是模式查询结果。

Studio 显示连接查询返回的 `SystemInfo.battery_percent` / `battery_charging`，之后每 60 秒用 `get_system_info` 刷新，SDK 响应超时为 2 秒。查询任务随 BLE 会话创建/取消，共用请求/写锁，不停止或消费 IMU 上报；音频传输期间暂缓查询。电量查询失败仅清空显示并记录警告，不终止会话；断开时取消查询并清空界面电量。

戒指录音必须在录音模式下**长按物理按键，松开后保存并推送**。SDK 没有电脑端开始/停止录音命令。Studio 用 `receive_auto_audio_file` 接收，用 `get_audio_file_count` 列索引，用 `download_audio_file` 下载历史录音；`get_audio_file_info` 会启动提取，不能单纯用于列表展示。自动监听待机时继续 IMU 探测/收数；检测到音频帧后，同一消费者才暂停 IMU 并接收一个文件，完成后保持监听并恢复 IMU。显式查询/下载与自动监听互斥，操作期间暂停 IMU 请求；停止电脑接收并不能停止戒指麦克风，未完整传输的文件需重新下载。

SDK 自身没有请求/写锁，Studio 负责串行发送、取消时结束提取、断连清理。先以唯一文件名保存原始数据，再在后台用 SDK 的解析/封装功能及限时 ffmpeg 解码；WAV 时长由文件头计算，不以传输用时冒充录音时长。真实模式切换与重连行为仍须真机验证。

### 后续升级

核对上游公开 API 和变更，更新 `studio/pyproject.toml` 的完整提交哈希，同时修改中英文 README 与本文的版本说明。运行 `uv lock`，将更新后的 `uv.lock` 与依赖声明一起提交；用 `uv sync --locked --all-packages` 同步环境，再运行 `uv run --locked --all-packages python -m unittest discover -s tests -v`，然后用真机检查扫描、录制、识别、取消和断连。正式发布时不要改用浮动分支。

本项目与 SDK 均采用 MPL-2.0，见根目录 [LICENSE](../LICENSE)。README 展示图原样取自同一上游提交的 `docs/assets/openzilo-hero.png`，经 OpenZilo 官方授权使用；OpenZilo 品牌标识归权利人所有。许可范围与素材来源见 [NOTICE.md](../NOTICE.md)。
