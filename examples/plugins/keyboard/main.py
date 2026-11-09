"""Trusted, non-sandboxed example. Import pynput only when an action runs."""
import re
import sys


def _keyboard():
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError("仅支持 macOS / Windows；Linux 不支持键盘示例")
    try:
        from pynput.keyboard import Controller, Key
    except Exception:
        raise RuntimeError("键盘后端不可用；请安装声明依赖，macOS 请检查辅助功能权限") from None
    return Controller(), Key


def _tokens(value):
    if not isinstance(value, str):
        raise ValueError("组合键必须是文本")
    tokens = [part.strip().lower() for part in value.split("+")]
    named = {"primary", "ctrl", "alt", "shift", "cmd", "enter", "tab", "space",
             "esc", "backspace", "delete", "left", "right", "up", "down"}
    if not 1 <= len(tokens) <= 5 or len(set(tokens)) != len(tokens):
        raise ValueError("组合键应包含 1–5 个不重复按键")
    if any(t not in named and not re.fullmatch(r"[a-z0-9]|f(?:[1-9]|1[0-2])", t)
           for t in tokens):
        raise ValueError("不支持的按键；请参考组合键参数说明")
    return tokens


def handle_action(context, action, event, parameters):
    if action == "type_text":
        if context.settings.get("allow_text", False) is not True:
            raise ValueError("请先明确允许输入文本")
        text = parameters.get("text", "Hello")
        if not isinstance(text, str) or not 1 <= len(text) <= 1000:
            raise ValueError("文本长度必须为 1–1000 字符")
        keyboard, _ = _keyboard()
        keyboard.type(text)
    elif action == "hotkey":
        tokens = _tokens(parameters.get("keys", "primary+c"))
        keyboard, key = _keyboard()
        primary = "cmd" if sys.platform == "darwin" else "ctrl"
        keys = [getattr(key, primary if t == "primary" else t)
                if len(t) > 1 else t for t in tokens]
        if len(set(keys)) != len(keys):
            raise ValueError("组合键包含重复修饰键")
        pressed = []
        try:
            for item in keys:
                pressed.append(item)
                keyboard.press(item)
        finally:
            # Attempt every release, even if one release fails.
            failed = False
            for item in reversed(pressed):
                try:
                    keyboard.release(item)
                except Exception:
                    failed = True
            if failed:
                raise RuntimeError("按键释放失败，请检查输入设备状态")
    else:
        raise ValueError("未知键盘动作")
    context.log("键盘动作已发送；请在目标应用检查结果（macOS 需辅助功能权限）")
    return {"sent": True}
