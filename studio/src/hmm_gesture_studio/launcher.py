# SPDX-License-Identifier: MPL-2.0
"""Choose which desktop interface to start; neither UI is imported until chosen."""
from __future__ import annotations

import argparse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="hmm-gesture-studio", description="OpenZilo 手势与录音工作室")
    parser.add_argument("--ui", choices=("new", "legacy"), default="new",
                        help="new：PySide6 新界面（默认）；legacy：Tkinter 旧界面")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    if parse_args(argv).ui == "legacy":
        from .gui import main as legacy_main
        legacy_main()
        return
    try:
        from .qt.app import main as qt_main
    except ImportError as exc:
        raise SystemExit(
            f"无法加载新界面：{exc}\n"
            "Linux 可能需要 sudo apt install libegl1 libgl1 libxkbcommon0 libdbus-1-3。\n"
            "也可以用 hmm-gesture-studio --ui legacy 启动旧界面。"
        ) from exc
    qt_main()
