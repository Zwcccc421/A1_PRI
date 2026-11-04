"""Stream a local USB camera as MJPEG for the remote A1 server.

This script is meant to run on the local computer where the USB camera is
physically connected. It uses the same OpenCV camera access pattern as
usb_camera_preview.py, then exposes:

  http://127.0.0.1:18081/video.mjpg
  http://127.0.0.1:18081/latest.jpg
  http://127.0.0.1:18081/health

If the A1 pipeline runs on a remote server, create an SSH reverse tunnel:

  ssh -N -R 18081:127.0.0.1:18081 user@server_ip

Then the server can read the local camera with:

  --camera http://127.0.0.1:18081/video.mjpg
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import threading
import time
import wave
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import cv2


LOGGER = logging.getLogger(__name__)


class UsbCamera:
    def __init__(self, index: int, width: int | None,
                 height: int | None, fps: int | None, backend: str):
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.backend = backend
        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    def _capture_loop(self):
        cap = open_video_capture(self.index, self.backend)
        if self.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if self.fps:
            cap.set(cv2.CAP_PROP_FPS, self.fps)

        if not cap.isOpened():
            LOGGER.error("Could not open camera index %s", self.index)
            return

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        LOGGER.info(
            "Opened camera index %s: %sx%s, fps=%.1f",
            self.index, actual_w, actual_h, actual_fps,
        )

        delay = 1.0 / max(self.fps or 30, 1)
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                LOGGER.warning("Failed to read camera frame")
                time.sleep(0.1)
                continue

            encoded, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if encoded:
                with self._lock:
                    self._latest_jpeg = buffer.tobytes()
            time.sleep(delay)

        cap.release()


def open_video_capture(index: int, backend: str):
    backend = backend.lower()
    if backend == "auto":
        if sys.platform == "darwin":
            backend = "avfoundation"
        elif sys.platform.startswith("win"):
            backend = "dshow"
        else:
            backend = "default"

    backend_map = {
        "avfoundation": cv2.CAP_AVFOUNDATION,
        "dshow": cv2.CAP_DSHOW,
        "default": None,
    }
    if backend not in backend_map:
        raise ValueError(
            f"Unknown camera backend {backend!r}; use auto, avfoundation, dshow, or default")
    api = backend_map[backend]
    LOGGER.info("Opening camera index %s with backend=%s", index, backend)
    if api is None:
        return cv2.VideoCapture(index)
    return cv2.VideoCapture(index, api)


class AudioRecorder:
    """Keep the latest few seconds of local microphone audio as WAV bytes."""

    def __init__(self, device: int | str | None, seconds: float,
                 sample_rate: int, channels: int):
        self.device = device
        self.seconds = seconds
        self.sample_rate = sample_rate
        self.channels = channels
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._chunks = deque()
        self._stream_chunks = deque(maxlen=300)
        self._max_samples = int(seconds * sample_rate)
        self._stream = None
        self._seq = 0

    def start(self):
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "Audio capture requires sounddevice. Install it on the local "
                "computer with: pip install sounddevice"
            ) from exc

        def callback(indata, frames, time_info, status):
            if status:
                LOGGER.warning("Audio status: %s", status)
            chunk = indata.copy()
            with self._lock:
                self._chunks.append(chunk)
                self._seq += 1
                self._stream_chunks.append((self._seq, chunk.tobytes()))
                total = sum(c.shape[0] for c in self._chunks)
                while self._chunks and total > self._max_samples:
                    removed = self._chunks.popleft()
                    total -= removed.shape[0]
                self._condition.notify_all()

        self._stream = sd.InputStream(
            device=self.device,
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=callback,
        )
        self._stream.start()
        LOGGER.info(
            "Audio capture started: device=%s, sample_rate=%s, channels=%s, window=%.1fs",
            self.device if self.device is not None else "default",
            self.sample_rate,
            self.channels,
            self.seconds,
        )

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def latest_wav(self) -> Optional[bytes]:
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return None

        import numpy as np

        audio = np.concatenate(chunks, axis=0)
        if audio.size == 0:
            return None

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(audio.tobytes())
        return buf.getvalue()

    def wait_pcm_chunk(self, last_seq: int, timeout: float = 1.0) -> tuple[int, Optional[bytes]]:
        deadline = time.time() + timeout
        with self._condition:
            while True:
                for seq, data in self._stream_chunks:
                    if seq > last_seq:
                        return seq, data
                remaining = deadline - time.time()
                if remaining <= 0:
                    return last_seq, None
                self._condition.wait(timeout=remaining)


class MjpegServer:
    def __init__(self, camera: UsbCamera, audio: Optional[AudioRecorder],
                 host: str, port: int):
        self.camera = camera
        self.audio = audio
        self.host = host
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        camera = self.camera
        audio = self.audio

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                LOGGER.debug("http: " + fmt, *args)

            def do_GET(self):
                if self.path == "/health":
                    self._write_json({
                        "ok": camera.latest_jpeg() is not None,
                        "video_ok": camera.latest_jpeg() is not None,
                        "audio_ok": (
                            True if audio is None else audio.latest_wav() is not None
                        ),
                    })
                    return

                if self.path == "/latest.jpg":
                    frame = camera.latest_jpeg()
                    if frame is None:
                        self.send_error(503, "camera frame not ready")
                        return
                    self._write_jpeg(frame)
                    return

                if self.path == "/latest.wav":
                    if audio is None:
                        self.send_error(404, "audio capture is disabled")
                        return
                    wav = audio.latest_wav()
                    if wav is None:
                        self.send_error(503, "audio not ready")
                        return
                    self._write_wav(wav)
                    return

                if self.path == "/audio.pcm":
                    if audio is None:
                        self.send_error(404, "audio capture is disabled")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("X-Sample-Rate", str(audio.sample_rate))
                    self.send_header("X-Channels", str(audio.channels))
                    self.send_header("X-Sample-Width", "2")
                    self.end_headers()
                    last_seq = 0
                    while True:
                        try:
                            last_seq, data = audio.wait_pcm_chunk(last_seq, timeout=1.0)
                            if data:
                                self.wfile.write(data)
                                self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                    return

                if self.path != "/video.mjpg":
                    self.send_error(404)
                    return

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary=frame",
                )
                self.end_headers()

                while True:
                    frame = camera.latest_jpeg()
                    if frame is None:
                        time.sleep(0.1)
                        continue
                    try:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(
                            f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                        time.sleep(0.03)
                    except (BrokenPipeError, ConnectionResetError):
                        break

            def _write_jpeg(self, frame: bytes):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)

            def _write_wav(self, wav: bytes):
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.end_headers()
                self.wfile.write(wav)

            def _write_json(self, payload: dict):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def parse_args():
    parser = argparse.ArgumentParser(description="USB camera MJPEG streamer")
    parser.add_argument("--index", type=int, default=0,
                        help="Camera index, usually 0 or 1")
    parser.add_argument("--camera-name", default=None,
                        help="Exact Windows DirectShow camera name; overrides --index")
    parser.add_argument("--backend", default="auto",
                        choices=["auto", "avfoundation", "dshow", "default"],
                        help="OpenCV camera backend; auto uses AVFoundation on macOS")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Use 127.0.0.1 for SSH tunnel, or 0.0.0.0 for LAN")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--enable-audio", action="store_true",
                        help="Also capture local microphone audio and expose /latest.wav")
    parser.add_argument("--audio-device", default=None,
                        help="sounddevice input device index/name; default uses system input")
    parser.add_argument("--audio-seconds", type=float, default=5.0,
                        help="Rolling audio window served by /latest.wav")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    audio_device = None
    if args.audio_device:
        try:
            audio_device = int(args.audio_device)
        except ValueError:
            audio_device = args.audio_device

    if args.camera_name:
        if not sys.platform.startswith("win") or args.backend not in {"auto", "dshow"}:
            raise RuntimeError("--camera-name requires Windows DirectShow")
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
        matches = [i for i, name in enumerate(names)
                   if name.casefold() == args.camera_name.casefold()]
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected exactly one camera named {args.camera_name!r}; "
                f"found {len(matches)}. Available cameras: {names!r}")
        args.index = matches[0]
        args.backend = "dshow"
        LOGGER.info("Selected camera by name: %s (index=%s)", names[args.index], args.index)
    camera = UsbCamera(args.index, args.width, args.height, args.fps, args.backend)
    audio = (
        AudioRecorder(
            device=audio_device,
            seconds=args.audio_seconds,
            sample_rate=args.sample_rate,
            channels=args.channels,
        )
        if args.enable_audio else None
    )
    server = MjpegServer(camera, audio, args.host, args.port)
    camera.start()
    if audio is not None:
        audio.start()
    server.start()

    LOGGER.info("Local camera stream: http://%s:%s/video.mjpg", args.host, args.port)
    if audio is not None:
        LOGGER.info("Local audio clip: http://%s:%s/latest.wav", args.host, args.port)
    LOGGER.info("Press Ctrl+C to stop")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        if audio is not None:
            audio.stop()
        camera.stop()


if __name__ == "__main__":
    main()
