# SPDX-License-Identifier: MPL-2.0
"""Persist complete ring recordings; no BLE, microphone, or GUI operations."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import wave

import openzilo as sdk


def _bounded_speex_decoder(source: bytes, options: dict) -> sdk.SpeexDecodeResult:
    """Keep SDK parsing/container semantics, but bound the external decoder."""
    config = sdk.normalize_pcm_config(options.get("pcm_config"))
    packet_count = 0
    source_type = "ogg-speex"
    if sdk.is_ogg_speex(source):
        ogg = source
    else:
        packets = sdk.parse_packetized_speex_stream(
            source, allow_framed_blocks=options.get("allow_framed_blocks", False),
        )
        source_type = "packet-speex"
        if not packets:
            packets = sdk.split_raw_speex_packets(
                source, quality=options.get("quality", 3),
                bits_size=options.get("bits_size"),
            )
            source_type = "raw-speex"
        if not packets:
            raise sdk.AudioDecodeError("Recording is not recognized as Speex")
        packet_count = len(packets)
        ogg = sdk.build_ogg_speex(packets, pcm_config=config)

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "ogg",
        "-i", "pipe:0", "-f", "s16le" if config.bit_depth == 16 else "u8",
        "-ac", str(config.channels), "-ar", str(config.sample_rate), "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command, input=ogg, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5, check=False,
        )
    except FileNotFoundError as exc:
        raise sdk.SpeexDecoderUnavailable(
            "ffmpeg is missing; install ffmpeg and ensure it is on PATH to decode recordings."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise sdk.AudioDecodeError("ffmpeg decoding timed out after 5 seconds") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise sdk.AudioDecodeError(f"ffmpeg decoding failed: {detail}")
    pcm = completed.stdout
    if not pcm:
        raise sdk.AudioDecodeError("ffmpeg returned no decoded audio")
    if packet_count:
        pcm = sdk.normalize_decoded_speex_pcm(
            pcm, packet_count=packet_count, pcm_config=config,
        )
    return sdk.SpeexDecodeResult(
        pcm_bytes=pcm, pcm_config=config, source_type=source_type,
        source_extension="spx", packet_count=packet_count,
    )


def _save_unique(directory: Path, suffix: str, data: bytes, *, prefix: str = "recording-") -> Path:
    """Exclusive creation prevents collisions and never truncates existing files."""
    fd, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def save_recording(
    data: bytes,
    file_index: int,
    directory: str | Path,
    *,
    metadata: dict | None = None,
) -> dict:
    """Save raw bytes first, then attempt WAV decoding with a five-second timeout.

    Decode errors return a raw-only result with a warning; filesystem errors
    propagate. Duration is taken only from a valid WAV header. Optional metadata
    persists only an integer SDK record_time, byte count and device file index.
    All filenames are generated locally, independently of device input.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if not data:
        raise ValueError("Recording data must not be empty")
    if isinstance(file_index, bool) or not isinstance(file_index, int):
        raise ValueError("file_index must be an unsigned 32-bit integer")
    if not 0 <= file_index <= 0xFFFFFFFF:
        raise ValueError("file_index must be an unsigned 32-bit integer")
    if metadata is not None and not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary or None")

    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    raw_path = _save_unique(directory, ".bin", data)
    result = {
        "path": str(raw_path), "raw_path": str(raw_path), "duration_s": None,
        "bytes": len(data), "file_index": file_index, "warning": None,
    }
    if metadata is not None:
        safe_metadata = {"bytes": len(data), "file_index": file_index}
        record_time = metadata.get("record_time")
        if type(record_time) is int:
            safe_metadata["record_time"] = record_time
        metadata_path = _save_unique(
            directory, ".metadata.json", json.dumps(safe_metadata).encode("utf-8"),
            prefix=raw_path.stem + "-",
        )
        result["metadata_path"] = str(metadata_path)

    try:
        wav_bytes = sdk.decode_audio_to_wav(data, speex_decoder=_bounded_speex_decoder)
        with wave.open(io.BytesIO(wav_bytes), "rb") as decoded:
            duration_s = decoded.getnframes() / decoded.getframerate()
    except Exception as exc:
        result["warning"] = f"Raw recording saved; WAV unavailable: {exc}"
        return result

    # Do not catch filesystem failures as if saving had succeeded.
    wav_path = _save_unique(directory, ".wav", wav_bytes, prefix=raw_path.stem + "-")
    result.update(path=str(wav_path), duration_s=duration_s)
    return result
