"""OCR engine abstraction supporting EasyOCR and PaddleOCR backends.

Strategy: run OCR on the full frame (downscaled for speed), then:
- Filter out camera OSD overlays (timestamps, brand watermarks)
- Classify remaining text as plate vs body using regex
- Associate text with vehicle bounding boxes by spatial proximity
"""
import re
from abc import ABC, abstractmethod
from typing import List, Tuple, Optional

import cv2
import numpy as np

from .config import Config
from .utils import blur_score


TextResult = Tuple[str, float]  # (text, confidence)

# Patterns for license plates — Indian focus (Rajasthan RJ-series), plus UK fallback
_PLATE_PATTERNS = [
    # Indian modern: RJ14CA1234 with optional spaces/hyphens between groups
    re.compile(r'^[A-Z]{2}[\s\-]?\d{2}[\s\-]?[A-Z]{1,2}[\s\-]?\d{4}$'),
    # Indian older / partial: RJ 14 C 1234
    re.compile(r'^[A-Z]{2}[\s\-]?\d{2}[\s\-]?[A-Z][\s\-]?\d{4}$'),
    # UK: AB12 XYZ
    re.compile(r'^[A-Z]{2}\d{2}\s?[A-Z]{3}$'),
    # Singapore: SBA1234A
    re.compile(r'^[A-Z]{3}\d{4}[A-Z]$'),
]

# Valid Indian state codes — if text starts with one of these it's almost certainly a plate
_INDIAN_STATE_RE = re.compile(
    r'^(AP|AR|AS|BR|CG|CH|DL|DN|GA|GJ|HR|HP|JH|JK|KA|KL|LA|LD|MH|ML|MN|MP|MZ|NL|OD|PB|PY|RJ|SK|TN|TR|TS|UK|UP|WB)',
    re.IGNORECASE
)

# Regex for camera OSD overlays to exclude (timestamps, brand names)
_OSD_PATTERNS = [
    re.compile(r'\d{4}[/\-]\d{2}'),                  # date fragment 2023/03
    re.compile(r'\d{2}\s*:\s*\d{2}'),                # time HH:MM (with optional spaces)
    re.compile(r'\d{2}[.]\d{2}[.]\d{2}'),            # time HH.MM.SS with dots
    re.compile(r'\d{2}[.]\d{2}\s*:'),                # time like 12.44:
    re.compile(r'^\d{1,2}[\s:]\s*\d{2}[\s:]\s*\d{2}'),  # HH:MM:SS variants
    re.compile(r'^20\d\d'),                           # starts with year 20xx
    re.compile(r'^I?PC$', re.IGNORECASE),             # IPC watermark (sometimes reads as "PC")
    re.compile(r'^ch\d+$', re.IGNORECASE),
    re.compile(r'^\d{1,2}[.:]\d{4,6}$'),              # clock fragment like 12.44303
]

# Max width for vehicle-crop OCR. Tested on real footage: at 800px phone numbers
# and company names on truck bodies garble; at 1280px they read correctly.
_VEHICLE_OCR_MAX_W = 1280


# Common truck words that happen to start with an Indian state code (TR, UP, KA...)
# or garble into one (CARRIER -> GARRIER, "GA" = Goa)
_PLATE_WORD_BLACKLIST = frozenset([
    'TRANSPORT', 'TRANSPORTS', 'TRAVELS', 'TRAILER', 'TRAILOR', 'TANKER',
    'GOODS', 'ARRIER', 'CARRIER', 'CARRIERS', 'ROADWAYS', 'ROADLINES',
    'LOGISTICS', 'UPDATE', 'KAPURTHALA', 'CHENNAI', 'HRSCHOOL', 'ASSAM',
    'GANDHI', 'ASHOK', 'ASHOKLEYLAND', 'LEYLAND', 'APOLLO', 'MAHINDRA',
])


def _looks_like_plate(text: str) -> bool:
    """Return True if text looks like a license plate.
    Uses strict regex for clean reads, falls back to state-code prefix for garbled OCR."""
    t_clean = re.sub(r'[^A-Z0-9]', '', text.upper())
    tu = text.upper().strip()
    # Substring match both ways: catches truncated OCR reads like "TRANSPOR"
    if len(t_clean) >= 5 and any(t_clean in w or w in t_clean
                                 for w in _PLATE_WORD_BLACKLIST):
        return False
    has_letters = bool(re.search(r'[A-Z]', t_clean))
    has_digits = bool(re.search(r'[0-9]', t_clean))
    # State code prefix + at least one digit -> almost certainly a plate, even when
    # OCR garbles some digits into letters (RJ14 -> RJIL). Letters-only strings are
    # NOT accepted: they're usually words (ASHOK, JAIPUR) and useless as plate data.
    if _INDIAN_STATE_RE.match(tu) and has_digits and 5 <= len(t_clean) <= 12:
        return True
    # General check: must have both letters AND digits
    if not (has_letters and has_digits and 5 <= len(t_clean) <= 11):
        return False
    return any(p.match(tu) for p in _PLATE_PATTERNS)


