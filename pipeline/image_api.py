"""Build a single TruckEvent from several still photos of the same truck.

The still-image API accepts up to a handful of photos of ONE truck taken from
different angles. Each photo is run through the same detect -> plate -> OCR chain
as a video frame, and every reading is accumulated into ONE event (we bypass the
tracker's IoU matching — these are independent photos, not consecutive frames),
so the whole batch collapses to a single row.
"""
import uuid

from .detector import BBox
from .tracker import TruckEvent
from .utils import blur_score, crop_with_padding

MAX_IMAGES = 5


def build_event_from_images(images, detector, plate_detector, frame_ocr,
                            source_ref: str = "", capture_crop: bool = True) -> TruckEvent:
    """images: list of BGR numpy arrays (already decoded). Returns one TruckEvent
    aggregating plate/body text across all photos.
    capture_crop=False skips keeping a photo crop (mobile reports never store images)."""
    event = TruckEvent(event_id=str(uuid.uuid4()), start_time=0.0,
                       source_video=source_ref, last_seen=0.0)
    best_score = 0.0

    for idx, img in enumerate(images[:MAX_IMAGES]):
        if img is None or getattr(img, "size", 0) == 0:
            continue
        detections = detector.detect(img)
        if detections:
            # One truck per batch — focus on the largest (closest) vehicle in each
            # shot and ignore background vehicles.
            main = max(detections, key=lambda d: d.vehicle_box.area).vehicle_box
        else:
            # Unlike a video frame (which can contain many irrelevant objects, so a
            # vehicle-detection gate matters), a mobile report photo is always ONE
            # deliberately-photographed truck. A close-up shot of a door/plate/sticker
            # often doesn't look like "a vehicle" to YOLO — fall back to the full
            # frame instead of silently dropping the photo's OCR text.
            h, w = img.shape[:2]
            main = BBox(0, 0, w, h, 0.0, -1)
        plate_boxes = plate_detector.detect(img) if plate_detector else []
        ocr = frame_ocr.process_frame(img, [main],
                                      plate_boxes=plate_boxes, body_indices=[0])
        res = ocr[0] if ocr else {"plate_texts": [], "body_texts": []}
        event.add_observation(timestamp=float(idx),
                              plate_texts=res["plate_texts"],
                              body_texts=res["body_texts"],
                              bbox=main.as_tuple(),
                              class_id=main.class_id)

        if capture_crop:
            crop = crop_with_padding(img, main.as_tuple(), 0.05)
            if crop.size:
                score = main.area * blur_score(crop)
                if score > best_score:
                    best_score = score
                    event.best_crop = crop.copy()

    return event
