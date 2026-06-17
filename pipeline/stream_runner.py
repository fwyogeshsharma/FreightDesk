"""Live-stream ingestion: pull frames from an RTSP/HTTP camera and feed the same
detect -> plate -> OCR -> track -> DB chain used for video files.

Differences from file processing: the source is unbounded, timestamps are
wall-clock (a stream sighting happens "now"), and we reconnect on dropout.

Run:  python -m pipeline.stream_runner rtsp://user:pass@host/stream
(or use run_stream.bat)
"""
import logging
import time

import cv2

from .config import Config
from .detector import Detector
from .ocr_engine import build_ocr_engine, FrameOCR
from .tracker import TruckTracker
from .utils import blur_score, crop_with_padding
import numpy as np

log = logging.getLogger(__name__)


def run_stream(url: str, config: Config, writer, stop_check=None,
               reconnect_delay: float = 3.0):
    """Process a live stream until stop_check() returns True (or forever).
    writer: any object with .write(event, progress) — typically a TruckDBWriter
    built with source=SourceType.stream."""
    detector = Detector(config)
    plate_detector = _maybe_plate_detector()
    ocr_engine = build_ocr_engine(config)
    frame_ocr = FrameOCR(config, ocr_engine)
    tracker = TruckTracker(config)

    base_interval = 1.0 / max(0.1, config.sample_fps)      # seconds between base samples
    burst_interval = 1.0 / max(0.1, config.burst_fps)
    written = 0
    prev_small = None
    burst_until = 0.0
    last_sample = 0.0
    last_ocr = {}
    OCR_COOLDOWN = 0.85
    t0 = time.monotonic()

    cap = cv2.VideoCapture(url)
    log.info(f"Stream opened: {url}")

    while stop_check is None or not stop_check():
        ok, frame = cap.read()
        if not ok or frame is None:
            log.warning("Stream read failed — reconnecting…")
            cap.release()
            time.sleep(reconnect_delay)
            cap = cv2.VideoCapture(url)
            continue

        now = time.monotonic()
        ts = now - t0

        # Motion-gated sampling (same idea as the file sampler)
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        in_burst = now < burst_until
        if prev_small is not None:
            diff = float(np.mean(np.abs(gray - prev_small))) / 255.0
            if diff > config.motion_threshold:
                burst_until = now + config.burst_duration_sec
                in_burst = True
        prev_small = gray

        interval = burst_interval if in_burst else base_interval
        if now - last_sample < interval:
            continue
        last_sample = now

        detections = detector.detect(frame)
        if not detections:
            written += _flush(tracker, writer)
            continue

        frame_area = frame.shape[0] * frame.shape[1]
        body_idx = []
        for i, d in enumerate(detections):
            vb = d.vehicle_box
            if vb.area < frame_area * 0.02:
                continue
            cx = (vb.x1 + vb.x2) // 2 // 300
            cy = (vb.y1 + vb.y2) // 2 // 300
            if now - last_ocr.get((cx, cy), -999) >= OCR_COOLDOWN:
                last_ocr[(cx, cy)] = now
                body_idx.append(i)

        ocr_map = {}
        if in_burst and body_idx:
            plate_boxes = plate_detector.detect(frame) if plate_detector else []
            results = frame_ocr.process_frame(
                frame, [d.vehicle_box for d in detections],
                plate_boxes=plate_boxes, body_indices=body_idx)
            ocr_map = {id(d): r for d, r in zip(detections, results)}

        for det in detections:
            res = ocr_map.get(id(det), {"plate_texts": [], "body_texts": []})
            event = tracker.update(timestamp=ts,
                                   plate_texts=res["plate_texts"],
                                   body_texts=res["body_texts"],
                                   vehicle_bbox=det.vehicle_box.as_tuple(),
                                   class_id=det.vehicle_box.class_id)
            if not event.source_video:
                event.source_video = "stream"
            _capture(event, det.vehicle_box, frame, frame_area)

        written += _flush(tracker, writer)

    # Drain on shutdown
    for ev in tracker.finalize():
        if writer.write(ev):
            written += 1
    cap.release()
    log.info(f"Stream stopped. {written} trucks written.")
    return written


def _flush(tracker, writer) -> int:
    n = 0
    for ev in tracker.pop_newly_closed():
        if writer.write(ev):
            n += 1
    return n


def _capture(event, vbox, frame, frame_area):
    if vbox.area < frame_area * 0.06:
        return
    crop = crop_with_padding(frame, vbox.as_tuple(), 0.05)
    if crop.size == 0:
        return
    score = vbox.area * blur_score(crop)
    if score > event.best_crop_score:
        event.best_crop_score = score
        event.best_crop = crop.copy()


def _maybe_plate_detector():
    from .detector import PlateDetector
    pd = PlateDetector()
    return pd if pd.available() else None


def main():
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Ingest a live camera stream into the DB")
    ap.add_argument("url", help="RTSP/HTTP stream URL")
    args = ap.parse_args()

    from .db import init_db, SourceType
    from .db_writer import TruckDBWriter
    config = Config()
    init_db()
    # No photos are stored; require a phone number so every record is callable.
    writer = TruckDBWriter(source=SourceType.stream, crops_dir=None, require_phone=True)
    run_stream(args.url, config, writer)


if __name__ == "__main__":
    main()
