# SPDX-License-Identifier: MPL-2.0
"""Small, dependency-free plugin SDK, exposed by runner as ``zilo_plugin``.

Plugins export synchronous ``handle_action(context, action, event, parameters)``
and/or ``handle_event(context, event)``. Events identify their type with ``type``.
This SDK is an IPC convenience, NOT a security sandbox.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

__all__ = ["Context", "Recording", "RPCError"]


class RPCError(RuntimeError):
    """The host rejected an RPC request."""

    def __init__(self, error: Any):
        self.error = error
        super().__init__(str(error))


class Recording:
    """Device ownership remains with the host; all methods are synchronous RPC."""

    def __init__(self, context: Context):
        self._context = context

    def start(self, max_seconds: float = 60, live: bool = False) -> Any:
        return self._context.call("recording.start", max_seconds=max_seconds, live=live)

    def stop(self, session_id: str) -> Any:
        return self._context.call("recording.stop", session_id=session_id)

    def status(self) -> Any:
        return self._context.call("recording.status")


class Context:
    """One task's settings snapshot, persistent directories and host RPC access.

    RPC is only valid on the handler thread while the handler is running.
    ``recording.completed`` handlers cannot start another recording (including
    via ``call``), preventing feedback loops. The host should enforce this too.
    """

    def __init__(
        self,
        settings: dict,
        data_dir: Path,
        cache_dir: Path,
        event: dict,
        call: Callable[..., Any],
        log: Callable[[str, str], None],
    ):
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.cache_dir = Path(cache_dir)
        self.recording = Recording(self)
        self._rpc = call
        self._log = log
        self._owner = threading.get_ident()
        self._active = True
        self._recording_completed = any(
            event.get(key) == "recording.completed"
            for key in ("type", "name", "kind", "event")
        )

    def log(self, message: Any, level: str = "info") -> None:
        self._log(str(message), str(level))

    def call(self, method: str, **params: Any) -> Any:
        if not self._active or threading.get_ident() != self._owner:
            raise RuntimeError("RPC is only available inside the synchronous handler")
        if not isinstance(method, str) or not method:
            raise ValueError("RPC method must be a nonempty string")
        if method == "recording.start" and self._recording_completed:
            raise RPCError("recording.start is forbidden during recording.completed")
        return self._rpc(method, params)
