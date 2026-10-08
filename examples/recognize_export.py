# SPDX-License-Identifier: MPL-2.0
"""Classify a recording using only the installed hmm-gesture runtime package.

uv run --locked python examples/recognize_export.py --bundle exports/demo.gesture.json --input sample_data/向上.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hmm_gesture import GestureRecognizer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path, help="Recording JSON with data or repetitions")
    args = parser.parse_args()
    try:
        recognizer = GestureRecognizer.load(args.bundle)
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        sample_rate = raw.get("sample_rate_hz", 25.0)
        sequences = [rep["data"] for rep in raw["repetitions"]] if "repetitions" in raw else [raw["data"]]
        for index, samples in enumerate(sequences, 1):
            prediction = recognizer.predict(samples, sample_rate_hz=sample_rate)
            if prediction is None:
                print(f"{index}: no prediction (too short or rejected)")
            else:
                print(f"{index}: {prediction.name}  confidence={prediction.confidence:.3f} "
                      f"score/frame={prediction.score:.3f}")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
