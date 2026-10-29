# OpenZilo SDK integration / SDK 接入说明

[English README](../README.md) · [中文 README](../README.zh-CN.md)

## Dependency / 依赖

Studio uses official **OpenZilo 0.6.2**, pinned to [`37928a8a65a4ca98be8b8d2be3300ad7f624004e`](https://github.com/ziloai/OpenZilo/commit/37928a8a65a4ca98be8b8d2be3300ad7f624004e) with the `pcm` extra. Only this SDK revision is supported; there is no older-SDK compatibility layer. The SDK supplies Bleak, cryptography and PyAV. `hmm-gesture` remains independent of BLE, SDK, Tk and audio dependencies.

Studio 固定上述提交及 `pcm` extra，只适配新版 SDK。PyAV 用于进程内实时 PCM 解码；签名固件验证使用 SDK 的 cryptography 依赖。独立识别包仍不依赖这些组件。

```bash
uv sync --locked --all-packages
uv run --locked --all-packages python -c "import openzilo; print(openzilo.__version__)"
uv run --locked --all-packages python -m openzilo scan --timeout 10
uv run --locked --all-packages python -m unittest discover -s tests -v
```

Python 3.10+, Git, a Tk-enabled Python and BLE permissions are required for Studio. File decoding uses system ffmpeg; live PCM uses PyAV and does not need the ffmpeg executable. Failed file decoding preserves the original `.bin`. Speex uses the application's five-second decoder bound; Opus uses the SDK's 30-second bound. Both mono and stereo OP2C recordings preserve their channel count.

文件转 WAV 需要系统 ffmpeg，失败保留原始文件；实时 PCM 不需要系统 ffmpeg。旧录音文件格式由新版 SDK 解码，不代表支持旧版 SDK。

## Upstream contracts / 上游契约

Use revision-linked upstream sources instead of copying the protocol implementation:

- [Public API](https://github.com/ziloai/OpenZilo/blob/37928a8a65a4ca98be8b8d2be3300ad7f624004e/src/openzilo/__init__.py)
- [Firmware migration](https://github.com/ziloai/OpenZilo/blob/37928a8a65a4ca98be8b8d2be3300ad7f624004e/docs/sdk-migration.zh-CN.md)
- [IMU and local calibration](https://github.com/ziloai/OpenZilo/blob/37928a8a65a4ca98be8b8d2be3300ad7f624004e/docs/sdk-imu.zh-CN.md)
- [Remote recording](https://github.com/ziloai/OpenZilo/blob/37928a8a65a4ca98be8b8d2be3300ad7f624004e/docs/recording-control.zh-CN.md)
- [Live audio](https://github.com/ziloai/OpenZilo/blob/37928a8a65a4ca98be8b8d2be3300ad7f624004e/docs/sdk-live-audio.zh-CN.md)
- [Signed OTA](https://github.com/ziloai/OpenZilo/blob/37928a8a65a4ca98be8b8d2be3300ad7f624004e/docs/sdk-ota.zh-CN.md)

The enhanced firmware baseline is **V2.001.0002.0001**. Query capabilities on every connection; a version label alone does not prove a feature is available. Unsupported/unknown contracts are rejected rather than force-enabled. SDK version and firmware version are independent.

新版固件取消录音/手势模式切换：订阅启动 IMU，停止订阅或断连停止采集，重连必须重新订阅。Studio 和戒指 CLI 连接后先查询设备能力。只有成功 ACK 后的新数据才能让手势采集就绪；单击事件不是模式查询结果。原始采样率与量程取 `SensorStartInfo`，数据仍以 `ax, ay, az, gx, gy, gz` 原始整数保存。

```python
import asyncio
import openzilo as sdk

async def main():
    async with sdk.OpenZiloClient(address="YOUR_DEVICE_ADDRESS") as ring:
        await sdk.get_device_capabilities(ring)
        start = await sdk.start_sensor_report(ring)
        try:
            batch = await sdk.wait_sensor_data(ring, timeout_s=5)
            print(start.sample_rate_hz, batch.samples)
        finally:
            if ring.is_connected:
                await sdk.stop_sensor_report(ring)

asyncio.run(main())
```

## Ownership and recovery / 独占与恢复

Studio has one BLE owner thread. SDK 0.6.2 owns fragmented-write and per-response request locks; Studio must not reacquire the same write lock around `super().send_command()`. The thin client observer tracks extraction indices using network byte order and completes in-flight writes before cancelling. Studio still uses the pinned client's private receive queues for bounded queue housekeeping; only a few wire IDs are local constants, not private SDK imports or a copied protocol implementation.

The IMU owner pauses before audio operations and maintenance, then restarts with a fresh segment. Calibration owns its own IMU stream and never emits model-input `samples`. OTA owns the entire connection, including reboot/reconnect; normal housekeeping must not interpret its expected reboot as link failure. In-flight battery requests finish before maintenance starts; new telemetry is deferred. An OTA attempt ends the Studio session without automatic retry; reconnect explicitly after checking the result.

自动监听同时识别 `0x050A` 保存结果与音频帧；新版通知通过 SDK 下载文件，不把保存通知当作音频数据。监听待机不阻塞 IMU；实际接收、查询、下载与 IMU 串行。中断传输不能报告为完整录音，稍后按索引补下载。原始文件先保存，转换在后台线程执行，文件名本机生成且不覆盖现有文件。

## New device tools / 新设备工具

- **Remote recording:** read settings/status; start FLASH recording with channel count, bitrate, Q8 digital gains and maximum duration. Stop requests are followed by terminal-status confirmation before download. A lost START acknowledgement is resolved by status, never a blind second START.
- **Live PCM:** subscribe before starting LIVE_AND_FLASH recording. Keep a device copy, use STOP on disconnect, accept only validated complete END before saving a WAV. No speaker monitoring or speech recognition. Partial data is not presented as a successful recording.
- **Local calibration:** accel/quick/full step-by-step collection, independent validation, then atomic profile replacement. Cancellation/validation failure preserves the previous profile. Profiles bind device identity, model, exact firmware and ranges. Mismatches require explicit reset; no silent migration. Calibrated diagnostic values are g and deg/s, separate from raw gesture training/recognition.
- **OTA:** select a signed APP ZIP and an independently obtained trusted P-256 PEM public key. Inspect offline, display signed metadata, confirm again, then use the immutable inspected bytes. Test bundles are rejected. While upgrading, other device actions and ordinary exit/disconnect are blocked. Transfer 100% is not success: the SDK must reconnect and verify the running version. Failed/unconfirmed updates are never automatically retried.

远程录音、实时 PCM、本机校准及 OTA 的具体操作见 [Studio 使用说明](../studio/README.md)。校准不会改写戒指参数或已有手势数据，不会把 g/°/s 输入原始计数模型。固件升级后原校准可能失效，需要明确重置后重新校准。可信公钥不得来自待验固件包本身；本项目不替用户建立信任锚。

## Verification and upgrades / 验证与后续升级

Automated tests use SDK types and simulated transports. They do not prove BLE behavior, microphone quality, calibration accuracy or installation on a physical ring. Before release, manually check capability discovery, fresh IMU/reconnection, remote stop, live END and disconnect handling, mono/stereo file decoding, calibration cancellation/validation, invalid signatures and OTA reboot/version verification on the intended hardware. Never flash as part of unattended software tests.

升级时更新 `studio/pyproject.toml` 的完整提交、`uv.lock` 和文档；运行 `uv lock`、`uv sync --locked --all-packages`、全量测试及 `uv build --all-packages --wheel`。不要将发布依赖改为浮动分支。资产来源独立记录在 [NOTICE.md](../NOTICE.md)，SDK 升级不改变现有展示图的来源提交。
