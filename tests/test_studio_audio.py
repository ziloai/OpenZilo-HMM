# SPDX-License-Identifier: MPL-2.0
"""Recording persistence tests without ffmpeg, hardware or a display."""
from __future__ import annotations

from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import openzilo as sdk

from hmm_gesture_studio import audio


class RecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.packetized = b"\x03\x00abc"
        self.pcm = b"\x01\x00" * 320
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.run = self.stack.enter_context(patch(
            "hmm_gesture_studio.audio.subprocess.run",
            return_value=Mock(returncode=0, stdout=self.pcm, stderr=b""),
        ))

    def save(self, data=None, **kwargs):
        return audio.save_recording(
            self.packetized if data is None else data, 7, self.directory, **kwargs,
        )

    def assert_raw_only(self, result, warning):
        self.assertEqual(result["path"], result["raw_path"])
        self.assertEqual(Path(result["raw_path"]).read_bytes(), self.packetized)
        self.assertEqual(result["bytes"], len(self.packetized))
        self.assertEqual(result["file_index"], 7)
        self.assertIsNone(result["duration_s"])
        self.assertIn(warning, result["warning"])
        self.assertEqual(list(self.directory.glob("*.wav")), [])

    def test_success_has_wav_header_duration_and_original_bytes(self):
        result = self.save()
        self.assertEqual(Path(result["raw_path"]).read_bytes(), self.packetized)
        self.assertEqual(result["bytes"], len(self.packetized))
        self.assertEqual(result["file_index"], 7)
        self.assertIsNone(result["warning"])
        with wave.open(result["path"], "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()),
                             (16000, 1, 2))
            self.assertEqual(wav.readframes(wav.getnframes()), self.pcm)
            self.assertEqual(result["duration_s"], wav.getnframes() / wav.getframerate())
        self.assertEqual(result["duration_s"], 0.02)

    def test_raw_exists_before_decoder_runs(self):
        def decode(*args, **kwargs):
            raw_files = list(self.directory.glob("*.bin"))
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(raw_files[0].read_bytes(), self.packetized)
            return Mock(returncode=0, stdout=self.pcm, stderr=b"")
        self.run.side_effect = decode
        self.save()

    def test_repeated_index_never_overwrites(self):
        existing = self.directory / "recording-user.bin"
        existing.write_bytes(b"user data")
        first = self.save()
        original_files = {p: p.read_bytes() for p in self.directory.iterdir()}
        second = self.save()
        self.assertNotEqual(first["raw_path"], second["raw_path"])
        self.assertNotEqual(first["path"], second["path"])
        for path, data in original_files.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(len(list(self.directory.glob("*.wav"))), 2)

    def test_missing_ffmpeg_preserves_bin_with_install_hint(self):
        self.run.side_effect = FileNotFoundError("ffmpeg")
        self.assert_raw_only(self.save(), "install ffmpeg")

    def test_ffmpeg_failure_preserves_bin(self):
        self.run.return_value = Mock(returncode=1, stdout=b"", stderr=b"bad Speex")
        self.assert_raw_only(self.save(), "ffmpeg decoding failed: bad Speex")

    def test_timeout_preserves_bin(self):
        self.run.side_effect = subprocess.TimeoutExpired("ffmpeg", 5)
        self.assert_raw_only(self.save(), "timed out after 5 seconds")

    def test_empty_decoded_audio_preserves_bin(self):
        self.run.return_value.stdout = b""
        self.assert_raw_only(self.save(), "no decoded audio")

    def test_subprocess_is_bounded_shell_free_and_sdk_builds_ogg(self):
        with patch.object(sdk, "parse_packetized_speex_stream", wraps=sdk.parse_packetized_speex_stream) as parse:
            self.save()
        parse.assert_called_once_with(self.packetized, allow_framed_blocks=False)
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "ogg",
            "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
        ])
        self.assertEqual(kwargs["timeout"], 5)
        self.assertFalse(kwargs.get("shell", False))
        self.assertIs(kwargs["check"], False)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        self.assertTrue(sdk.is_ogg_speex(kwargs["input"]))
        self.assertEqual(kwargs["input"], sdk.build_ogg_speex([b"abc"]))

    def test_raw_speex_uses_sdk_split_and_normalization(self):
        raw = b"\x55" * sdk.pick_bits_size(quality=3)
        self.run.return_value.stdout = self.pcm * 2
        with patch.object(sdk, "split_raw_speex_packets", wraps=sdk.split_raw_speex_packets) as split:
            result = self.save(raw)
        split.assert_called_once_with(raw, quality=3, bits_size=None)
        self.assertEqual(result["duration_s"], 0.02)

    def test_existing_ogg_is_passed_unchanged(self):
        ogg = sdk.build_ogg_speex([b"abc"])
        result = self.save(ogg)
        self.assertEqual(self.run.call_args.kwargs["input"], ogg)
        self.assertIsNone(result["warning"])

    def test_existing_wav_needs_no_ffmpeg_and_is_unchanged(self):
        target = io.BytesIO()
        with wave.open(target, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0\0" * 4000)
        original = target.getvalue()
        result = self.save(original)
        self.run.assert_not_called()
        self.assertEqual(Path(result["path"]).read_bytes(), original)
        self.assertEqual(Path(result["raw_path"]).read_bytes(), original)
        self.assertEqual(result["duration_s"], 0.5)

    def test_bad_audio_has_no_invented_duration(self):
        for data in (b"invalid", b"RIFF\0\0\0\0WAVE"):
            with self.subTest(data=data):
                result = self.save(data)
                self.assertEqual(Path(result["raw_path"]).read_bytes(), data)
                self.assertEqual(result["path"], result["raw_path"])
                self.assertIsNone(result["duration_s"])
                self.assertTrue(result["warning"])
        self.run.assert_not_called()

    def test_empty_input_and_illegal_indices_rejected_before_writing(self):
        with self.assertRaises(ValueError):
            self.save(b"")
        with self.assertRaises(TypeError):
            self.save("not bytes")
        for index in (-1, 2**32, True, False, 1.5, "../outside", None):
            with self.subTest(index=index), self.assertRaises(ValueError):
                audio.save_recording(self.packetized, index, self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.run.assert_not_called()

    def test_uint32_boundary_indices_are_valid(self):
        for index in (0, 2**32 - 1):
            self.assertEqual(audio.save_recording(
                self.packetized, index, str(self.directory),
            )["file_index"], index)

    def test_raw_save_error_propagates_without_decode(self):
        with patch.object(audio.tempfile, "mkstemp", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                self.save()
        self.run.assert_not_called()

    def test_failed_write_is_not_success_and_partial_file_is_removed(self):
        with patch.object(audio.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.save()
        self.assertEqual(list(self.directory.iterdir()), [])
        self.run.assert_not_called()

    def test_wav_write_failure_propagates_and_raw_survives(self):
        with patch.object(audio.os, "fsync", side_effect=[None, OSError("disk full")]):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.save()
        self.assertEqual(list(self.directory.glob("*.wav")), [])
        raw_files = list(self.directory.glob("*.bin"))
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(raw_files[0].read_bytes(), self.packetized)

    def test_directory_failure_propagates(self):
        blocked = self.directory / "not-a-directory"
        blocked.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            audio.save_recording(self.packetized, 7, blocked)
        self.assertEqual(blocked.read_bytes(), b"keep")
        self.run.assert_not_called()

    def test_metadata_is_small_allowlisted_and_cannot_control_paths(self):
        result = self.save(metadata={
            "record_time": 123456, "file_index": "../outside", "bytes": 9999,
            "sn": "private-serial", "cpuid": "private-id", "address": "private-address",
        })
        metadata_path = Path(result["metadata_path"])
        self.assertEqual(json.loads(metadata_path.read_text()), {
            "record_time": 123456, "file_index": 7, "bytes": len(self.packetized),
        })
        for path in self.directory.iterdir():
            self.assertEqual(path.parent, self.directory)
            self.assertNotIn("private", path.name)
        self.assertNotIn("private", metadata_path.read_text())

    def test_metadata_write_error_is_visible_and_raw_survives(self):
        with patch.object(audio.os, "fsync", side_effect=[None, OSError("disk full")]):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.save(metadata={"record_time": 100})
        raw_files = list(self.directory.glob("*.bin"))
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(raw_files[0].read_bytes(), self.packetized)
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
