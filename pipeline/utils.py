import cv2
import numpy as np
from typing import Tuple


def compute_iou(box_a: Tuple[int, int, int, int], box_b: Tuple[int, int, int, int]) -> float:
    """Compute IoU between two (x1,y1,x2,y2) boxes."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def box_center(box: Tuple[int, int, int, int]) -> Tuple[float, float]:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def blur_score(img: np.ndarray) -> float:
    """Laplacian variance — higher = sharper."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def preprocess_plate(img: np.ndarray) -> np.ndarray:
    """Prepare a plate crop for OCR: upscale, CLAHE, binarize, deskew."""
    h, w = img.shape[:2]
    if w < 200:
        scale = 200.0 / w
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img.copy()

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Deskew via Hough lines
    edges = cv2.Canny(binary, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=30, minLineLength=w // 4, maxLineGap=5)
    if lines is not None and len(lines) > 0:
        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if x2 != x1:
                angles.append(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if angles:
            median_angle = np.median(angles)
            if abs(median_angle) < 15:
                M = cv2.getRotationMatrix2D((w / 2, gray.shape[0] / 2), median_angle, 1.0)
                binary = cv2.warpAffine(binary, M, (w, gray.shape[0]), flags=cv2.INTER_LINEAR,
                                        borderMode=cv2.BORDER_REPLICATE)

    # Convert back to BGR for OCR engines that expect color
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def preprocess_body(img: np.ndarray) -> np.ndarray:
    """Light enhancement for truck body text — keep color, just sharpen."""
    h, w = img.shape[:2]
    if h < 400:
        scale = 400.0 / h
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
    return cv2.filter2D(img, -1, kernel)


def crop_with_padding(frame: np.ndarray, box: Tuple[int, int, int, int],
                      padding: float = 0.0) -> np.ndarray:
    """Crop frame to box, expanding by padding fraction of box dimensions."""
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    px, py = int(bw * padding), int(bh * padding)
    cx1 = max(0, x1 - px)
    cy1 = max(0, y1 - py)
    cx2 = min(fw, x2 + px)
    cy2 = min(fh, y2 + py)
    return frame[cy1:cy2, cx1:cx2]


def apply_roi(frame: np.ndarray, roi: tuple) -> Tuple[np.ndarray, Tuple[int, int]]:
    """Crop frame to fractional ROI. Returns (cropped, (offset_x, offset_y))."""
    fh, fw = frame.shape[:2]
    x1 = int(roi[0] * fw)
    y1 = int(roi[1] * fh)
    x2 = int(roi[2] * fw)
    y2 = int(roi[3] * fh)
    return frame[y1:y2, x1:x2], (x1, y1)


def normalize_text(text: str) -> str:
    """Uppercase, collapse whitespace."""
    import re
    return re.sub(r'\s+', ' ', text.strip().upper())
