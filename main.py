#!/usr/bin/env python3
"""
Truck Text Extraction Pipeline
Extracts all visible text from trucks in road camera videos.
Writes one .txt file per truck, named by license plate.

Usage:
    python main.py [--input VIDEO_OR_DIR] [--output DIR] [--fps 1.0] [--debug] [--workers 2]
"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Log to both console and a persistent log file in the project root
_LOG_FILE = Path(__file__).parent / "extractor.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Extract text from trucks in videos")
    parser.add_argument("--input", default=None,
                        help="Path to a single video file or directory of videos")
    parser.add_argument("--output", default=None, help="Output directory")
    parser.add_argument("--fps", type=float, default=None, help="Base sampling FPS (default: 1.0)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel video workers (default: 1)")
    parser.add_argument("--debug", action="store_true", help="Save debug crops to output/debug/")
    parser.add_argument("--backend", choices=["auto", "paddleocr", "easyocr"], default=None,
                        help="OCR backend (default: from config)")
    parser.add_argument("--roi", type=str, default=None,
                        help="ROI as x1,y1,x2,y2 fractions e.g. 0.0,0.3,1.0,1.0")
    parser.add_argument("--sink", choices=["csv", "db", "both"], default="both",
                        help="Where to write results: csv, db (PostgreSQL), or both (default)")
    return parser.parse_args()


def _fmt_hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def find_videos(path_str: str) -> list:
    p = Path(path_str)
    if p.is_file():
        return [str(p)]
    elif p.is_dir():
        videos = sorted(p.glob("*.mp4")) + sorted(p.glob("*.avi")) + sorted(p.glob("*.mov"))
        return [str(v) for v in videos]
    else:
        log.error(f"Input path not found: {path_str}")
        sys.exit(1)


def process_video(video_path: str, config, ocr_engine, plate_detector,
                  debug_dir: Path | None, csv_writer, use_tqdm: bool = True,
                  capture_crops: bool = False) -> int:
    """Process one video, writing rows to csv_writer as each truck event closes.
    capture_crops keeps the best (largest x sharpest) body photo per truck for the
    broker app — left off for plain CSV runs.
    Returns the count of truck events found."""
    import time
    from pipeline.video_sampler import sample_frames, video_info
    from pipeline.detector import Detector
    from pipeline.ocr_engine import FrameOCR
    from pipeline.tracker import TruckTracker
    from pipeline.utils import blur_score, crop_with_padding

    info = video_info(video_path)
    video_name = Path(video_path).name
    duration = info["duration_sec"]
    # Estimated sampled frames: base 1fps + burst overhead (measured ~2.2x on
    # this dense-traffic footage)
    est_frames = max(1, int(duration * config.sample_fps * 2.2))
    log.info(f"  Video: {video_name} | {duration/60:.1f} min | "
             f"{info['width']}x{info['height']} @ {info['fps']:.1f}fps | "
             f"~{est_frames} frames to process")
    t_start = time.time()

    detector = Detector(config)
    frame_ocr = FrameOCR(config, ocr_engine)
    tracker = TruckTracker(config)

    # Large vehicles (close to camera) carry the readable text and change fast —
    # OCR them roughly every other sampled burst frame. Small/far ones less often.
    # Tiny ones (<2% of frame) never have legible text — skip body OCR entirely.
    OCR_COOLDOWN_LARGE = 0.85  # < 2 burst intervals, so big trucks OCR every other sample
    OCR_COOLDOWN_SMALL = 1.5
    LARGE_AREA_FRAC = 0.08
    MIN_OCR_AREA_FRAC = 0.02
    frame_area = info["width"] * info["height"]
    _last_ocr_ts: dict = {}

    def _needs_ocr(vbox, ts: float) -> bool:
        if vbox.area < frame_area * MIN_OCR_AREA_FRAC:
            return False
        cx = (vbox.x1 + vbox.x2) // 2
        cy = (vbox.y1 + vbox.y2) // 2
        cell = (cx // 300, cy // 300)
        cooldown = (OCR_COOLDOWN_LARGE if vbox.area >= frame_area * LARGE_AREA_FRAC
                    else OCR_COOLDOWN_SMALL)
        if ts - _last_ocr_ts.get(cell, -999) >= cooldown:
            _last_ocr_ts[cell] = ts
            return True
        return False

    # Representative-photo capture: only prominent (close-to-camera) trucks make
    # good app thumbnails. Keep the single best area x sharpness crop per truck.
    CROP_MIN_AREA_FRAC = 0.06

    def _capture_crop(event, vbox, frame):
        if vbox.area < frame_area * CROP_MIN_AREA_FRAC:
            return
        crop = crop_with_padding(frame, vbox.as_tuple(), 0.05)
        if crop.size == 0:
            return
        score = vbox.area * blur_score(crop)
        if score > event.best_crop_score:
            event.best_crop_score = score
            event.best_crop = crop.copy()  # copy: crop is a view into the full frame

    progress = None
    if use_tqdm:
        try:
            from tqdm import tqdm
            progress = tqdm(desc=f"  {video_name[:30]}", unit="frame",
                            total=est_frames, leave=True, dynamic_ncols=True)
        except ImportError:
            pass

    total_written = 0
    n_frames = 0
    last_ts = 0.0

    for frame_rec in sample_frames(video_path, config):
        n_frames += 1
        last_ts = frame_rec.timestamp
        if progress is not None:
            progress.update(1)
            progress.set_postfix({"ts": f"{frame_rec.timestamp:.0f}s",
                                  "written": total_written})
        elif n_frames % 200 == 0:
            pct = frame_rec.timestamp / duration * 100 if duration else 0
            log.info(f"  {video_name}: {n_frames} frames | "
                     f"video @ {_fmt_hms(frame_rec.timestamp)} ({pct:.1f}%) | "
                     f"written={total_written}")

        detections = detector.detect(frame_rec.frame)
        if not detections:
            continue

        if frame_rec.in_burst:
            body_idx = [i for i, d in enumerate(detections)
                        if _needs_ocr(d.vehicle_box, frame_rec.timestamp)]
            # Plate detector runs on frames where we're OCRing anyway (its YOLO@1280
            # pass is too costly to run every burst frame)
            plate_boxes = (plate_detector.detect(frame_rec.frame)
                           if plate_detector and body_idx else [])
            if plate_boxes or body_idx:
                ocr_results = frame_ocr.process_frame(
                    frame_rec.frame,
                    [d.vehicle_box for d in detections],
                    plate_boxes=plate_boxes,
                    body_indices=body_idx,
                )
                ocr_map = {id(d): r for d, r in zip(detections, ocr_results)}
            else:
                ocr_map = {}
        else:
            ocr_map = {}

        for det in detections:
            ocr_result = ocr_map.get(id(det), {"plate_texts": [], "body_texts": []})

            if debug_dir and (ocr_result["plate_texts"] or ocr_result["body_texts"]):
                _save_debug(debug_dir, frame_rec, det, ocr_result)

            event = tracker.update(
                timestamp=frame_rec.timestamp,
                plate_texts=ocr_result["plate_texts"],
                body_texts=ocr_result["body_texts"],
                vehicle_bbox=det.vehicle_box.as_tuple(),
                class_id=det.vehicle_box.class_id,
            )
            if not event.source_video:
                event.source_video = video_name
            if capture_crops:
                _capture_crop(event, det.vehicle_box, frame_rec.frame)

        # Write any events that just closed (truck left frame and gap exceeded)
        newly_closed = tracker.pop_newly_closed()
        if newly_closed:
            # Progress through the video + pace-based estimate of time remaining
            frac = min(1.0, frame_rec.timestamp / duration) if duration else 0.0
            elapsed_video = time.time() - t_start
            row_progress = {'video_pct': f"{frac * 100:.1f}%"}
            if frac > 0.01:
                row_progress['est_remaining'] = _fmt_hms(elapsed_video * (1 - frac) / frac)
        for closed_event in newly_closed:
            if not closed_event.source_video:
                closed_event.source_video = video_name
            if csv_writer.write(closed_event, row_progress):
                plate = closed_event.best_plate or "UNKNOWN"
                log.info(f"  -> Wrote: {plate}  (frames={closed_event.frame_count})")
                total_written += 1

    if progress is not None:
        progress.close()

    # Flush the remaining active events at end of video. Report the actual
    # coverage reached — if decoding stopped early, don't claim 100%.
    final_frac = min(1.0, last_ts / duration) if duration else 1.0
    final_progress = {'video_pct': f"{final_frac * 100:.1f}%",
                      'est_remaining': '00:00:00'}
    for event in tracker.finalize():
        if not event.source_video:
            event.source_video = video_name
        if csv_writer.write(event, final_progress):
            plate = event.best_plate or "UNKNOWN"
            log.info(f"  -> Wrote: {plate}  (frames={event.frame_count})")
            total_written += 1

    elapsed = time.time() - t_start
    log.info(f"")
    log.info(f"  *** VIDEO COMPLETE: {video_name} ***")
    log.info(f"  *** {total_written} trucks written | took {elapsed/60:.1f} min ***")
    log.info(f"  *** covered {_fmt_hms(last_ts)} of {_fmt_hms(duration)} "
             f"({final_frac * 100:.1f}%) ***")
    if final_frac < 0.98:
        log.warning(f"  Video ended {_fmt_hms(duration - last_ts)} early — "
                    f"check the log above for decode failures.")
    log.info(f"")
    return total_written


def _save_debug(debug_dir: Path, frame_rec, det, ocr_result):
    """Save vehicle and plate crops for manual inspection."""
    import cv2
    from pipeline.utils import crop_with_padding

    ts = int(frame_rec.timestamp)
    stem = f"{frame_rec.video_name.replace('.mp4', '')}_{ts:05d}"

    v_crop = crop_with_padding(frame_rec.frame, det.vehicle_box.as_tuple(), 0.05)
    if v_crop.size > 0:
        cv2.imwrite(str(debug_dir / f"{stem}_vehicle.jpg"), v_crop)

    if det.plate_box:
        p_crop = crop_with_padding(frame_rec.frame, det.plate_box.as_tuple(), 0.15)
        if p_crop.size > 0:
            cv2.imwrite(str(debug_dir / f"{stem}_plate.jpg"), p_crop)

            plate_texts = [t for t, _ in ocr_result["plate_texts"]]
            if plate_texts:
                (debug_dir / f"{stem}_plate.txt").write_text(
                    "\n".join(plate_texts), encoding="utf-8"
                )


class _MultiWriter:
    """Fan one closed event out to several sinks (CSV + database)."""

    def __init__(self, writers: list):
        self._writers = writers

    def write(self, event, progress=None) -> bool:
        wrote = False
        for w in self._writers:
            if w.write(event, progress):
                wrote = True
        return wrote

    def close(self):
        for w in self._writers:
            if hasattr(w, "close"):
                w.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _WorkerWriter:
    """Sink used inside a worker process. Writes to the database directly (Postgres
    is concurrency-safe) and/or ships events to the parent's CSV writer via a queue."""

    def __init__(self, db_writer=None, queue=None, counter=None):
        self._db = db_writer
        self._q = queue
        self._counter = counter  # shared tally, used only when there's no CSV queue

    def write(self, event, progress=None) -> bool:
        wrote = False
        if self._db is not None and self._db.write(event, progress):
            wrote = True
        if self._q is not None:
            from pipeline.extract import is_noise_event
            if not is_noise_event(event):
                event.best_crop = None  # don't pickle the full-res crop across the queue
                self._q.put((event, progress))
                wrote = True
        elif wrote and self._counter is not None:
            with self._counter.get_lock():
                self._counter.value += 1
        return wrote


