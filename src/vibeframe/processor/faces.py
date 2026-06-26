"""Face detection for smart crop.

Uses OpenCV's bundled YuNet ONNX model (~230 KB, ships with the package).
Detection runs on a downscaled copy of the source — long side capped at
``_INPUT_MAX`` — so big NFS photos don't pay a multi-megapixel CNN pass.
A face's centre matters more than perfect bbox accuracy for cropping.

The detector is constructed lazily on first call and reused across images.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_MODEL_FILENAME = "face_yunet.onnx"
_INPUT_MAX = 640        # longer-side cap; ~50 ms detect on Pi 4 at this size
_SCORE_THRESHOLD = 0.6
_NMS_THRESHOLD = 0.3

_lock = threading.Lock()
_detector = None        # cv2.FaceDetectorYN | None
_detector_input: tuple[int, int] | None = None


def _model_path() -> Path | None:
    p = Path(__file__).with_name(_MODEL_FILENAME)
    return p if p.is_file() else None


def _get_detector(input_w: int, input_h: int):
    """Lazy-create the detector and update its input size if it changed."""
    global _detector, _detector_input
    try:
        import cv2
    except ImportError:
        return None
    with _lock:
        if _detector is None:
            mp = _model_path()
            if mp is None:
                log.warning("YuNet model not found at %s", _MODEL_FILENAME)
                return None
            try:
                _detector = cv2.FaceDetectorYN.create(  # type: ignore[attr-defined]
                    model=str(mp),
                    config="",
                    input_size=(input_w, input_h),
                    score_threshold=_SCORE_THRESHOLD,
                    nms_threshold=_NMS_THRESHOLD,
                    top_k=5000,
                )
                _detector_input = (input_w, input_h)
            except Exception as e:
                log.warning("YuNet init failed: %s", e)
                return None
        elif _detector_input != (input_w, input_h):
            _detector.setInputSize((input_w, input_h))
            _detector_input = (input_w, input_h)
        return _detector


def detect_faces(image_rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Return face bounding boxes (x, y, w, h) in the source image's pixel
    coordinates. Empty list if no faces, OpenCV missing, or detection fails.
    """
    try:
        import cv2
    except ImportError:
        return []
    if image_rgb.ndim != 3 or image_rgb.shape[2] != 3:
        return []

    h, w = image_rgb.shape[:2]
    scale = min(1.0, _INPUT_MAX / max(w, h))
    if scale < 1.0:
        rw = max(1, int(round(w * scale)))
        rh = max(1, int(round(h * scale)))
        resized = cv2.resize(image_rgb, (rw, rh), interpolation=cv2.INTER_AREA)
    else:
        rw, rh, resized = w, h, image_rgb
    bgr = cv2.cvtColor(resized, cv2.COLOR_RGB2BGR)
    det = _get_detector(rw, rh)
    if det is None:
        return []
    try:
        _ok, faces = det.detect(bgr)
    except Exception as e:
        log.warning("YuNet detect failed: %s", e)
        return []
    if faces is None or len(faces) == 0:
        return []
    inv = 1.0 / scale
    out: list[tuple[int, int, int, int]] = []
    for row in faces:
        fx, fy, fw, fh = row[0:4]
        out.append((
            int(fx * inv),
            int(fy * inv),
            int(fw * inv),
            int(fh * inv),
        ))
    return out
