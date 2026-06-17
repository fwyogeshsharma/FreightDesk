"""Vehicle detection gate using YOLOv8n + dedicated plate detector."""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .config import Config
from .utils import apply_roi, compute_iou

_PLATE_MODEL_PATH = Path(__file__).parent.parent / "models" / "best.pt"

# COCO class IDs — trucks and buses only (skip cars/motorcycles which don't carry text labels)
# COCO: 5=bus, 6=train, 7=truck — tanker trucks frequently classify as "train"
_VEHICLE_CLASSES = [5, 6, 7]


@dataclass
class BBox:
    x1: int
    y1: int
    x2: int
    y2: int
    conf: float
    class_id: int = -1

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)


@dataclass
class Detection:
    vehicle_box: BBox
    plate_box: Optional[BBox] = None  # reserved for future plate-detector integration


class Detector:
    def __init__(self, config: Config):
        self.config = config
        self._vehicle_model = None

    def _load_models(self):
        from ultralytics import YOLO
        if self._vehicle_model is None:
            self._vehicle_model = YOLO("yolov8n.pt")

    def detect(self, frame: np.ndarray) -> List[Detection]:
        self._load_models()
        cfg = self.config

        working_frame = frame
        offset = (0, 0)
        if cfg.truck_roi:
            working_frame, offset = apply_roi(frame, cfg.truck_roi)

        results = self._vehicle_model(
            working_frame,
            conf=cfg.yolo_vehicle_conf,
            classes=_VEHICLE_CLASSES,
            imgsz=640,   # YOLO resizes internally; passing full 2592x1944 wastes time
            verbose=False,
        )

        fh, fw = working_frame.shape[:2]
        min_area = (fw * fh) * 0.005   # ignore detections < 0.5% of frame area
        max_area = (fw * fh) * 0.95    # ignore detections > 95% of frame (OSD false-positives)

        detections: List[Detection] = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                area = max(0, x2 - x1) * max(0, y2 - y1)
                if not (min_area <= area <= max_area):
                    continue
                vbox = BBox(
                    x1 + offset[0], y1 + offset[1],
                    x2 + offset[0], y2 + offset[1],
                    float(box.conf[0]),
                    int(box.cls[0]),
                )
                detections.append(Detection(vbox))

        # Cross-class NMS: YOLO only suppresses within a class, so the same truck
        # can appear as truck+train+bus boxes. Keep the highest-conf one per region.
        detections.sort(key=lambda d: d.vehicle_box.conf, reverse=True)
        kept: List[Detection] = []
        for d in detections:
            if all(compute_iou(d.vehicle_box.as_tuple(), k.vehicle_box.as_tuple()) < 0.6
                   for k in kept):
                kept.append(d)
        return kept


class PlateDetector:
    """Dedicated license plate detector using a fine-tuned YOLOv8n model."""

    def __init__(self, conf: float = 0.25, imgsz: int = 1280):
        self._conf = conf
        self._imgsz = imgsz
        self._model = None

    def available(self) -> bool:
        return _PLATE_MODEL_PATH.exists()

    def _load(self):
        if self._model is None:
            from ultralytics import YOLO
            self._model = YOLO(str(_PLATE_MODEL_PATH))

    def detect(self, frame: np.ndarray) -> List[BBox]:
        """Return bounding boxes of all detected license plates in the frame."""
        self._load()
        results = self._model(frame, conf=self._conf, imgsz=self._imgsz, verbose=False)
        plates = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                fh, fw = frame.shape[:2]
                # Clamp to frame bounds
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(fw, x2), min(fh, y2)
                if (x2 - x1) > 10 and (y2 - y1) > 5:  # ignore tiny false positives
                    plates.append(BBox(x1, y1, x2, y2, float(box.conf[0])))
        return plates