def _video_worker(video_path: str, config, debug_dir, queue, db_active: bool,
                  crops_dir: str, counter=None):
    """Process one video in a child process. Models are loaded per-process."""
    import os
    from pipeline.ocr_engine import build_ocr_engine
    from pipeline.detector import PlateDetector

    # Split CPU threads across workers — two processes each grabbing all
    # cores thrash the caches and run slower than half-and-half.
    threads = max(1, (os.cpu_count() or 8) // max(1, config.workers))
    try:
        import torch
        torch.set_num_threads(threads)
    except ImportError:
        pass
    try:
        import cv2
        cv2.setNumThreads(threads)
    except ImportError:
        pass

    log.info(f"[worker] Loading models for {Path(video_path).name}...")
    ocr_engine = build_ocr_engine(config)
    plate_detector = PlateDetector()
    if not plate_detector.available():
        plate_detector = None

    db_writer = None
    if db_active:
        from pipeline.db import reset_engine, SourceType
        from pipeline.db_writer import TruckDBWriter
        reset_engine()  # spawned process: build its own engine, don't inherit parent's
        db_writer = TruckDBWriter(source=SourceType.video, crops_dir=None,
                                  require_phone=True)

    writer = _WorkerWriter(db_writer=db_writer, queue=queue, counter=counter)
    # Photos are never stored — process frames, keep only the extracted data.
    process_video(video_path, config, ocr_engine, plate_detector,
                  debug_dir, writer, use_tqdm=False, capture_crops=False)


def _run_parallel(video_files: list, config, debug_dir, output_dir: Path,
                  sinks: set) -> int:
    """Process videos across config.workers child processes. With a CSV sink the
    parent owns the file (rows stay incremental, ids sequential); the DB sink is
    written by each worker directly since Postgres handles concurrency."""
    import multiprocessing as mp
    import time
    from queue import Empty

    csv_active = "csv" in sinks
    db_active = "db" in sinks
    crops_dir = None  # photos are never stored

    ctx = mp.get_context("spawn")
    queue = ctx.Queue() if csv_active else None
    # DB-only mode has no parent-side write loop to tally, so workers report via
    # a shared counter.
    counter = ctx.Value("i", 0) if (db_active and not csv_active) else None
    pending = list(video_files)
    running: list = []
    total = 0

    csv_writer = None
    if csv_active:
        from pipeline.writer import TruckCSVWriter
        csv_writer = TruckCSVWriter(str(output_dir))

    def _spawn_due():
        nonlocal running
        running = [p for p in running if p.is_alive()]
        while pending and len(running) < config.workers:
            video_path = pending.pop(0)
            p = ctx.Process(target=_video_worker,
                            args=(video_path, config, debug_dir, queue,
                                  db_active, crops_dir, counter),
                            daemon=True)
            p.start()
            running.append(p)
            log.info(f"Worker {p.pid} started: {Path(video_path).name}")

    try:
        while pending or running:
            _spawn_due()
            if queue is not None:
                try:
                    event, row_progress = queue.get(timeout=1.0)
                except Empty:
                    continue
                if csv_writer.write(event, row_progress):
                    total += 1
            else:
                time.sleep(0.5)  # DB-only: workers self-write, just await completion

        # Workers done — drain anything still buffered for the CSV writer
        if queue is not None:
            while True:
                try:
                    event, row_progress = queue.get(timeout=0.5)
                except Empty:
                    break
                if csv_writer.write(event, row_progress):
                    total += 1
    finally:
        if csv_writer is not None:
            csv_writer.close()

    if counter is not None:
        total = counter.value
    return total


def main():
    args = parse_args()

    log.info("=" * 60)
    log.info(f"EXTRACTOR RUN STARTED  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    from pipeline.config import Config
    config = Config()

    if args.input:
        config.input_dir = args.input if Path(args.input).is_dir() else str(Path(args.input).parent)
    if args.output:
        config.output_dir = args.output
    if args.fps:
        config.sample_fps = args.fps
    if args.workers:
        config.workers = args.workers
    if args.debug:
        config.debug = True
    if args.backend:
        config.ocr_backend = args.backend
    if args.roi:
        parts = [float(x) for x in args.roi.split(",")]
        config.truck_roi = tuple(parts)

    # Resolve input videos
    input_path = args.input or config.input_dir
    video_files = find_videos(input_path)
    if not video_files:
        log.error(f"No video files found in: {input_path}")
        sys.exit(1)
    log.info(f"Found {len(video_files)} video(s) to process")

    # Set up output directory
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = None
    if config.debug:
        debug_dir = output_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

    # Resolve output sinks (CSV file and/or PostgreSQL).
    sinks = {"csv", "db"} if args.sink == "both" else {args.sink}
    csv_path = output_dir / "trucks.csv"
    if "csv" in sinks:
        log.info(f"Output CSV: {csv_path}")
    if "db" in sinks:
        from pipeline.db import init_db, database_url
        try:
            init_db()
            log.info(f"Output DB: {database_url()}")
        except Exception as e:
            log.error(f"Cannot reach PostgreSQL ({e}).")
            log.error("Start Postgres / set DATABASE_URL, or run with --sink csv.")
            sys.exit(1)

    if config.workers > 1 and len(video_files) > 1:
        n = min(config.workers, len(video_files))
        log.info(f"Parallel mode: {n} videos at a time "
                 f"(each worker loads its own models)")
        config.workers = n
        total = _run_parallel(video_files, config, debug_dir, output_dir, sinks)
    else:
        # Load OCR engine once (heavy startup cost)
        log.info("Loading OCR engine...")
        from pipeline.ocr_engine import build_ocr_engine
        ocr_engine = build_ocr_engine(config)
        log.info("OCR engine ready.")

        from pipeline.detector import PlateDetector

        plate_detector = PlateDetector()
        if plate_detector.available():
            log.info("Plate detector model found — using dedicated plate detection.")
        else:
            log.info("Plate detector model not found — plate detection via regex only.")
            plate_detector = None

        # Build the sink(s). DB writes also capture a representative truck photo.
        writers = []
        if "csv" in sinks:
            from pipeline.writer import TruckCSVWriter
            writers.append(TruckCSVWriter(str(output_dir)))
        if "db" in sinks:
            from pipeline.db import SourceType
            from pipeline.db_writer import TruckDBWriter
            writers.append(TruckDBWriter(source=SourceType.video, crops_dir=None,
                                         require_phone=True))
        capture_crops = False  # photos are never stored — only extracted data is kept
        writer = writers[0] if len(writers) == 1 else _MultiWriter(writers)

        total = 0
        with writer:
            for video_path in video_files:
                log.info(f"Processing: {Path(video_path).name}")
                count = process_video(video_path, config, ocr_engine, plate_detector,
                                      debug_dir, writer, capture_crops=capture_crops)
                log.info(f"  -> {count} truck(s) written from this video")
                total += count

    dest = " + ".join(sorted(sinks))
    log.info(f"Done. {total} truck events written to: {dest}")
    log.info(f"Log file: {_LOG_FILE}")
    log.info("=" * 60)
    log.info(f"EXTRACTOR RUN COMPLETE  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
