#!/usr/bin/env python3
"""远端下行：本机麦克风 -> HTTP PCM，供机器人拉流播放。

在 PC/服务器上运行，不要在 G1 机器人上运行。
机器人 camera_open.sh 配置:
  --audio-play-url http://<本机LAN_IP>:18085/audio.pcm
"""
from __future__ import annotations

import argparse
import audioop
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Union

try:
    import sounddevice as sd
except ImportError:
    sd = None

ROBOT_SAMPLE_RATE = 16000
DEFAULT_SEND_PORT = 18085


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def list_pulse_sources() -> None:
    if not shutil.which("pactl"):
        print("未找到 pactl（可安装: sudo apt install pulseaudio-utils）", flush=True)
        return
    print("=== PulseAudio sources (Ubuntu 麦克风优先看这个) ===", flush=True)
    try:
        print(subprocess.check_output(["pactl", "list", "short", "sources"], text=True), flush=True)
        default = subprocess.check_output(["pactl", "get-default-source"], text=True).strip()
        print(f"默认 source: {default}", flush=True)
    except subprocess.CalledProcessError as exc:
        print(f"无法列出 sources: {exc}", flush=True)


def list_devices() -> None:
    if sd is not None:
        d = sd.default.device
        print("=== PortAudio 输入设备 (sounddevice) ===", flush=True)
        print(sd.query_devices(), flush=True)
        print(f"默认输入={d[0]}  默认输出={d[1]}", flush=True)
    else:
        print("sounddevice 未安装", flush=True)
    list_pulse_sources()
    print(f"本机 LAN IP: {get_lan_ip()}", flush=True)
    print(
        f"机器人需配置: --audio-play-url http://{get_lan_ip()}:{DEFAULT_SEND_PORT}/audio.pcm",
        flush=True,
    )


def pick_input_device(device: Optional[Union[int, str]]) -> Union[int, str]:
    if sd is None:
        raise RuntimeError("sounddevice is required")
    if device is not None:
        return device

    candidates = []
    defaults = sd.default.device
    if isinstance(defaults, (list, tuple)) and defaults[0] is not None and int(defaults[0]) >= 0:
        candidates.append(defaults[0])
    for idx, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) > 0:
            candidates.append(idx)

    seen = set()
    ordered = []
    for dev in candidates:
        if dev not in seen:
            seen.add(dev)
            ordered.append(dev)

    def score(dev: Union[int, str]) -> tuple:
        name = str(sd.query_devices(dev).get("name", "")).lower()
        monitor = 1 if ".monitor" in name or "monitor" in name else 0
        dummy = 1 if "dummy" in name or "null" in name else 0
        hdmi = 1 if "hdmi" in name else 0
        default_bonus = 0 if dev == candidates[0] else 1
        return (monitor, dummy, hdmi, default_bonus)

    if not ordered:
        raise RuntimeError("未找到麦克风，请 --list-devices 后使用 --mic-device 指定")
    return sorted(ordered, key=score)[0]


def describe_device(device: Union[int, str]) -> str:
    info = sd.query_devices(device)
    return f"[{device}] {info.get('name', '?')} ({int(info.get('default_samplerate', 0))} Hz native)"


def resolve_sample_rate(device: Union[int, str], kind: str) -> int:
    info = sd.query_devices(device)
    native = int(info.get("default_samplerate") or 48000)
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    candidates = []
    for rate in (ROBOT_SAMPLE_RATE, native, 48000, 44100, 32000, 22050, 8000):
        if rate not in candidates:
            candidates.append(rate)
    errors = []
    for rate in candidates:
        try:
            check(device=device, samplerate=rate, channels=1, dtype="int16")
            return rate
        except Exception as exc:
            errors.append(f"{rate}Hz: {exc}")
    raise RuntimeError(
        f"设备 {device} 无可用采样率。\n"
        + "\n".join(errors[:4])
        + "\n请 --list-devices 后换 --mic-device。"
    )


