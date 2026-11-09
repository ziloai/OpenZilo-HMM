# SPDX-License-Identifier: MPL-2.0
"""Local model snapshots for gesture bindings; no plugin code or device access.

Snapshots are addressed by inference-content fingerprint. The small active.json
pointer is updated only after binding persistence succeeds. Loading never starts
recognition. Missing/corrupt files raise rather than silently selecting a model.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from hmm_gesture import GestureBundle

from .manifest import contained_path, read_json
from .storage import atomic_json

_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def model_fingerprint(bundle):
    """Hash inference content, not filesystem location or training timestamps."""
    if bundle is None:
        return ""
    from hmm_gesture.bundle import _model_arrays
    content = dict(pipeline=asdict(bundle.pipeline), segmentation=asdict(bundle.segmentation),
                   gestures=[(name, {key: value.tolist() for key, value in _model_arrays(model).items()})
                             for name, model in bundle.models.items()],
                   rejection={name: asdict(value) for name, value in bundle.rejection.items()}
                   if bundle.rejection is not None else None)
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class BoundModelStore:
    def __init__(self, plugin_root):
        self.plugin_root = Path(plugin_root)

    @property
    def root(self):
        # Hidden from plugin discovery and outside any individual plugin's data.
        return contained_path(self.plugin_root, ".models")

    def path(self, fingerprint):
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("Invalid bound-model fingerprint")
        return contained_path(self.root, fingerprint + ".gesture.json")

    def save(self, bundle, fingerprint):
        if not isinstance(bundle, GestureBundle) or model_fingerprint(bundle) != fingerprint:
            raise ValueError("Current model does not match the binding fingerprint")
        path = self.path(fingerprint)
        # GestureBundle.save uses a same-directory temporary file and os.replace.
        bundle.save(path)
        return path

    def activate(self, fingerprint):
        path = self.path(fingerprint)
        if not path.is_file():
            raise ValueError("Bound model snapshot is missing")
        atomic_json(contained_path(self.root, "active.json"), dict(format=1, fingerprint=fingerprint))

    def load_active(self):
        pointer = contained_path(self.root, "active.json")
        if not pointer.exists():
            return None
        data = read_json(pointer, maximum=4096)
        if not isinstance(data, dict) or data.get("format") != 1:
            raise ValueError("Invalid bound-model selection")
        fingerprint = data.get("fingerprint")
        path = self.path(fingerprint)
        bundle = GestureBundle.load(path)
        if model_fingerprint(bundle) != fingerprint:
            raise ValueError("Saved model content does not match its fingerprint")
        return bundle, path
