# SPDX-License-Identifier: MPL-2.0
"""Standalone plugin subprocess. Run by absolute filename; no app/Qt imports.

Wire format: bounded UTF-8 JSON lines. fd 1 is diverted to stderr before any
plugin code runs; a private duplicate is reserved for protocol output. This is
process isolation for reliability, not a sandbox for untrusted Python.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid

MAX_MESSAGE = 64 * 1024
MAX_LOG = 4096


def encode(message: dict) -> bytes:
    data = (json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ValueError("Plugin message exceeds 64 KiB")
    return data


def receive(stream) -> dict:
    line = stream.readline(MAX_MESSAGE + 1)
    if not line:
        raise EOFError("Host disconnected")
    if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
        raise ValueError("Invalid or oversized host message")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


class Protocol:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.RLock()
        self.task_id = None
        self.window = 0.0
        self.logs = 0

    def send(self, message: dict) -> None:
        data = encode(message)
        with self.lock:
            self.stream.write(data)
            self.stream.flush()

    def log(self, message: str, level: str = "info") -> None:
        with self.lock:
            now = time.monotonic()
            if now - self.window >= 1:
                self.window, self.logs = now, 0
            if self.logs >= 20:
                return
            self.logs += 1
            self.send({"kind": "log", "message": message[:MAX_LOG],
                       "level": level[:32], "task_id": self.task_id})


class LogStream(io.TextIOBase):
    """Bounded line buffering for Python prints, including partial/huge lines."""

    def __init__(self, protocol: Protocol, level: str):
        self.protocol = protocol
        self.level = level
        self.pending = ""
        self.lock = threading.RLock()

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        with self.lock:
            # Never retain an unbounded partial line. Splitting is chunked too.
            for offset in range(0, len(text), MAX_LOG):
                self.pending += text[offset:offset + MAX_LOG]
                while "\n" in self.pending:
                    line, self.pending = self.pending.split("\n", 1)
                    if line:
                        self.protocol.log(line, self.level)
                if len(self.pending) >= MAX_LOG:
                    self.protocol.log(self.pending[:MAX_LOG], self.level)
                    self.pending = ""
        return len(text)

    def flush(self):
        with self.lock:
            if self.pending:
                self.protocol.log(self.pending, self.level)
                self.pending = ""


def load_entry(code: Path, entry: str):
    """Entry is a relative .py path or dotted module, optionally :callable/object."""
    module_name, separator, attribute = entry.partition(":")
    root = code if code.is_dir() else code.parent
    sys.path.insert(0, str(root))
    if module_name.endswith(".py") or "/" in module_name or "\\" in module_name:
        path = root / module_name
        spec = importlib.util.spec_from_file_location("_zilo_plugin_entry", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load plugin entry: {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_name)
    target = getattr(module, attribute) if separator else module
    action = target if callable(target) else getattr(target, "handle_action", None)
    event = getattr(target, "handle_event", None)
    if separator and event is None:
        event = getattr(module, "handle_event", None)
    if not callable(action) and not callable(event):
        raise TypeError("Entry must export handle_action and/or handle_event")
    return action, event


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True, type=Path)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args()

    protocol = Protocol(os.fdopen(os.dup(sys.stdout.fileno()), "wb"))
    incoming = sys.stdin.buffer
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    output, errors = LogStream(protocol, "info"), LogStream(protocol, "error")
    sys.stdout = sys.__stdout__ = output
    sys.stderr = sys.__stderr__ = errors
    # Resolve the SDK before plugin paths can shadow it, without importing app.
    sdk_spec = importlib.util.spec_from_file_location(
        "zilo_plugin", Path(__file__).resolve().with_name("sdk.py")
    )
    sdk = importlib.util.module_from_spec(sdk_spec)
    sys.modules["zilo_plugin"] = sdk
    sdk_spec.loader.exec_module(sdk)

    try:
        args.data.mkdir(parents=True, exist_ok=True)
        args.cache.mkdir(parents=True, exist_ok=True)
        action_handler, event_handler = load_entry(args.code, args.entry)
        output.flush()
        errors.flush()
        protocol.send({"kind": "started"})
    except BaseException:
        protocol.send({"kind": "error", "phase": "startup", "fatal": True,
                       "message": traceback.format_exc()[-8192:]})
        return 1

    def rpc(method, params):
        request_id = uuid.uuid4().hex
        protocol.send({"kind": "rpc", "request_id": request_id, "method": method,
                       "params": params, "task_id": protocol.task_id})
        response = receive(incoming)
        if response.get("kind") != "reply" or response.get("request_id") != request_id:
            raise RuntimeError("Unexpected host RPC response")
        if response.get("error") is not None:
            raise sdk.RPCError(response["error"])
        return response.get("result")

    while True:
        try:
            task = receive(incoming)
        except EOFError:
            return 0
        if task.get("kind") != "task":
            raise ValueError("Expected a task")
        protocol.task_id = task["task_id"]
        context = sdk.Context(task["settings"], args.data, args.cache,
                              task["event"], rpc, protocol.log)
        try:
            if task["action"] == "@event":
                if not callable(event_handler):
                    raise TypeError("Plugin does not export handle_event")
                result = event_handler(context, task["event"])
            else:
                if not callable(action_handler):
                    raise TypeError("Plugin does not export handle_action")
                result = action_handler(context, task["action"], task["event"], task["parameters"])
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("Plugin handlers must be synchronous")
            output.flush()
            errors.flush()
            # Encoding here also validates that the result is finite JSON and bounded.
            protocol.send({"kind": "finished", "task_id": protocol.task_id,
                           "result": result})
        except BaseException:
            output.flush()
            errors.flush()
            protocol.send({"kind": "error", "task_id": protocol.task_id,
                           "phase": "task", "fatal": False,
                           "message": traceback.format_exc()[-8192:]})
        finally:
            context._active = False
            protocol.task_id = None


if __name__ == "__main__":
    raise SystemExit(main())