class PcmResampler:
    def __init__(self, in_rate: int, out_rate: int):
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._state = None

    def process(self, pcm: bytes) -> bytes:
        if not pcm or self.in_rate == self.out_rate:
            return pcm
        pcm, self._state = audioop.ratecv(pcm, 2, 1, self.in_rate, self.out_rate, self._state)
        return pcm


def _pcm_rms(data: bytes) -> int:
    return audioop.rms(data, 2) if len(data) >= 2 else 0


def _hangover_chunks(hangover_ms: float, chunk_size: int, sample_rate: int = ROBOT_SAMPLE_RATE) -> int:
    chunk_ms = (chunk_size / (sample_rate * 2)) * 1000.0
    return max(1, int(round(hangover_ms / chunk_ms))) if chunk_ms > 0 else 1


class VoiceGate:
    def __init__(self, threshold: int, hangover_chunks: int = 5):
        self.threshold = max(0, int(threshold))
        self.hangover_chunks = max(0, int(hangover_chunks))
        self._remaining = 0

    def active(self, data: bytes) -> bool:
        if _pcm_rms(data) >= self.threshold:
            self._remaining = self.hangover_chunks
            return True
        if self._remaining > 0:
            self._remaining -= 1
            return True
        return False


class PcmHub:
    def __init__(self, chunk_size: int, sample_rate: int = ROBOT_SAMPLE_RATE):
        self.chunk_size = chunk_size
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending = bytearray()
        self._max_pending_bytes = sample_rate * 2 // 2
        self._silence = b"\x00" * chunk_size

    def seek_live(self):
        with self._cond:
            self._pending.clear()
            self._cond.notify_all()

    def push(self, data: bytes):
        if not data:
            return
        with self._cond:
            self._pending.extend(data)
            if len(self._pending) > self._max_pending_bytes:
                del self._pending[:-self._max_pending_bytes]
            self._cond.notify_all()

    def read_chunk(self, timeout: float = 0.2, *, allow_silence: bool = True) -> bytes:
        with self._cond:
            while True:
                if len(self._pending) >= self.chunk_size:
                    chunk = bytes(self._pending[:self.chunk_size])
                    del self._pending[:self.chunk_size]
                    return chunk
                if allow_silence:
                    deadline = time.monotonic() + timeout
                    if time.monotonic() >= deadline:
                        return self._silence
                    self._cond.wait(timeout=deadline - time.monotonic())
                else:
                    self._cond.wait(timeout=1.0)


class AudioSenderHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/audio.pcm":
            self._serve_pcm()
        elif path == "/":
            self._serve_index()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_index(self):
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")
        body = f"<html><body><a href='http://{host}/audio.pcm'>/audio.pcm</a></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_pcm(self):
        hub: PcmHub = self.server.pcm_hub  # type: ignore[attr-defined]
        sample_rate: int = self.server.sample_rate  # type: ignore[attr-defined]
        chunk_size: int = self.server.chunk_size  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", f"audio/L16; rate={sample_rate}; channels=1")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        hub.seek_live()
        chunk_interval = chunk_size / (sample_rate * 2)
        next_deadline = time.monotonic()
        vad_on = getattr(self.server, "vad_rms", 0) > 0  # type: ignore[attr-defined]
        try:
            while True:
                chunk = hub.read_chunk(timeout=chunk_interval, allow_silence=not vad_on)
                self.wfile.write(chunk)
                self.wfile.flush()
                next_deadline += chunk_interval
                sleep_for = next_deadline - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_deadline = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass


def push_pcm_with_logs(
    hub: PcmHub,
    pcm: bytes,
    voice_gate: Optional[VoiceGate],
    *,
    captured_chunks: list,
    sent_chunks: list,
):
    captured_chunks[0] += 1
    rms = _pcm_rms(pcm)
    if captured_chunks[0] == 1 or captured_chunks[0] % 100 == 0:
        print(f"麦克风采集 #{captured_chunks[0]} rms={rms}", flush=True)
    if voice_gate is None or voice_gate.active(pcm):
        hub.push(pcm)
        sent_chunks[0] += 1
        if sent_chunks[0] == 1 or sent_chunks[0] % 50 == 0:
            print(f"已发送音频块 #{sent_chunks[0]} rms={rms}", flush=True)


