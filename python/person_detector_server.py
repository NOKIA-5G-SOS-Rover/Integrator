#!/usr/bin/env python3
"""
Person detector service (integrator copy).

This adapts the YOLO detector server to directly notify the cloud backend
when a person first appears and upload a snapshot for the generated event.

Usage: set environment variables or pass defaults below.

Environment variables:
  BACKEND_URL  - API base URL (default: http://localhost:5000)
  ROVER_ID     - rover identifier (default: ROVER-Q1)
  CAMERA_ID    - camera identifier (default: CAM-01)

This script is a lightly modified copy of the ai-ml/person_detector_server.py
with an integrated HTTP bridge for event creation + image upload.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

import cv2
import numpy as np
import requests
from flask import Flask, Response, jsonify, render_template_string, request as flask_request

try:
    from ultralytics import YOLO
except ImportError as exc:
    raise SystemExit("Missing ultralytics. Run: pip install ultralytics") from exc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("integrator-person-detector")

PERSON_CLASS_ID = 0


@dataclass(frozen=True)
class SignalConfig:
    target_pid: Optional[int]
    signal_number: int
    signal_name: str
    detection_env: str
    status_file: Optional[str]


@dataclass
class Detection:
    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float


@dataclass
class AppState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = True

    latest_frame: Optional[np.ndarray] = None
    display_frame: Optional[np.ndarray] = None
    detections: List[Detection] = field(default_factory=list)

    fps_capture: float = 0.0
    fps_inference: float = 0.0
    fps_stream: float = 0.0

    person_present: bool = False
    person_count: int = 0
    signal_count: int = 0
    last_signal_error: Optional[str] = None
    signal_target_pid: Optional[int] = None


def open_camera(device: int, width: int, height: int, fps: int) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera device index {device}")

    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    log.info("Camera ready: %dx%d @ %.1f FPS", actual_w, actual_h, actual_fps)
    return cap


def capture_worker(cap: cv2.VideoCapture, state: AppState) -> None:
    count = 0
    t0 = time.perf_counter()

    while state.running:
        ok, frame = cap.read()
        if not ok:
            log.warning("Camera read failed, retrying...")
            time.sleep(0.05)
            continue

        count += 1
        now = time.perf_counter()
        if now - t0 >= 1.0:
            with state.lock:
                state.fps_capture = count / (now - t0)
            count = 0
            t0 = now

        with state.lock:
            state.latest_frame = frame


def write_detection_status(status_file: Optional[str], detection_env: str, person_present: bool, person_count: int) -> None:
    value = "1" if person_present else "0"
    os.environ[detection_env] = value
    os.environ[f"{detection_env}_COUNT"] = str(person_count)

    if not status_file:
        return

    status_path = os.path.abspath(status_file)
    status_dir = os.path.dirname(status_path)
    os.makedirs(status_dir, exist_ok=True)

    temporary_path = f"{status_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        handle.write(f"{detection_env}={value}\n")
        handle.write(f"{detection_env}_COUNT={person_count}\n")
        handle.write(f"PERSON_DETECTOR_PID={os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary_path, status_path)


def send_event_and_image(backend_url: str, rover_id: str, camera_id: str, detection: Detection, frame: np.ndarray) -> None:
    """Create event on backend and upload snapshot image."""
    try:
        payload = {
            "roverId": rover_id,
            "sessionId": "integrator-session",
            "alertType": "Human Detected",
            "source": "YOLOv8-Camera",
            "detectedAt": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            "locationX": float(detection.x1),
            "locationY": float(detection.y1),
            "boundingBoxWidth": max(float(detection.x2 - detection.x1), 0.1),
            "boundingBoxHeight": max(float(detection.y2 - detection.y1), 1.0),
            "confidenceScore": float(detection.confidence),
            "motorHaltRequested": True,
            "injuryClass": "Unknown",
            "cameraId": camera_id,
            "status": "NEW",
        }

        res = requests.post(f"{backend_url}/events", json=payload, timeout=5)
        if res.status_code not in (200, 201):
            log.error("Backend rejected event: %s %s", res.status_code, res.text)
            return

        event = res.json()
        event_id = event.get("id") or event.get("Id")
        if not event_id:
            log.error("Backend did not return event id: %s", event)
            return

        # Encode the frame to JPEG
        ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            log.error("Failed to encode image for upload")
            return

        files = {"image": ("snapshot.jpg", io.BytesIO(buf.tobytes()), "image/jpeg")}
        upload_res = requests.post(f"{backend_url}/events/{event_id}/image", files=files, timeout=10)
        if upload_res.status_code not in (200, 201):
            log.error("Image upload failed: %s %s", upload_res.status_code, upload_res.text)
            return

        log.info("Uploaded snapshot for event %s", event_id)
    except Exception as exc:
        log.exception("Error sending event/image to backend: %s", exc)


def notify_detection_change(state: AppState, config: SignalConfig, person_count: int, backend_url: str, rover_id: str, camera_id: str) -> None:
    person_present = person_count > 0

    with state.lock:
        previous_person_present = state.person_present
        previous_person_count = state.person_count
        state.person_present = person_present
        state.person_count = person_count

    if (person_present != previous_person_present) or (person_count != previous_person_count):
        try:
            write_detection_status(status_file=config.status_file, detection_env=config.detection_env, person_present=person_present, person_count=person_count)
        except OSError as exc:
            log.error("Could not update detection status: %s", exc)

    # On rising edge, create event + upload image
    if person_present and not previous_person_present:
        with state.lock:
            # take a safe copy of the frame and detections
            frame_copy = state.latest_frame.copy() if state.latest_frame is not None else None
            detections_copy = list(state.detections)

        if not detections_copy:
            log.warning("Rising edge but no detections available to attach image")
            return

        # choose the first detection
        det = detections_copy[0]

        # spawn a thread to avoid blocking inference loop
        if frame_copy is not None:
            threading.Thread(target=send_event_and_image, args=(backend_url, rover_id, camera_id, det, frame_copy), daemon=True).start()

    # Original signal behavior preserved for compatibility with local processes
    if not person_present or previous_person_present:
        return

    if config.target_pid is None:
        log.warning("Person detected, but no signal target configured; %s was not sent.", config.signal_name)
        return

    try:
        os.kill(config.target_pid, config.signal_number)
    except ProcessLookupError:
        message = f"Target PID {config.target_pid} does not exist"
        with state.lock:
            state.last_signal_error = message
        log.error("%s; could not send %s.", message, config.signal_name)
    except PermissionError:
        message = f"Permission denied for target PID {config.target_pid}"
        with state.lock:
            state.last_signal_error = message
        log.error("%s; could not send %s.", message, config.signal_name)
    except OSError as exc:
        message = str(exc)
        with state.lock:
            state.last_signal_error = message
        log.error("Could not send %s to PID %d: %s", config.signal_name, config.target_pid, exc)
    else:
        with state.lock:
            state.signal_count += 1
            state.last_signal_error = None
        log.info("Person detected: sent %s to PID %d.", config.signal_name, config.target_pid)


def inference_worker(model: YOLO, state: AppState, imgsz: int, conf: float, device: str, signal_config: SignalConfig, backend_url: str, rover_id: str, camera_id: str) -> None:
    count = 0
    t0 = time.perf_counter()

    while state.running:
        loop_start = time.perf_counter()

        with state.lock:
            frame = state.latest_frame
            if frame is None:
                frame_copy = None
            else:
                frame_copy = frame.copy()

        if frame_copy is None:
            time.sleep(0.01)
            continue

        results = model.predict(source=frame_copy, imgsz=imgsz, conf=conf, classes=[PERSON_CLASS_ID], verbose=False, device=device)

        detections: List[Detection] = []
        if results and results[0].boxes is not None:
            for box in results[0].boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                score = float(box.conf[0])
                detections.append(Detection(x1, y1, x2, y2, score))

        count += 1
        now = time.perf_counter()
        if now - t0 >= 1.0:
            with state.lock:
                state.fps_inference = count / (now - t0)
            count = 0
            t0 = now

        with state.lock:
            state.detections = detections

        notify_detection_change(state=state, config=signal_config, person_count=len(detections), backend_url=backend_url, rover_id=rover_id, camera_id=camera_id)

        elapsed = time.perf_counter() - loop_start
        if elapsed < 0.005:
            time.sleep(0.005)


def draw_overlay(frame: np.ndarray, detections: List[Detection], hud: str) -> np.ndarray:
    out = frame.copy()

    for det in detections:
        cv2.rectangle(out, (det.x1, det.y1), (det.x2, det.y2), (0, 255, 0), 2)
        label = f"person {det.confidence:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
        y = max(th + 8, det.y1)
        cv2.rectangle(out, (det.x1, y - th - 8), (det.x1 + tw + 4, y), (0, 255, 0), -1)
        cv2.putText(out, label, (det.x1 + 2, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)

    cv2.putText(out, hud, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    return out


def render_worker(state: AppState) -> None:
    while state.running:
        with state.lock:
            frame = state.latest_frame
            detections = list(state.detections)
            fps_cap = state.fps_capture
            fps_inf = state.fps_inference

        if frame is not None:
            hud = f"capture {fps_cap:.1f} fps | infer {fps_inf:.1f} fps | persons {len(detections)}"
            display = draw_overlay(frame, detections, hud)
            with state.lock:
                state.display_frame = display

        time.sleep(1.0 / 30.0)


def mjpeg_stream(state: AppState, jpeg_quality: int):
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
    count = 0
    t0 = time.perf_counter()

    while state.running:
        with state.lock:
            frame = state.display_frame
            if frame is None:
                frame = state.latest_frame

        if frame is None:
            time.sleep(0.01)
            continue

        ok, buffer = cv2.imencode('.jpg', frame, encode_params)
        if not ok:
            continue

        count += 1
        now = time.perf_counter()
        if now - t0 >= 1.0:
            with state.lock:
                state.fps_stream = count / (now - t0)
            count = 0
            t0 = now

        yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")

        time.sleep(1.0 / 30.0)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Integrator Person Detector</title>
  <style>body{font-family:Arial, sans-serif;background:#111;color:#eee;margin:0;padding:20px;}img{width:100%;max-width:960px;border-radius:8px;background:#000}</style>
</head>
<body>
  <h1>Person Detection Stream</h1>
  <img id="video" src="/video_feed" alt="MJPEG stream" />
  <div id="stats">Connecting...</div>
  <script>
    async function pollStats(){try{const res=await fetch('/api/detections',{cache:'no-store'});const data=await res.json();document.getElementById('stats').textContent = `Capture: ${data.fps_capture.toFixed(1)} FPS | Inference: ${data.fps_inference.toFixed(1)} FPS | Persons: ${data.persons.length}`;}catch(e){document.getElementById('stats').textContent='Stats unavailable';}}
    setInterval(pollStats,500);pollStats();
  </script>
</body>
</html>"""


