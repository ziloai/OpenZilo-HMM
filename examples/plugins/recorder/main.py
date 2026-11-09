"""Host-owned recording; local WAV metadata and hash, no network or STT."""
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4
import wave

PLUGIN_ID = "example.recorder"


def _state_path(context):
    directory = Path(context.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "session.json"


def _read_session(context):
    path = _state_path(context)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("session_id")


def _write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def handle_action(context, action, event, parameters):
    if action == "status":
        return context.recording.status()
    if action == "start":
        if event.get("type") == "recording.completed":
            raise ValueError("完成事件不能启动下一次录音")
        seconds = parameters.get("max_seconds", 60)
        live = parameters.get("live", False)
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 1 <= seconds <= 60
                or not float(seconds).is_integer()):
            raise ValueError("录音时长必须为 1–60 的整数秒")
        if not isinstance(live, bool):
            raise ValueError("实时预览必须为布尔值")
        # The host is authoritative for busy/ownership/OTA checks. A stale local
        # session after a crash must not prevent starting once the host is idle.
        result = context.recording.start(max_seconds=seconds, live=live)
        session = result.get("session_id")
        if not isinstance(session, str) or not session:
            raise RuntimeError("宿主未返回有效 session_id")
        _write_json(_state_path(context), {"session_id": session})
        context.log("已申请录音；完整保存后才会生成分析结果")
        return result
    if action == "stop":
        session = _read_session(context)
        if not session:
            raise ValueError("没有本插件记录的 session_id；请查询宿主状态")
        result = context.recording.stop(session)
        # Retain the ID until completion; stop acknowledgement is NOT a file.
        context.log("已请求停止；请等待完整保存，不将停止响应视为完成")
        return result
    raise ValueError("未知录音动作")


def handle_event(context, event):
    if event.get("type") != "recording.completed":
        return {"ignored": True}
    # The host filters subscription scope and authorizes manual WAV selection.
    # Other owners/manual events must not clear our outstanding recording ID.
    session = event.get("session_id")
    if (event.get("owner") == PLUGIN_ID and isinstance(session, str) and session
            and _read_session(context) == session):
        _state_path(context).unlink(missing_ok=True)
    if event.get("raw_only"):
        context.log("跳过本地 WAV 分析：完整 raw 已保存，但尚未解码为 WAV")
        return {"ignored": True, "reason": "raw_only"}
    if context.settings.get("analyze_completed", True) is not True:
        return {"ignored": True}
    # Only the complete saved WAV path is used. Never treat raw_path as a
    # complete WAV fallback, and never rename/modify the host's audio files.
    saved_path = event.get("path")
    if not isinstance(saved_path, str) or not saved_path:
        raise ValueError("完成事件缺少已保存 WAV 路径")
    audio = Path(saved_path)
    with audio.open("rb") as stream:
        with wave.open(stream, "rb") as wav:
            metadata = {
                "channels": wav.getnchannels(), "sample_width_bytes": wav.getsampwidth(),
                "sample_rate_hz": wav.getframerate(), "frames": wav.getnframes(),
                "compression": wav.getcomptype(),
                "duration_s": wav.getnframes() / wav.getframerate(),
            }
            # Validate that data really contains the frames described by the
            # WAV header; a truncated file must not get a successful result.
            remaining = wav.getnframes()
            frame_bytes = wav.getnchannels() * wav.getsampwidth()
            while remaining:
                chunk = wav.readframes(min(remaining, 65536))
                if not chunk or len(chunk) % frame_bytes:
                    raise ValueError("WAV 音频数据不完整")
                remaining -= len(chunk) // frame_bytes
        stream.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    result = {
        "session_id": session, "owner": event.get("owner"), "path": str(audio),
        "raw_path": event.get("raw_path"), "event_duration_s": event.get("duration_s"),
        "wave": metadata, "sha256": digest.hexdigest(),
    }
    # Manual WAV selection has no session. Hash IDs rather than using external
    # input as path components; UUID fallback also permits repeat manual runs.
    identity = session if isinstance(session, str) and session else event.get("id")
    if not isinstance(identity, str) or not identity:
        identity = uuid4().hex
    name = "recording-" + hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json"
    directory = Path(context.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    _write_json(destination, result)
    context.log("已在本地保存 WAV 元数据和 SHA-256；未转写、未上传")
    return {"result_path": str(destination), "sha256": result["sha256"]}