def mic_to_hub_sounddevice(hub, device_rate, robot_rate, chunk_size, mic_device, voice_gate):
    resampler = PcmResampler(device_rate, robot_rate)
    dev_block = max(1, int((chunk_size // 2) * device_rate / robot_rate))
    captured = [0]
    sent = [0]

    def callback(indata, frames, time_info, status):
        del frames, time_info
        if status:
            print(f"麦克风状态: {status}", flush=True)
        data = resampler.process(bytes(indata))
        push_pcm_with_logs(hub, data, voice_gate, captured_chunks=captured, sent_chunks=sent)

    try:
        stream_ctx = sd.RawInputStream(
            samplerate=device_rate,
            channels=1,
            dtype="int16",
            blocksize=dev_block,
            device=mic_device,
            callback=callback,
        )
    except Exception as exc:
        print(f"错误: 无法打开麦克风 ({device_rate}Hz): {exc}", flush=True)
        print("请试: python3 audio_sender.py --list-devices --mic-device <编号>", flush=True)
        print("Ubuntu 推荐: --backend parec", flush=True)
        return
    with stream_ctx:
        while True:
            time.sleep(0.5)


def mic_to_hub_parec(hub, robot_rate, chunk_size, pulse_source, voice_gate):
    if not shutil.which("parec"):
        print("未找到 parec，请: sudo apt install pulseaudio-utils", flush=True)
        return
    cmd = [
        "parec",
        "--format=s16le",
        f"--rate={robot_rate}",
        "--channels=1",
        "--latency-msec=50",
    ]
    if pulse_source:
        cmd += [f"--device={pulse_source}"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except Exception as exc:
        print(f"parec 启动失败: {exc}", flush=True)
        return
    if proc.stdout is None:
        print("parec stdout 不可用", flush=True)
        return
    print(f"parec 已启动: {' '.join(cmd)}", flush=True)
    captured = [0]
    sent = [0]
    try:
        while True:
            data = proc.stdout.read(chunk_size)
            if not data:
                err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                print(f"parec 结束: {err.strip()}", flush=True)
                break
            push_pcm_with_logs(hub, data, voice_gate, captured_chunks=captured, sent_chunks=sent)
    finally:
        proc.terminate()


def test_microphone(backend: str, mic_device, pulse_source, robot_rate: int) -> bool:
    print("=== 麦克风测试 3 秒，请对着麦克风说话 ===", flush=True)
    max_rms = 0
    samples = 0
    if backend == "parec":
        if not shutil.which("parec"):
            print("未找到 parec", flush=True)
            return False
        cmd = ["parec", "--format=s16le", f"--rate={robot_rate}", "--channels=1"]
        if pulse_source:
            cmd += [f"--device={pulse_source}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        t0 = time.time()
        while time.time() - t0 < 3:
            data = proc.stdout.read(1024)
            if not data:
                break
            samples += 1
            max_rms = max(max_rms, _pcm_rms(data))
        proc.terminate()
    else:
        if sd is None:
            print("sounddevice 未安装", flush=True)
            return False
        mic = pick_input_device(mic_device)
        rate = resolve_sample_rate(mic, "input")
        buf = []

        def cb(indata, frames, time_info, status):
            del frames, time_info, status
            buf.append(bytes(indata))

        with sd.RawInputStream(
            samplerate=rate, channels=1, dtype="int16", device=mic, callback=cb, blocksize=1024
        ):
            time.sleep(3)
        for data in buf:
            samples += 1
            max_rms = max(max_rms, _pcm_rms(data))
    print(f"测试结果: blocks={samples} max_rms={max_rms}", flush=True)
    if samples == 0:
        print("未采到任何数据：麦克风未打开或权限不足", flush=True)
        return False
    if max_rms < 30:
        print("音量过低：请检查系统麦克风音量/静音，或换 --pulse-source / --mic-device", flush=True)
        return False
    print("麦克风测试通过", flush=True)
    return True


def choose_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if shutil.which("parec"):
        return "parec"
    return "sounddevice"


def parse_args():
    p = argparse.ArgumentParser(description="远端下行：本机麦克风 -> 机器人（非机器人运行）")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_SEND_PORT)
    p.add_argument("--sample-rate", type=int, default=ROBOT_SAMPLE_RATE)
    p.add_argument("--chunk-size", type=int, default=1024)
    p.add_argument("--mic-device", default=None, help="sounddevice 输入设备编号")
    p.add_argument("--pulse-source", default=None, help="Ubuntu PulseAudio source 名")
    p.add_argument(
        "--backend",
        choices=("auto", "sounddevice", "parec"),
        default="auto",
        help="Ubuntu 推荐 auto：优先 parec",
    )
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--test-mic", action="store_true", help="测试麦克风 3 秒")
    p.add_argument("--vad-rms", type=int, default=0,
                   help="RMS VAD (0=关，推荐先用 0 排查)")
    p.add_argument("--vad-hangover-ms", type=float, default=300.0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_devices:
        list_devices()
        return 0

    backend = choose_backend(args.backend)
    robot_rate = args.sample_rate
    lan = get_lan_ip()
    hangover = _hangover_chunks(args.vad_hangover_ms, args.chunk_size, robot_rate)
    gate = VoiceGate(args.vad_rms, hangover) if args.vad_rms > 0 else None

    if args.test_mic:
        ok = test_microphone(backend, args.mic_device, args.pulse_source, robot_rate)
        return 0 if ok else 1

    if backend == "sounddevice":
        if sd is None:
            print("需要安装 sounddevice: pip install sounddevice", flush=True)
            return 1
        try:
            mic = pick_input_device(args.mic_device)
            device_rate = resolve_sample_rate(mic, "input")
        except RuntimeError as exc:
            print(f"错误: {exc}", flush=True)
            return 1
        mic_desc = describe_device(mic)
        rate_note = (
            f"{device_rate} Hz"
            if device_rate == robot_rate
            else f"{device_rate} Hz -> {robot_rate} Hz"
        )
    else:
        mic = None
        device_rate = robot_rate
        mic_desc = args.pulse_source or "(系统默认 PulseAudio source)"
        rate_note = f"{robot_rate} Hz"

    hub = PcmHub(args.chunk_size, robot_rate)
    if backend == "parec":
        target = mic_to_hub_parec
        thread_args = (hub, robot_rate, args.chunk_size, args.pulse_source, gate)
    else:
        target = mic_to_hub_sounddevice
        thread_args = (hub, device_rate, robot_rate, args.chunk_size, mic, gate)

    threading.Thread(target=target, args=thread_args, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), AudioSenderHandler)
    srv.pcm_hub = hub  # type: ignore[attr-defined]
    srv.sample_rate = robot_rate  # type: ignore[attr-defined]
    srv.chunk_size = args.chunk_size  # type: ignore[attr-defined]
    srv.vad_rms = args.vad_rms  # type: ignore[attr-defined]
    vad_label = "关闭(连续发送)" if args.vad_rms <= 0 else f"开启(rms>={args.vad_rms})"
    print(
        f"[远端下行] http://0.0.0.0:{args.port}/audio.pcm\n"
        f"本机 IP: {lan}\n"
        f"采集后端: {backend}\n"
        f"麦克风: {mic_desc}\n"
        f"采集/发送采样率: {rate_note}\n"
        f"VAD: {vad_label}\n"
        f"机器人 camera_open.sh:\n"
        f"  --audio-play-url http://{lan}:{args.port}/audio.pcm",
        flush=True,
    )
    print("启动后应看到「麦克风采集 #1」；说话后应看到「已发送音频块 #1」", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