def create_flask_app(state: AppState, jpeg_quality: int) -> Flask:
    app = Flask(__name__)

    @app.get('/')
    def index():
        return render_template_string(INDEX_HTML)

    @app.get('/video_feed')
    def video_feed():
        return Response(mjpeg_stream(state, jpeg_quality=jpeg_quality), mimetype='multipart/x-mixed-replace; boundary=frame')

    @app.get('/api/detections')
    def detections_api():
        with state.lock:
            payload = {
                'fps_capture': state.fps_capture,
                'fps_inference': state.fps_inference,
                'fps_stream': state.fps_stream,
                'detector_pid': os.getpid(),
                'person_present': state.person_present,
                'person_count': state.person_count,
                'signals_sent': state.signal_count,
                'last_signal_error': state.last_signal_error,
                'persons': [
                    {'x1': d.x1, 'y1': d.y1, 'x2': d.x2, 'y2': d.y2, 'confidence': d.confidence}
                    for d in state.detections
                ],
            }
        return jsonify(payload)

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Integrator YOLO person detector + MJPEG stream')
    parser.add_argument('--camera', type=int, default=0, help='V4L2 camera index')
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--model', default='yolov8n.pt')
    parser.add_argument('--imgsz', type=int, default=320)
    parser.add_argument('--conf', type=float, default=0.45)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8082)
    parser.add_argument('--jpeg-quality', type=int, default=70)
    parser.add_argument('--signal-pid', type=int, default=None)
    parser.add_argument('--signal-pid-env', default='ROVER_CONTROL_PID')
    parser.add_argument('--signal', choices=('SIGUSR1', 'SIGUSR2'), default='SIGUSR1')
    parser.add_argument('--detection-env', default='PERSON_DETECTED')
    parser.add_argument('--status-file', default='/tmp/person_detector_status.env')
    return parser.parse_args()


