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


def validate_training_region(region, sample_count: int) -> tuple[int, int] | None:
    """Validate a non-destructive, half-open [start, end) raw-sample annotation."""
    if region is None:
        return None
    if (not isinstance(region, (list, tuple)) or len(region) != 2
            or any(type(value) is not int for value in region)
            or not 0 <= region[0] < region[1] <= sample_count):
        raise ValueError("training_region must be [start, end) integer sample indices within the recording")
    return tuple(region)


@dataclass
class GestureDataset:
    name: str
    repetitions: list[np.ndarray]
    sample_rate_hz: float = 25.0
    # None means automatic cropping for every repetition. Otherwise entries
    # align with repetitions; an individual None still uses automatic cropping.
    training_regions: list[tuple[int, int] | None] | None = None

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
        if self.training_regions is not None:
            if (not isinstance(self.training_regions, list)
                    or len(self.training_regions) != len(validated)):
                raise ValueError("training_regions must have one entry per recording")
            self.training_regions = [validate_training_region(region, len(rep))
                                     for region, rep in zip(self.training_regions, validated)]
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
        regions = [rep.get("training_region") for rep in raw["repetitions"]]
        return GestureDataset(raw["name"], [rep["data"] for rep in raw["repetitions"]],
                              raw.get("sample_rate_hz", 25.0),
                              regions if any(region is not None for region in regions) else None)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
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
    return save_dataset_file(dataset, dataset_path(dataset.name, directory))


def save_dataset_file(dataset: GestureDataset, path: str | Path) -> Path:
    """Atomically save at an existing file's exact path, including imported names."""
    dataset.__post_init__()
    path = Path(path)
    if path.exists():
        existing = load_dataset(path)
        if existing.name != dataset.name:
            raise ValueError(f"Filename collision with gesture {existing.name!r}; choose another name")
    repetitions = []
    for i, rep in enumerate(dataset.repetitions):
        entry = {"index": i, "num_samples": len(rep), "data": rep.tolist()}
        region = dataset.training_regions[i] if dataset.training_regions is not None else None
        if region is not None:
            entry["training_region"] = list(region)
        repetitions.append(entry)
    data = {"name": dataset.name, "created_at": datetime.now().astimezone().isoformat(),
            "sample_rate_hz": dataset.sample_rate_hz, "axes": list(AXES), "units": "raw_int16",
            "num_repetitions": len(dataset.repetitions),
            "repetitions": repetitions}
    return write_json(path, data)
