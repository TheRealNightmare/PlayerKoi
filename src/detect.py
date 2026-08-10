"""Wraps the exported NCNN YOLO model for chess piece detection.

Expects a model whose class names follow the "{color}-{piece}" convention,
e.g. "white-pawn", "black-knight" -- this is what board_state.py expects.
If your dataset uses different class names, add entries to NAME_MAP below
rather than changing board_state.py.
"""

import os

# Ultralytics re-checks its optional deps every time it loads an NCNN backend
# (nn/backends/ncnn.py calls check_requirements("ncnn") right after logging
# "Loading ... for NCNN inference..."). On the Pi that check goes out to the
# package index -- via piwheels, which is slow enough to look like a hang, with
# the CPU sitting idle. ncnn is already pinned in requirements.txt, so skip it.
os.environ.setdefault("ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS", "1")

NAME_MAP = {}


def load_model(model_path):
    from ultralytics import YOLO

    return YOLO(model_path, task="detect")


def _normalize(name):
    name = NAME_MAP.get(name, name)
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def detect(model, frame, conf=0.4, imgsz=640):
    """Runs inference on a single BGR frame. Returns a list of
    {"class": str, "confidence": float, "bbox": (x1, y1, x2, y2)}."""
    results = model.predict(frame, conf=conf, imgsz=imgsz, verbose=False)[0]

    detections = []
    for box in results.boxes:
        class_id = int(box.cls[0])
        class_name = _normalize(results.names[class_id])
        confidence = float(box.conf[0])
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        detections.append(
            {"class": class_name, "confidence": confidence, "bbox": (x1, y1, x2, y2)}
        )

    return detections
