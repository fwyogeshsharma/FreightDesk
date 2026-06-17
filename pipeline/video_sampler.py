"""Adaptive frame sampler: base rate + motion-triggered burst."""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from .config import Config

log = logging.getLogger(__name__)

# Corrupt frames mid-file are recoverable (seek past them); give up only if a
# video has an absurd number of them.
_DECODE_FAIL_LIMIT = 200


@dataclass
class FrameRecord:
    frame_idx: int
    timestamp: float   # seconds from video start
    frame: np.ndarray
    video_name: str
    in_burst: bool = False  # True when motion spike detected (truck entering frame)


def sample_frames(video_path: str, config: Config,
                  start_sec: float = 0.0) -> Iterator[FrameRecord]:
    """Yield sampled frames from a video file.
    start_sec skips ahead before sampling begins (used for debugging)."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_name = Path(video_path).name

    base_interval = max(1, int(native_fps / config.sample_fps))
    burst_interval = max(1, int(native_fps / config.burst_fps))
    burst_len_frames = int(native_fps * config.burst_duration_sec)

    prev_small: np.ndarray | None = None
    frame_idx = 0
    next_sample = 0
    burst_end = -1
    decode_failures = 0

    if start_sec > 0:
        frame_idx = next_sample = int(start_sec * native_fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)

    while True:
        is_sample = frame_idx >= next_sample
        if is_sample:
            ok, frame = cap.read()
        else:
            # grab() advances the decoder without retrieving/copying the frame —
            # much cheaper for the majority of frames we skip
            ok = cap.grab()

        if not ok:
            # Either the true end of the video, or a corrupt frame mid-file.
            # Corrupt frames must not drop the rest of the video: seek past
            # them and keep decoding (verified recoverable on these videos).
            # Failures within the last second are just the unreadable DVR
            # file tail — treat as end-of-video.
            if frame_idx >= total_frames - native_fps:
                break
            decode_failures += 1
            if decode_failures > _DECODE_FAIL_LIMIT:
                log.warning(f"  {video_name}: giving up after {decode_failures} "
                            f"decode failures (at frame {frame_idx}, "
                            f"ts={frame_idx / native_fps:.1f}s)")
                break
            log.warning(f"  {video_name}: corrupt frame {frame_idx} "
                        f"(ts={frame_idx / native_fps:.1f}s) — skipping past it")
            frame_idx += 1
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            continue

        if not is_sample:
            frame_idx += 1
            continue

        # Motion detection on downscaled grayscale
        small = cv2.resize(frame, (320, 180))
        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)

        in_burst = frame_idx < burst_end
        if prev_small is not None:
            diff = float(np.mean(np.abs(gray_small - prev_small))) / 255.0
            if diff > config.motion_threshold:
                burst_end = frame_idx + burst_len_frames
                in_burst = True

        prev_small = gray_small

        ts = frame_idx / native_fps
        yield FrameRecord(frame_idx, ts, frame.copy(), video_name, in_burst=in_burst)

        interval = burst_interval if in_burst else base_interval
        next_sample = frame_idx + interval
        frame_idx += 1

    cap.release()


def video_info(video_path: str) -> dict:
    cap = cv2.VideoCapture(video_path)
    info = {
        "fps": cap.get(cv2.CAP_PROP_FPS),
        "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
    info["duration_sec"] = info["frame_count"] / info["fps"] if info["fps"] else 0
    cap.release()
    return info
