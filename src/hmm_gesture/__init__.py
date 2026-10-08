# SPDX-License-Identifier: MPL-2.0
"""Load Studio exports and recognize gestures without GUI or Bluetooth dependencies."""
from .bundle import GestureBundle
from .config import AXES, PipelineConfig, SegmentationConfig
from .recognizer import GestureRecognizer, Prediction

__version__ = "0.2.0"
__all__ = ["AXES", "GestureBundle", "GestureRecognizer", "PipelineConfig", "Prediction", "SegmentationConfig"]
