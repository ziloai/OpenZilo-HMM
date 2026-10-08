# SPDX-License-Identifier: MPL-2.0
"""Portable, versioned JSON exports. Loading never executes pickle payloads."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from hmmlearn.hmm import GaussianHMM
import numpy as np

from .config import AXES, N_FEATURES, PipelineConfig, SegmentationConfig, RejectionCalibration

FORMAT = "hmm-gesture"
FORMAT_VERSION = 2
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(ord(c) < 32 for c in name):
        raise ValueError("Gesture name must be nonempty text, at most 128 characters, without control characters")
    return name


def write_json(path: str | Path, data: object, *, max_bytes: int | None = None) -> Path:
    """Replace a JSON file atomically, leaving any existing export intact on error."""
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Bundle is too large; export fewer gestures")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _model_arrays(model: GaussianHMM) -> dict[str, np.ndarray]:
    if not isinstance(model, GaussianHMM) or model.covariance_type != "diag":
        raise ValueError("Only diagonal Gaussian HMMs are supported")
    try:
        arrays = {"startprob": np.asarray(model.startprob_, dtype=float),
                  "transmat": np.asarray(model.transmat_, dtype=float),
                  "means": np.asarray(model.means_, dtype=float),
                  "covars": np.asarray(model._covars_, dtype=float)}
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Model has missing or invalid fitted parameters") from exc
    states = model.n_components
    if not isinstance(states, int) or not 1 <= states <= 512:
        raise ValueError("A model must have between 1 and 512 states")
    shapes = {"startprob": (states,), "transmat": (states, states),
              "means": (states, N_FEATURES), "covars": (states, N_FEATURES)}
    for name, array in arrays.items():
        if array.shape != shapes[name] or not np.isfinite(array).all():
            raise ValueError(f"Invalid {name}: expected finite array with shape {shapes[name]}")
    for name in ("startprob", "transmat"):
        array = arrays[name]
        if (array < 0).any() or not np.allclose(array.sum(axis=-1), 1, rtol=1e-7, atol=1e-8):
            raise ValueError(f"Invalid {name}: probabilities must be nonnegative and sum to one")
    if (arrays["covars"] <= 0).any():
        raise ValueError("Model variances must be strictly positive")
    return arrays


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _load_model(raw: object) -> GaussianHMM:
    if not isinstance(raw, dict) or raw.get("type") != "gaussian-hmm" or raw.get("covariance_type") != "diag":
        raise ValueError("Unsupported model type or covariance type")
    try:
        means = np.asarray(raw["means"], dtype=float)
        if means.ndim != 2:
            raise ValueError("Model means must be a matrix")
        model = GaussianHMM(n_components=len(means), covariance_type="diag", init_params="")
        model.startprob_ = np.asarray(raw["startprob"], dtype=float)
        model.transmat_ = np.asarray(raw["transmat"], dtype=float)
        model.means_ = means
        model.covars_ = np.asarray(raw["covars"], dtype=float)
        _model_arrays(model)
        model._check()
        return model
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"Invalid model parameters: {exc}") from exc


def _config_dict(config, version):
    fields = asdict(config)
    if version == 1:
        # Version 1 always used zero filter state and baseline-motion segmentation.
        for key in ("filter_initialization", "mode", "post_roll"):
            fields.pop(key, None)
    return fields


def _load_config(cls: type, raw: object, version: int = FORMAT_VERSION) -> Any:
    fields = set(cls.__dataclass_fields__)
    if version == 1:
        fields -= {"filter_initialization", "mode", "post_roll"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError(f"Missing or unsupported fields in {cls.__name__}")
    try:
        return cls(**raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {cls.__name__}: {exc}") from exc


@dataclass
class GestureBundle:
    models: dict[str, GaussianHMM]
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
    metadata: dict[str, Any] = field(default_factory=dict)
    rejection: dict[str, RejectionCalibration] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pipeline, PipelineConfig) or not isinstance(self.segmentation, SegmentationConfig):
            raise ValueError("Bundle requires PipelineConfig and SegmentationConfig")
        if not isinstance(self.models, dict) or not self.models:
            raise ValueError("A gesture bundle must contain at least one model")
        for name, model in self.models.items():
            validate_name(name)
            _model_arrays(model)
        if self.rejection is not None:
            if not isinstance(self.rejection, dict) or set(self.rejection) != set(self.models):
                raise ValueError("Rejection calibration must cover every gesture exactly")
            for calibration in self.rejection.values():
                if not isinstance(calibration, RejectionCalibration):
                    raise ValueError("Invalid rejection calibration")
                calibration.__post_init__()
        if not isinstance(self.metadata, dict):
            raise ValueError("Bundle metadata must be an object")
        try:
            json.dumps(self.metadata, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Bundle metadata must contain finite JSON values") from exc

    @property
    def gesture_names(self) -> tuple[str, ...]:
        return tuple(self.models)

    def save(self, path: str | Path) -> Path:
        self.__post_init__()
        gestures = []
        for name, model in self.models.items():
            params = {key: value.tolist() for key, value in _model_arrays(model).items()}
            gestures.append({"name": name, "model": {"type": "gaussian-hmm",
                            "covariance_type": "diag", **params}})
        version = (1 if self.rejection is None and self.pipeline.filter_initialization == "zero"
                   and self.segmentation.mode == "motion" and self.segmentation.post_roll == 0 else FORMAT_VERSION)
        data = {"format": FORMAT, "version": version,
                "axes": list(AXES), "units": "raw_int16",
                "pipeline": _config_dict(self.pipeline, version),
                "segmentation": _config_dict(self.segmentation, version),
                "gestures": gestures, "metadata": self.metadata}
        if version >= 2:
            data["rejection"] = ({name: asdict(value) for name, value in self.rejection.items()}
                                 if self.rejection is not None else None)
        return write_json(path, data, max_bytes=MAX_BUNDLE_BYTES)

    @classmethod
    def load(cls, path: str | Path) -> GestureBundle:
        path = Path(path)
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("Bundle exceeds the 32 MiB size limit")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError("Not a valid UTF-8 gesture JSON bundle (pickle is not supported)") from exc
        if not isinstance(raw, dict) or raw.get("format") != FORMAT:
            raise ValueError("Not a hmm-gesture bundle; retrain/export legacy pickle models in Studio")
        if type(raw.get("version")) is not int or raw["version"] not in (1, FORMAT_VERSION):
            raise ValueError(f"Unsupported gesture bundle version: {raw.get('version')}")
        if raw.get("axes") != list(AXES) or raw.get("units") != "raw_int16":
            raise ValueError("Unsupported axis order or sensor units")
        version = raw["version"]
        pipeline = _load_config(PipelineConfig, raw.get("pipeline"), version)
        segmentation = _load_config(SegmentationConfig, raw.get("segmentation"), version)
        gestures = raw.get("gestures")
        if not isinstance(gestures, list) or not gestures:
            raise ValueError("Bundle must contain a nonempty gestures list")
        models = {}
        for entry in gestures:
            if not isinstance(entry, dict):
                raise ValueError("Invalid gesture entry")
            name = validate_name(entry.get("name"))
            if name in models:
                raise ValueError(f"Duplicate gesture name: {name}")
            models[name] = _load_model(entry.get("model"))
        rejection = None
        if version >= 2:
            if "rejection" not in raw:
                raise ValueError("Version 2 requires an explicit rejection field")
            if raw["rejection"] is not None:
                if not isinstance(raw["rejection"], dict):
                    raise ValueError("Rejection calibration must be an object or null")
                rejection = {name: _load_config(RejectionCalibration, value)
                             for name, value in raw["rejection"].items()}
        elif "rejection" in raw:
            raise ValueError("Version 1 cannot contain rejection calibration")
        return cls(models, pipeline, segmentation, raw.get("metadata", {}), rejection)
