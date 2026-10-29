# SPDX-License-Identifier: MPL-2.0
"""Best-effort local PCM monitoring, independent of recording and Tk.

``emit(name, payload)`` must return promptly and be safe on worker threads.
Device imports, opening and cleanup happen on a dedicated thread. Call ``close``
via a joined/shielded ``asyncio.to_thread`` when used from an async worker.
"""
from __future__ import annotations

from collections.abc import Callable
from queue import Empty, Full, Queue
import threading
from typing import Any


class PcmPlayback:
    """A single-use, bounded 16 kHz int16 monitor; failures never stop recording."""

    def __init__(self, emit: Callable[[str, Any], None]) -> None:
        self._emit = emit
        self._queue: Queue[bytes] = Queue(maxsize=25)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._warned = False
        self._frame_bytes = 0
        self._input_pending = b""
        self._output_pending = b""

    def _fail(self, reason: Any) -> None:
        self._stop.set()
        with self._lock:
            if self._warned:
                return
            self._warned = True
        try:
            self._emit("warning", f"实时播放已停止：{reason}。录音接收与保存继续，不受影响。")
        except Exception:
            # Reporting a monitor failure must not become a recording failure.
            pass

    def start(self, pcm_config: Any) -> None:
        """Start once without waiting for a device; accept SDK PcmConfig."""
        try:
            with self._lock:
                if self._stop.is_set() or self._thread is not None:
                    return
                # OpenZilo calls the bit-depth field `bit_depth`.
                if (pcm_config.sample_rate != 16000 or pcm_config.channels not in (1, 2)
                        or pcm_config.bit_depth != 16):
                    raise ValueError("仅支持 16 kHz、16-bit 单/双声道 PCM")
                self._frame_bytes = 320 * pcm_config.channels * 2
                thread = threading.Thread(
                    target=self._run, args=(pcm_config,), name="pcm-playback", daemon=True,
                )
                thread.start()
                self._thread = thread
        except Exception as exc:
            self._fail(exc)

    def feed(self, data: bytes) -> None:
        """Enqueue without device I/O; overflow disables monitoring, not recording."""
        try:
            with self._lock:
                if self._stop.is_set() or self._thread is None or not data:
                    return
                # Count partial input too, so even fragmented feeds stay <= 500 ms.
                if (self._queue.qsize() * self._frame_bytes
                        + len(self._input_pending) + len(data) > 25 * self._frame_bytes):
                    raise Full("播放缓冲区已满")
                pending = self._input_pending + data
                complete = len(pending) - len(pending) % self._frame_bytes
                for offset in range(0, complete, self._frame_bytes):
                    self._queue.put_nowait(pending[offset:offset + self._frame_bytes])
                self._input_pending = pending[complete:]
        except Exception as exc:
            self._fail(exc)

    def _callback(self, outdata: Any, frames: int, time: Any, status: Any) -> None:
        try:
            output = memoryview(outdata).cast("B")
            output[:] = bytes(len(output))
            if self._stop.is_set():
                return
            if status:
                self._fail(status)
                return
            offset = 0
            while offset < len(output) and not self._stop.is_set():
                if not self._output_pending:
                    try:
                        self._output_pending = self._queue.get_nowait()
                    except Empty:
                        break
                count = min(len(output) - offset, len(self._output_pending))
                output[offset:offset + count] = self._output_pending[:count]
                self._output_pending = self._output_pending[count:]
                offset += count
            if self._stop.is_set():
                output[:] = bytes(len(output))
        except Exception as exc:
            # PortAudio otherwise swallows callback exceptions and may keep going.
            try:
                outdata[:] = bytes(len(outdata))
            except Exception:
                pass
            self._fail(exc)

    def _run(self, config: Any) -> None:
        stream = None
        try:
            if self._stop.is_set():
                return
            import sounddevice

            stream = sounddevice.RawOutputStream(
                dtype="int16", samplerate=config.sample_rate, channels=config.channels,
                blocksize=320, callback=self._callback,
            )
            if self._stop.is_set():
                return
            stream.start()
            while not self._stop.wait(0.05):
                if not stream.active:
                    raise RuntimeError("音频输出设备已停止")
        except Exception as exc:
            self._fail(exc)
        finally:
            if stream is not None:
                # Never drain queued audio on shutdown, and try close even if abort fails.
                for cleanup in (stream.abort, stream.close):
                    try:
                        cleanup()
                    except Exception as exc:
                        self._fail(exc)

    def close(self) -> None:
        """Silence immediately and join device cleanup; safe before start/repeatedly."""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join()
            except Exception as exc:
                self._fail(exc)
        with self._lock:
            self._input_pending = b""
            self._output_pending = b""
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break
