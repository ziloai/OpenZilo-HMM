# SPDX-License-Identifier: MPL-2.0
"""Validated training datasets, compatible with the original recorder JSON."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re

import numpy as np

from hmm_gesture.bundle import validate_name, write_json
from hmm_gesture.config import AXES, positive_number
from hmm_gesture.preprocessing import validate_samples


@dataclass
class GestureDataset:
    name: str
    repetitions: list[np.ndarray]
    sample_rate_hz: float = 25.0

    def __post_init__(self) -> None:
        validate_name(self.name)
        positive_number("sample_rate_hz", self.sample_rate_hz)
        if not isinstance(self.repetitions, list) or not self.repetitions:
            raise ValueError(f"{self.name}: at least one recording is required")
        validated = []
        for rep in self.repetitions:
            data = validate_samples(rep)
            if not np.equal(data, np.rint(data)).all():
                raise ValueError("Recordings must contain raw integer IMU values")
            validated.append(data.astype(np.int16))
        self.repetitions = validated


def load_dataset(path: str | Path) -> GestureDataset:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Dataset must be a JSON object")
        if raw.get("axes", list(AXES)) != list(AXES):
            raise ValueError("Unsupported axis order")
        if raw.get("units", "raw_int16") != "raw_int16":
            raise ValueError("Unsupported sensor units")
        # Original sample_data predates explicit rates; it was recorded at 25 Hz.
        return GestureDataset(raw["name"], [rep["data"] for rep in raw["repetitions"]],
                              raw.get("sample_rate_hz", 25.0))
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{path.name}: invalid gesture dataset: {exc}") from exc


def dataset_path(name: str, directory: str | Path) -> Path:
    validate_name(name)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:64]
    if not safe:
        raise ValueError("Gesture name does not produce a valid filename")
    if safe.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        safe = "_" + safe
    return Path(directory) / f"{safe}.json"


def save_dataset(dataset: GestureDataset, directory: str | Path) -> Path:
    dataset.__post_init__()
    path = dataset_path(dataset.name, directory)
    if path.exists():
        existing = load_dataset(path)
        if existing.name != dataset.name:
            raise ValueError(f"Filename collision with gesture {existing.name!r}; choose another name")
    data = {"name": dataset.name, "created_at": datetime.now().astimezone().isoformat(),
            "sample_rate_hz": dataset.sample_rate_hz, "axes": list(AXES), "units": "raw_int16",
            "num_repetitions": len(dataset.repetitions),
            "repetitions": [{"index": i, "num_samples": len(rep), "data": rep.tolist()}
                            for i, rep in enumerate(dataset.repetitions)]}
    return write_json(path, data)
