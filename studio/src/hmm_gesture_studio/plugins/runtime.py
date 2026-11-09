# SPDX-License-Identifier: MPL-2.0
"""Main-thread plugin process coordinator; no Qt or application dependencies.

Call poll() regularly on the owner thread (e.g. every 25 ms). All public methods
belong to that thread. Workers only communicate through bounded internal queues.
Host events are ('plugin', payload); use an unbounded queue.Queue for the host
queue and drain it regularly. Log delivery is deliberately lossy/rate limited.
Python plugins are arbitrary trusted code, NOT sandboxed. POSIX process groups
are killed, including descendants that stay in the group; deliberately detached
children cannot be contained. Windows tree cleanup is best effort via taskkill.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Any
import uuid

MAX_MESSAGE = 64 * 1024
MAX_LOG = 4096
MAX_WAITING = 20


def _encode(message: dict) -> bytes:
    data = (json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ValueError("Plugin message exceeds 64 KiB")
    return data


def _reader(stream, inbox: queue.Queue, protocol: bool) -> None:
    """No runtime state mutation, including for native stdout/stderr output."""
    window, logs = 0.0, 0
    try:
        while True:
            line = stream.readline(MAX_MESSAGE + 1 if protocol else MAX_LOG)
            if not line:
                break
            if protocol:
                if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                    raise ValueError("Invalid or oversized plugin message")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Plugin message must be an object")
            else:
                message = {"kind": "log", "level": "error",
                           "message": line.decode("utf-8", errors="replace").rstrip()}
            if message.get("kind") == "log":
                now = time.monotonic()
                if now - window >= 1:
                    window, logs = now, 0
                if logs >= 20:
                    continue
                logs += 1
                message["message"] = str(message.get("message", ""))[:MAX_LOG]
                if inbox.qsize() < 128:
                    try:
                        inbox.put_nowait(("message", message))
                    except queue.Full:
                        pass
            else:
                message["_received_at"] = time.monotonic()
                try:
                    inbox.put_nowait(("message", message))
                except queue.Full:
                    return  # Protocol flood: close the pipe rather than leak a worker.
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        if protocol:
            try:
                inbox.put_nowait(("broken", str(exc)))
            except queue.Full:
                pass
    finally:
        stream.close()
        if protocol:
            try:
                inbox.put_nowait(("eof", None))
            except queue.Full:
                pass


def _writer(stream, outbox: queue.Queue, inbox: queue.Queue) -> None:
    try:
        while True:
            data = outbox.get()
            if data is None:
                return
            remaining = memoryview(data)
            while remaining:
                written = stream.write(remaining)
                if not written:
                    raise OSError("Plugin input pipe closed")
                remaining = remaining[written:]
            stream.flush()
    except (OSError, ValueError) as exc:
        try:
            inbox.put_nowait(("broken", str(exc)))
        except queue.Full:
            pass
    finally:
        stream.close()


@dataclass
class _Process:
    plugin_id: str
    process: subprocess.Popen
    deadline: float
    inbox: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=256))
    outbox: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=32))
    waiting: deque = field(default_factory=deque)
    active: dict | None = None
    ready: bool = False
    eof: bool = False
    exit_seen: float | None = None
    requests: set = field(default_factory=set)
    log_window: float = 0.0
    log_count: int = 0


class Runtime:
    def __init__(self, event_queue: queue.Queue, *, startup_timeout: float = 30):
        self._events = event_queue
        self._owner = threading.get_ident()
        self._processes: dict[str, _Process] = {}
        self._closed = False
        self.startup_timeout = self._timeout(startup_timeout)

    @staticmethod
    def _timeout(value: float) -> float:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Timeout must be positive and finite")
        return value

    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("Runtime methods must be called on the owner thread")

    def _emit(self, plugin_id: str, kind: str, **payload: Any) -> None:
        try:
            self._events.put_nowait(("plugin", dict(payload, plugin_id=plugin_id, kind=kind)))
        except queue.Full:
            # A bounded caller queue must not freeze the UI. Use an unbounded
            # host queue when reliable lifecycle/RPC delivery is required.
            pass

    def _log(self, state: _Process, message: str, level: str = "info", task_id=None) -> None:
        now = time.monotonic()
        if now - state.log_window >= 1:
            state.log_window, state.log_count = now, 0
        if state.log_count >= 40 or self._events.qsize() >= 256:
            return
        state.log_count += 1
        self._emit(state.plugin_id, "log", message=str(message)[:MAX_LOG],
                   level=str(level)[:32], task_id=task_id)

    def start(self, plugin_id: str, python: Path, code: Path, entry: str,
              data_dir: Path, cache_dir: Path) -> None:
        self._check_thread()
        if self._closed:
            raise RuntimeError("Runtime is closed")
        if plugin_id in self._processes:
            raise ValueError(f"Plugin already running: {plugin_id}")
        runner = Path(__file__).resolve().with_name("runner.py")
        command = [str(Path(python).absolute()), "-u", str(runner),
                   "--code", str(Path(code).resolve()), "--entry", entry,
                   "--data", str(Path(data_dir).resolve()), "--cache", str(Path(cache_dir).resolve())]
        try:
            options = {"start_new_session": True} if os.name == "posix" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            }
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, bufsize=0, **options)
        except (OSError, ValueError) as exc:
            self._emit(plugin_id, "error", phase="startup", fatal=True, message=str(exc))
            self._emit(plugin_id, "stopped", reason="Failed to start")
            return
        state = _Process(plugin_id, process, time.monotonic() + self.startup_timeout)
        self._processes[plugin_id] = state
        for target, args in (
            (_reader, (process.stdout, state.inbox, True)),
            (_reader, (process.stderr, state.inbox, False)),
            (_writer, (process.stdin, state.outbox, state.inbox)),
        ):
            threading.Thread(target=target, args=args, daemon=True,
                             name=f"plugin-{plugin_id}-{target.__name__}").start()

    def running(self, plugin_id: str) -> bool:
        """True while the child is alive, including startup/import."""
        self._check_thread()
        state = self._processes.get(plugin_id)
        return state is not None and state.process.poll() is None

    def busy(self, plugin_id: str) -> bool:
        """True only for an in-flight task, not startup or waiting-only work."""
        self._check_thread()
        state = self._processes.get(plugin_id)
        return state is not None and state.active is not None

    def cancel_pending(self, plugin_id: str | None = None, event_type: str | None = None) -> int:
        """Discard matching waiting tasks, never interrupt the in-flight handler.

        None selects all plugins/event types; event_type matches event['type']
        exactly. Returns the number removed. No cancellation events are emitted.
        """
        self._check_thread()
        removed = 0
        for key, state in self._processes.items():
            if plugin_id is not None and key != plugin_id:
                continue
            retained = deque()
            for item in state.waiting:
                if event_type is None or item[0]["event"].get("type") == event_type:
                    removed += 1
                else:
                    retained.append(item)
            state.waiting = retained
        return removed

    def submit(self, plugin_id: str, action: str, event: dict, parameters: dict,
               settings: dict, timeout: float = 30) -> bool:
        self._check_thread()
        state = self._processes.get(plugin_id)
        if state is None or state.process.poll() is not None:
            return False
        if len(state.waiting) >= MAX_WAITING:
            self._log(state, "Task rejected: plugin waiting queue is full (20)", "warning")
            return False
        try:
            duration = self._timeout(timeout)
            if not isinstance(action, str) or not all(isinstance(v, dict) for v in (event, parameters, settings)):
                raise ValueError("Action must be a string; event, parameters and settings must be dicts")
            task = {"kind": "task", "task_id": uuid.uuid4().hex, "action": action,
                    "event": event, "parameters": parameters, "settings": settings}
            # JSON round trip freezes nested caller-owned values at submission.
            data = _encode(task)
            snapshot = json.loads(data)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            self._log(state, f"Task rejected: {exc}", "warning")
            return False
        state.waiting.append((snapshot, data, duration))
        self._advance(state)
        return True

    def _advance(self, state: _Process) -> None:
        if state.ready and state.active is None and state.waiting:
            task, data, timeout = state.waiting.popleft()
            state.active = task
            state.deadline = time.monotonic() + timeout
            self._send(state, data)

    def _send(self, state: _Process, data: bytes) -> None:
        try:
            state.outbox.put_nowait(data)
        except queue.Full:
            self._fail(state, "Plugin IPC output queue is full", "protocol")

    def has_request(self, plugin_id: str, request_id: str) -> bool:
        """Reject stale host calls after stop, restart or task completion."""
        self._check_thread()
        state = self._processes.get(plugin_id)
        return state is not None and request_id in state.requests

    def reply(self, plugin_id: str, request_id: str, result=None, error=None) -> None:
        self._check_thread()
        state = self._processes.get(plugin_id)
        if state is None or request_id not in state.requests:
            return  # Late/duplicate replies after stop or task completion are inert.
        try:
            data = _encode({"kind": "reply", "request_id": request_id, "result": result, "error": error})
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            data = _encode({"kind": "reply", "request_id": request_id,
                            "error": f"Invalid host RPC response: {str(exc)[:512]}"})
        state.requests.remove(request_id)
        self._send(state, data)

    def _fail(self, state: _Process, message: str, phase: str) -> None:
        task = state.active or {}
        self._emit(state.plugin_id, "error", message=message[:8192], phase=phase, fatal=True,
                   task_id=task.get("task_id"), action=task.get("action"), event=task.get("event"))
        self.stop(state.plugin_id, message)

    def _message(self, state: _Process, message: dict) -> None:
        kind = message.get("kind")
        if (kind != "log" and (not state.ready or state.active is not None)
                and message.get("_received_at", 0) > state.deadline):
            self._fail(state, "Plugin startup timed out" if not state.ready else "Plugin task timed out",
                       "startup" if not state.ready else "timeout")
            return
        if kind == "log":
            self._log(state, message.get("message", ""), message.get("level", "info"),
                      message.get("task_id"))
        elif kind == "started" and not state.ready:
            state.ready = True
            self._emit(state.plugin_id, "started", pid=state.process.pid)
        elif kind == "error" and not state.ready:
            self._fail(state, str(message.get("message", "Import failed")), "startup")
        elif kind in ("finished", "error", "rpc") and state.active is not None:
            task = state.active
            if message.get("task_id") != task["task_id"]:
                self._fail(state, "Mismatched task id", "protocol")
                return
            if kind == "rpc":
                request_id, method, params = (message.get(k) for k in ("request_id", "method", "params"))
                if not isinstance(request_id, str) or not isinstance(method, str) or not isinstance(params, dict) or state.requests:
                    self._fail(state, "Invalid RPC request", "protocol")
                    return
                state.requests.add(request_id)
                self._emit(state.plugin_id, "rpc", request_id=request_id, method=method,
                           params=params, task_id=task["task_id"],
                           action=task["action"], event=task["event"])
                return
            details = {"task_id": task["task_id"], "action": task["action"], "event": task["event"]}
            if kind == "finished":
                details["result"] = message.get("result")
            else:
                details.update(message=str(message.get("message", "Task failed"))[:8192],
                               phase="task", fatal=False)
            self._emit(state.plugin_id, kind, **details)
            state.active = None
            state.requests.clear()
        else:
            self._fail(state, "Unexpected plugin protocol message", "protocol")

    def poll(self) -> None:
        self._check_thread()
        for plugin_id, state in list(self._processes.items()):
            for _ in range(256):
                try:
                    kind, value = state.inbox.get_nowait()
                except queue.Empty:
                    break
                if kind == "message":
                    self._message(state, value)
                elif kind == "broken":
                    self._fail(state, f"Plugin IPC failed: {value}", "protocol")
                elif kind == "eof":
                    state.eof = True
                if plugin_id not in self._processes:
                    break
            if plugin_id not in self._processes:
                continue
            now = time.monotonic()
            returncode = state.process.poll()
            if returncode is not None:
                if state.exit_seen is None:
                    state.exit_seen = now
                # Allow the reader to enqueue final frames, but don't wait on
                # inherited pipe handles retained by a child process forever.
                if state.eof or now - state.exit_seen >= 0.1:
                    self._fail(state, f"Plugin process exited ({returncode})", "exit")
                continue
            if state.eof:
                self._fail(state, "Plugin closed its protocol stream", "protocol")
            elif (not state.ready or state.active is not None) and now >= state.deadline:
                self._fail(state, "Plugin startup timed out" if not state.ready else "Plugin task timed out",
                           "startup" if not state.ready else "timeout")
            else:
                self._advance(state)

    def stop(self, plugin_id: str, reason: str = "") -> None:
        self._check_thread()
        state = self._processes.pop(plugin_id, None)
        if state is None:
            return
        state.waiting.clear()
        state.requests.clear()
        process = state.process
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                # macOS can report EPERM while the process group is exiting.
                # Always reap, even if group signalling races with child exit.
                try:
                    process.kill()
                except OSError:
                    pass
        else:
            # No Windows Job Object containment: detached descendants are not
            # guaranteed. taskkill is asynchronous so close never waits on it.
            try:
                killer = subprocess.Popen(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                threading.Thread(target=killer.wait, daemon=True).start()
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            state.outbox.put_nowait(None)
        except queue.Full:
            pass
        # Reap asynchronously: never wait for process or pipe threads on the UI.
        threading.Thread(target=process.wait, daemon=True, name=f"plugin-{plugin_id}-reap").start()
        self._emit(plugin_id, "stopped", reason=reason, pid=process.pid)

    def close(self) -> None:
        self._check_thread()
        if self._closed:
            return
        self._closed = True
        for plugin_id in list(self._processes):
            self.stop(plugin_id, "Runtime closed")
