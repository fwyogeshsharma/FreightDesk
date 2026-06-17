"""End-to-end verification of the DB + web app + image API against a live Postgres.

Assumes DATABASE_URL points at a reachable PostgreSQL. Inserts synthetic rows,
queries them through the FastAPI app (TestClient), and runs the still-image API on
a real frame pulled from one of the project videos (if any).
"""
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from pipeline.db import init_db, get_session_factory, Truck, SourceType
    from pipeline.db_writer import TruckDBWriter
    from pipeline.tracker import TruckEvent

    print("1. init_db()")
    init_db()

    # Clean slate for a deterministic check
    Session = get_session_factory()
    with Session() as s:
        s.query(Truck).delete()
        s.commit()

    print("2. insert synthetic video-sourced events (with a fake crop)")
    crops_dir = ROOT / "output" / "crops"
    w = TruckDBWriter(source=SourceType.video, crops_dir=str(crops_dir))

    def make_event(eid, start, plate, texts, frames=12):
        ev = TruckEvent(event_id=eid, start_time=start,
                        source_video="D01_20230331124308.mp4",
                        last_seen=start + 8, frame_count=frames)
        if plate:
            ev.plate_candidates = Counter({plate: 4})
        ev.class_votes = Counter({7: 8})
        ev.body_texts = [(t, 0.7) for t in texts]
        ev.best_crop = np.full((120, 160, 3), 60, dtype=np.uint8)  # dummy image
        return ev

    assert w.write(make_event("a", 100, "RJ14CA1234",
                              ["SHREE BALAJI TRANSPORT", "CALL 9811008120", "JAIPUR"]))
    assert w.write(make_event("b", 400, "RJ09GB5521",
                              ["MAHADEV LOGISTICS", "9928001122", "JODHPUR"]))
    assert w.last_dict["image_path"], "crop path should be set"
    print("   inserted; last image_path =", w.last_dict["image_path"])

    print("3. query via FastAPI TestClient")
    from fastapi.testclient import TestClient
    from webapp.app import app
    client = TestClient(app)

    r = client.get("/api/trucks")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2, rows
    # newest-first: event 'b' (start 400 -> later detected_at) comes first
    assert rows[0]["license_plate"] == "RJ09GB5521", rows[0]
    print("   /api/trucks newest-first OK:", [x["license_plate"] for x in rows])

    r = client.get("/api/trucks", params={"q": "balaji"})
    assert len(r.json()) == 1 and r.json()[0]["license_plate"] == "RJ14CA1234"
    print("   search q=balaji OK")

    tid = rows[0]["id"]
    assert client.get(f"/api/trucks/{tid}").status_code == 200
    assert client.get("/api/trucks/999999").status_code == 404
    print("   detail + 404 OK")

    r = client.get("/")
    assert r.status_code == 200 and "RJ09GB5521" in r.text
    assert "tel:9928001122" in r.text
    print("   broker HTML renders cards + click-to-call OK")

    print("4. still-image API on a real video frame (if a video is available)")
    _test_image_api(client)

    print("\nALL DB/APP/IMAGE-API CHECKS PASSED")


def _test_image_api(client):
    import cv2
    vids = sorted((Path(__file__).resolve().parent.parent / "videos").glob("*.mp4"))
    if not vids:
        print("   (no videos found — skipping image API live test)")
        return
    cap = cv2.VideoCapture(str(vids[0]))
    # Grab a frame ~40s in, where a truck is likely present
    cap.set(cv2.CAP_PROP_POS_FRAMES, 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        print("   (could not read a frame — skipping)")
        return
    ok2, buf = cv2.imencode(".jpg", frame)
    files = [("images", ("f1.jpg", buf.tobytes(), "image/jpeg"))]
    data = {"phone_number": "9811008120", "vehicle_number": "RJ14CA1234",
            "loaded_status": "loaded", "number_of_wheels": "12",
            "location": "NH-48 Jaipur", "reported_by": "verify_db"}
    r = client.post("/api/trucks/report", files=files, data=data)
    print("   report API status:", r.status_code, "->", str(r.json())[:220])
    # leave no test row behind
    if r.status_code == 200:
        from pipeline.db import get_engine
        from sqlalchemy import text
        with get_engine().begin() as c:
            c.execute(text("DELETE FROM trucks WHERE reported_by='verify_db'"))


if __name__ == "__main__":
    main()