def resolve_target_pid(args: argparse.Namespace) -> Optional[int]:
    if args.signal_pid is not None:
        if args.signal_pid <= 0:
            raise ValueError('--signal-pid must be a positive integer')
        return args.signal_pid

    raw_pid = os.environ.get(args.signal_pid_env)
    if raw_pid is None or not raw_pid.strip():
        return None

    try:
        target_pid = int(raw_pid)
    except ValueError as exc:
        raise ValueError(f"{args.signal_pid_env} must contain an integer PID, got {raw_pid!r}") from exc

    if target_pid <= 0:
        raise ValueError(f"{args.signal_pid_env} must contain a positive PID")

    return target_pid


def make_signal_config(args: argparse.Namespace) -> SignalConfig:
    signal_number = getattr(signal, args.signal, 0)
    target_pid = resolve_target_pid(args)

    return SignalConfig(target_pid=target_pid, signal_number=signal_number, signal_name=args.signal, detection_env=args.detection_env, status_file=args.status_file or None)


def main() -> None:
    args = parse_args()
    signal_config = make_signal_config(args)

    backend_url = os.environ.get('BACKEND_URL', 'http://localhost:5000')
    rover_id = os.environ.get('ROVER_ID', 'ROVER-Q1')
    camera_id = os.environ.get('CAMERA_ID', 'CAM-01')

    detector_pid = os.getpid()
    os.environ['PERSON_DETECTOR_PID'] = str(detector_pid)
    write_detection_status(status_file=signal_config.status_file, detection_env=signal_config.detection_env, person_present=False, person_count=0)

    log.info('Detector PID: %d', detector_pid)
    if signal_config.target_pid is None:
        log.warning('No signal target configured. Local signalling disabled.')
    else:
        log.info('A new person detection will send %s to PID %d.', signal_config.signal_name, signal_config.target_pid)

    log.info('Loading YOLO model: %s', args.model)
    model = YOLO(args.model)

    cap = open_camera(args.camera, args.width, args.height, args.fps)
    state = AppState(signal_target_pid=signal_config.target_pid)

    workers = [
        threading.Thread(target=capture_worker, args=(cap, state), name='capture', daemon=True),
        threading.Thread(target=inference_worker, args=(model, state, args.imgsz, args.conf, args.device, signal_config, backend_url, rover_id, camera_id), name='inference', daemon=True),
        threading.Thread(target=render_worker, args=(state,), name='render', daemon=True),
    ]

    for worker in workers:
        worker.start()
        log.info('Started thread: %s', worker.name)

    app = create_flask_app(state, jpeg_quality=args.jpeg_quality)
    log.info('Open on PC: http://<HOST>:%d', args.port)

    # Telemetry forward endpoint: accept JSON POSTs from rover and forward to backend /telemetry
    @app.post('/telemetry')
    def telemetry_forward():
        try:
            payload = flask_request.get_json(force=True)
        except Exception as exc:
            log.error('Invalid telemetry payload: %s', exc)
            return ('Bad Request', 400)

        try:
            requests.post(f"{backend_url}/telemetry", json=payload, timeout=3)
        except Exception as exc:
            log.error('Failed to forward telemetry to backend: %s', exc)

        return ('OK', 200)

    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        state.running = False
        cap.release()
        log.info('Stopped.')


if __name__ == '__main__':
    main()
