from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, List

# Project root (the folder containing pipeline/). Keeps defaults independent of where
# the project lives or what the folder is named.
_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # Paths (relative to the project root by default; override via CLI/env)
    input_dir: str = str(_ROOT / "videos")
    output_dir: str = str(_ROOT / "output")
    debug: bool = False

    # Frame sampling
    sample_fps: float = 1.0
    motion_threshold: float = 0.015
    burst_fps: float = 2.2  # 11-frame interval @25fps (0.44s) — odd interval de-aliases against text-legibility windows
    burst_duration_sec: float = 3.0

    # Detection
    yolo_vehicle_conf: float = 0.1  # big trucks near the camera score as low as 0.1; area filter + class gate handle junk
    yolo_plate_conf: float = 0.35
    plate_padding: float = 0.15
    # Optional (x1_frac, y1_frac, x2_frac, y2_frac) to pre-crop frame to road lane
    truck_roi: Optional[Tuple[float, float, float, float]] = None

    # OCR
    ocr_backend: str = "easyocr"  # "paddleocr", "easyocr", or "auto"
    ocr_languages: List[str] = field(default_factory=lambda: ["en"])
    min_text_confidence: float = 0.4
    min_text_length: int = 2
    blur_variance_threshold: float = 50.0

    # Tracking / grouping
    plate_match_distance: int = 3
    tracker_iou_threshold: float = 0.3
    max_gap_seconds: float = 4.0  # trucks clear the frame in ~5s; 8s chained consecutive trucks into one event
    cross_video_gap_minutes: float = 30.0

    # Output
    unknown_prefix: str = "Unknown"
    workers: int = 1  # parallel video workers (set to 2 for dual-core processing)