def _preprocess_plate(img: np.ndarray) -> np.ndarray:
    """CLAHE contrast enhancement to improve OCR on plate crops."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
    enhanced = clahe.apply(gray)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def _crop_vehicle(frame: np.ndarray, vbox_tuple: tuple, scale: float) -> Optional[np.ndarray]:
    """Crop a vehicle bbox from the full-res frame and downscale for OCR."""
    x1, y1, x2, y2 = vbox_tuple
    fh, fw = frame.shape[:2]
    crop = frame[max(0, y1):min(y2, fh), max(0, x1):min(x2, fw)]
    if crop.size == 0:
        return None
    ch, cw = crop.shape[:2]
    new_w, new_h = max(1, int(cw * scale)), max(1, int(ch * scale))
    return cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _is_osd(text: str) -> bool:
    """Return True if text looks like a camera OSD overlay (timestamp etc.)."""
    return any(p.search(text) for p in _OSD_PATTERNS)


class OCREngine(ABC):
    @abstractmethod
    def read_text(self, img: np.ndarray) -> List[Tuple[list, str, float]]:
        """Run OCR. Returns list of (bbox_points, text, confidence)."""


class EasyOCREngine(OCREngine):
    def __init__(self, config: Config):
        import easyocr
        langs = config.ocr_languages or ["en"]
        self._reader = easyocr.Reader(langs, gpu=False, verbose=False)
        self._conf_threshold = config.min_text_confidence
        self._min_len = config.min_text_length

    def read_text(self, img: np.ndarray) -> List[Tuple[list, str, float]]:
        results = self._reader.readtext(img, detail=1)
        return self._filter(results)

    def read_text_two_pass(self, img: np.ndarray) -> List[Tuple[list, str, float]]:
        """Locate text on a 640px downscale (detection is the slow stage),
        then recognize the found boxes at full resolution.
        ~3x faster than readtext() on large crops AND recognition quality of full res."""
        h, w = img.shape[:2]
        long_side = max(h, w)
        if long_side <= 700:
            return self.read_text(img)  # small crop — single pass is fine

        scale = 640.0 / long_side
        small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
        h_list, f_list = self._reader.detect(small)
        h_list, f_list = h_list[0], f_list[0]
        if not h_list and not f_list:
            return []

        inv = 1.0 / scale
        pad = 4
        boxes = []
        for (x_min, x_max, y_min, y_max) in h_list:
            boxes.append([max(0, int(x_min * inv) - pad), min(w, int(x_max * inv) + pad),
                          max(0, int(y_min * inv) - pad), min(h, int(y_max * inv) + pad)])
        polys = [[[int(x * inv), int(y * inv)] for (x, y) in poly] for poly in f_list]

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        results = self._reader.recognize(gray, horizontal_list=boxes, free_list=polys,
                                         detail=1)
        return self._filter(results)

    def _filter(self, results) -> List[Tuple[list, str, float]]:
        out = []
        for (bbox, text, conf) in results:
            text = str(text).strip()
            # Use a low floor (0.1) here — callers apply their own thresholds
            if conf >= 0.1 and len(text) >= self._min_len:
                out.append((bbox, text, float(conf)))
        return out


class PaddleOCREngine(OCREngine):
    def __init__(self, config: Config):
        from paddleocr import PaddleOCR
        lang = config.ocr_languages[0] if config.ocr_languages else "en"
        self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False,
                               det_db_thresh=0.3, rec_batch_num=1)
        self._conf_threshold = config.min_text_confidence
        self._min_len = config.min_text_length

    def read_text(self, img: np.ndarray) -> List[Tuple[list, str, float]]:
        results = self._ocr.ocr(img, det=True, rec=True, cls=True)
        out = []
        if not results or results[0] is None:
            return out
        for line in results[0]:
            bbox, (text, conf) = line
            text = str(text).strip()
            if conf >= self._conf_threshold and len(text) >= self._min_len:
                out.append((bbox, text, float(conf)))
        return out


def build_ocr_engine(config: Config) -> OCREngine:
    """Try PaddleOCR first; fall back to EasyOCR if unavailable."""
    backend = config.ocr_backend.lower()

    if backend in ("paddleocr", "auto"):
        try:
            return PaddleOCREngine(config)
        except Exception as e:
            if backend == "paddleocr":
                raise
            print(f"[OCR] PaddleOCR unavailable ({e}), falling back to EasyOCR")

    try:
        return EasyOCREngine(config)
    except Exception as e:
        raise RuntimeError(
            "Neither PaddleOCR nor EasyOCR could be loaded.\n"
            "Install one via: pip install paddleocr   OR   pip install easyocr"
        ) from e



class FrameOCR:
    """Runs OCR on each vehicle's bounding-box crop (full res, capped 1280px wide).
    One EasyOCR call per vehicle; dedicated plate crops OCR'd separately at full res."""

    def __init__(self, config: Config, engine: OCREngine):
        self.config = config
        self.engine = engine

    def process_frame(self, frame: np.ndarray, vehicle_boxes: list,
                      plate_boxes: list = None,
                      body_indices: list = None) -> List[dict]:
        """
        OCR strategy:
        - plate_boxes (from dedicated plate detector): OCR each crop at full res,
          assign to nearest vehicle. Cheap (small crops) — run every burst frame.
        - Vehicle-crop body OCR is expensive; if body_indices is given, only those
          vehicles get a body pass (callers gate this with a cooldown).
        Returns list of dicts with 'plate_texts' and 'body_texts' per vehicle.
        """
        if blur_score(frame) < self.config.blur_variance_threshold:
            return [{"plate_texts": [], "body_texts": []} for _ in vehicle_boxes]

        plate_conf_floor = max(0.15, self.config.min_text_confidence - 0.2)
        results = [{"plate_texts": [], "body_texts": []} for _ in vehicle_boxes]

        # --- Plate OCR: run on detected plate crops (high precision) ---
        for pbox in (plate_boxes or []):
            crop = _crop_vehicle(frame, pbox.as_tuple(), 1.0)  # full res
            if crop is None:
                continue
            ph, pw = crop.shape[:2]
            if pw < 200:  # upscale tiny plates so OCR can read them
                scale = 200 / pw
                crop = cv2.resize(crop, (200, max(1, int(ph * scale))),
                                  interpolation=cv2.INTER_CUBIC)
            # Assign this plate crop to the nearest vehicle by centre proximity
            px = (pbox.x1 + pbox.x2) / 2
            py = (pbox.y1 + pbox.y2) / 2
            best_i, best_d = 0, float("inf")
            for i, vbox in enumerate(vehicle_boxes):
                vx = (vbox.x1 + vbox.x2) / 2
                vy = (vbox.y1 + vbox.y2) / 2
                d = (px - vx) ** 2 + (py - vy) ** 2
                if d < best_d:
                    best_d, best_i = d, i
            for (_, text, conf) in self.engine.read_text(crop):
                if _is_osd(text):
                    continue
                # Digit-heavy reads (phone numbers near plates) get a lower floor
                digit_count = sum(c.isdigit() for c in text)
                floor = 0.12 if digit_count >= 7 else plate_conf_floor
                if conf < floor:
                    continue
                if _looks_like_plate(text):
                    results[best_i]["plate_texts"].append((text, conf))
                else:
                    # Full-res crop reads are high quality — keep non-plate text
                    # (phone numbers / company names often sit right next to plates)
                    results[best_i]["body_texts"].append((text, conf))

        # --- Body text OCR: vehicle crops at full res, capped at 1280px wide ---
        for i, vbox in enumerate(vehicle_boxes):
            if body_indices is not None and i not in body_indices:
                continue
            crop = _crop_vehicle(frame, vbox.as_tuple(), 1.0)
            if crop is None:
                continue
            if crop.shape[1] > _VEHICLE_OCR_MAX_W:
                sc = _VEHICLE_OCR_MAX_W / crop.shape[1]
                crop = cv2.resize(crop, (_VEHICLE_OCR_MAX_W,
                                         max(1, int(crop.shape[0] * sc))),
                                  interpolation=cv2.INTER_AREA)
            read = getattr(self.engine, 'read_text_two_pass', self.engine.read_text)
            for (_, text, conf) in read(crop):
                if _is_osd(text):
                    continue
                # Phone numbers painted on trucks often OCR at ~0.3 confidence —
                # allow digit-heavy reads through at a lower floor
                digit_count = sum(c.isdigit() for c in text)
                floor = 0.25 if digit_count >= 7 else self.config.min_text_confidence
                if conf < floor:
                    continue
                if not _looks_like_plate(text):
                    results[i]["body_texts"].append((text, conf))

        return results

    # Kept for backward compatibility
    def process(self, frame: np.ndarray, vehicle_box, plate_box=None) -> dict:
        res = self.process_frame(frame, [vehicle_box])
        return res[0]


