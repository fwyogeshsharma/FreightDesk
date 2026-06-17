"""Truck identity tracking: groups frame detections into per-truck events."""
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .config import Config
from .utils import compute_iou, box_center


@dataclass
class TruckEvent:
    event_id: str
    start_time: float
    source_video: str
    plate_candidates: Counter = field(default_factory=Counter)
    body_texts: List[Tuple[str, float]] = field(default_factory=list)
    class_votes: Counter = field(default_factory=Counter)  # YOLO class ids (5=bus, 7=truck)
    last_seen: float = 0.0
    last_bbox: Optional[Tuple[int, int, int, int]] = None
    frame_count: int = 0
    pass_index: int = 1  # filled in during cross-video merge
    # Representative photo for the broker app (best area x sharpness crop seen).
    # Not serialized to CSV; populated only when capture_crops is enabled.
    best_crop: Optional[object] = None
    best_crop_score: float = 0.0

    @property
    def best_plate(self) -> Optional[str]:
        if not self.plate_candidates:
            return None
        # Prefer the longest plate string among top candidates
        top_count = self.plate_candidates.most_common(1)[0][1]
        candidates = [p for p, c in self.plate_candidates.items() if c >= max(1, top_count - 1)]
        return max(candidates, key=len)

    def add_observation(self, timestamp: float, plate_texts: list,
                        body_texts: list, bbox: Tuple[int, int, int, int],
                        class_id: int = -1):
        self.last_seen = timestamp
        self.last_bbox = bbox
        self.frame_count += 1
        if class_id >= 0:
            self.class_votes[class_id] += 1
        for text, conf in plate_texts:
            norm = _normalize_plate(text)
            if norm:
                self.plate_candidates[norm] += 1
        self.body_texts.extend(body_texts)


def _normalize_plate(text: str) -> str:
    """Strip non-alphanumeric except hyphens/spaces; uppercase."""
    import re
    t = re.sub(r'[^A-Z0-9\- ]', '', text.upper().strip())
    t = re.sub(r'\s+', ' ', t).strip()
    return t if len(t) >= 2 else ""


def _levenshtein(a: str, b: str) -> int:
    """Pure-Python Levenshtein distance (fallback if library not installed)."""
    try:
        from Levenshtein import distance
        return distance(a, b)
    except ImportError:
        pass
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1,
                            prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


class TruckTracker:
    def __init__(self, config: Config):
        self.config = config
        self._active: List[TruckEvent] = []
        self._closed: List[TruckEvent] = []

    def update(self, timestamp: float, plate_texts: list, body_texts: list,
               vehicle_bbox: Tuple[int, int, int, int],
               class_id: int = -1) -> TruckEvent:
        cfg = self.config
        plate_norms = [_normalize_plate(t) for t, _ in plate_texts if _normalize_plate(t)]

        # Expire stale events
        still_active = []
        for ev in self._active:
            if timestamp - ev.last_seen > cfg.max_gap_seconds:
                self._closed.append(ev)
            else:
                still_active.append(ev)
        self._active = still_active

        # Try to match to an existing active event
        best_event: Optional[TruckEvent] = None
        best_score = 999

        for ev in self._active:
            # Plate match (highest priority)
            if plate_norms and ev.best_plate:
                for pn in plate_norms:
                    dist = _levenshtein(pn, ev.best_plate)
                    if dist <= cfg.plate_match_distance and dist < best_score:
                        best_event = ev
                        best_score = dist
                        break
                if best_event is ev:
                    continue

            # IoU match
            if ev.last_bbox:
                iou = compute_iou(vehicle_bbox, ev.last_bbox)
                if iou > cfg.tracker_iou_threshold:
                    score = 100 + int((1 - iou) * 100)  # lower = better
                    if score < best_score:
                        best_event = ev
                        best_score = score

        if best_event is None:
            best_event = TruckEvent(
                event_id=str(uuid.uuid4()),
                start_time=timestamp,
                source_video="",
                last_seen=timestamp,
            )
            self._active.append(best_event)

        best_event.add_observation(timestamp, plate_texts, body_texts, vehicle_bbox,
                                   class_id=class_id)
        return best_event

    def set_video_name(self, name: str):
        for ev in self._active:
            if not ev.source_video:
                ev.source_video = name

    def pop_newly_closed(self) -> List[TruckEvent]:
        """Return and clear events that have been closed since the last call.
        Call this after tracker.update() to get events ready to write immediately."""
        ready, self._closed = self._closed, []
        return ready

    def finalize(self) -> List[TruckEvent]:
        """Close all remaining active events and return everything."""
        self._closed.extend(self._active)
        self._active = []
        return self.pop_newly_closed()


def merge_cross_video_events(all_events: List[TruckEvent], config: Config) -> List[TruckEvent]:
    """
    Group events from different videos that share the same plate.
    If the same plate appears in different videos > cross_video_gap_minutes apart,
    treat as separate passes and assign pass_index.
    """
    gap_sec = config.cross_video_gap_minutes * 60

    # Build groups keyed by canonical plate
    groups: List[List[TruckEvent]] = []
    used = set()

    for i, ev in enumerate(all_events):
        if i in used:
            continue
        group = [ev]
        used.add(i)
        if ev.best_plate:
            for j, other in enumerate(all_events):
                if j in used:
                    continue
                if other.best_plate and _levenshtein(ev.best_plate, other.best_plate) <= config.plate_match_distance:
                    group.append(other)
                    used.add(j)
        groups.append(group)

    # Within each group, assign pass indices by time
    result = []
    for group in groups:
        group_sorted = sorted(group, key=lambda e: e.start_time)
        pass_idx = 1
        for k, ev in enumerate(group_sorted):
            ev.pass_index = pass_idx
            if k + 1 < len(group_sorted):
                next_ev = group_sorted[k + 1]
                if (next_ev.start_time - ev.last_seen) > gap_sec:
                    pass_idx += 1
            result.append(ev)

    return result
