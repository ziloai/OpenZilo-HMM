"""Send OS media keys without importing the platform backend at discovery."""
import math
import sys
import time

MEDIA_KEYS = {
    "play_pause": "media_play_pause", "next": "media_next",
    "previous": "media_previous", "volume_up": "media_volume_up",
    "volume_down": "media_volume_down", "mute": "media_volume_mute",
}


def _keyboard():
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError("仅支持 macOS / Windows；Linux 不支持媒体示例")
    try:
        from pynput.keyboard import Controller, Key
    except Exception:
        raise RuntimeError("媒体后端不可用；请安装声明依赖，macOS 请检查辅助功能权限") from None
    return Controller(), Key


def handle_action(context, action, event, parameters):
    if action != "send":
        raise ValueError("未知媒体动作")
    name = parameters.get("key", "play_pause")
    count = parameters.get("repeat", 1)
    interval = context.settings.get("repeat_interval", 0.15)
    if not isinstance(name, str) or name not in MEDIA_KEYS:
        raise ValueError("未知媒体键")
    if isinstance(count, bool) or not isinstance(count, (int, float)) or count not in range(1, 6):
        raise ValueError("次数必须是 1–5 的整数")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0.05 <= interval <= 1:
        raise ValueError("按键间隔必须为 0.05–1 秒")
    keyboard, key = _keyboard()
    button = getattr(key, MEDIA_KEYS[name], None)
    if button is None:
        raise RuntimeError("当前系统后端不支持此媒体键")
    for index in range(int(count)):
        if index:
            time.sleep(interval)
        try:
            keyboard.press(button)
        finally:
            keyboard.release(button)
    context.log("媒体键已发送；实际响应取决于系统和播放器，macOS 需辅助功能权限")
    return {"sent": int(count), "key": name}
