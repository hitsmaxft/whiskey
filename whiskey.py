#!/usr/bin/env python3
"""Whiskey: loopback HTTP adapter for installed ASR engines."""

import argparse
import base64
import io
import json
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_UPLOAD = 32 * 1024 * 1024
REQUEST_TIMEOUT = 90
IDLE_SECONDS = 300


def pcm16_mono_16k(wav_bytes: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as source:
            channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
            if channels not in (1, 2) or width != 2 or not 8000 <= rate <= 96000:
                raise ValueError("expected 8–96 kHz mono/stereo PCM16 WAV")
            samples = array("h")
            samples.frombytes(source.readframes(source.getnframes()))
    except (EOFError, wave.Error) as error:
        raise ValueError("invalid PCM16 WAV") from error
    if sys.byteorder != "little":
        samples.byteswap()
    if channels == 2:
        samples = array("h", ((int(samples[i]) + int(samples[i + 1])) // 2 for i in range(0, len(samples), 2)))
    if rate != 16000:
        output = array("h")
        for i in range(len(samples) * 16000 // rate):
            position = i * rate / 16000
            left = int(position)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            output.append(round(samples[left] * (1 - fraction) + samples[right] * fraction))
        samples = output
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise ValueError("empty audio")
    return samples.tobytes()


def pcm_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue()


class DoubaoEngine:
    def __init__(self, binary: Path, model: Path):
        self.binary, self.model = binary, model
        self._lock = threading.Lock()
        self._process = None
        self._used_at = 0.0
        threading.Thread(target=self._reap_idle, daemon=True).start()

    def ready(self):
        return self.binary.is_file() and self.model.is_file() and all(
            (self.model.parent / name).is_file()
            for name in ("libc++_shared.so", "libiesapplogger.so", "libaudioeffect.so")
        )

    def _close(self):
        if self._process is not None:
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait(timeout=5)
            self._process = None

    def _reap_idle(self):
        while True:
            time.sleep(30)
            with self._lock:
                if self._process is not None and time.monotonic() - self._used_at > IDLE_SECONDS:
                    self._close()

    def transcribe(self, pcm: bytes):
        with self._lock:
            if not self.ready():
                raise RuntimeError("VoiceKey Doubao model or engine files are missing")
            for attempt in range(2):
                try:
                    if self._process is None or self._process.poll() is not None:
                        self._close()
                        self._process = subprocess.Popen(
                            [str(self.binary), "--offline-worker", str(self.model)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0,
                        )
                    request = json.dumps(
                        {"op": "asr", "b64": base64.b64encode(pcm).decode("ascii")},
                        separators=(",", ":"),
                    ).encode() + b"\n"
                    self._process.stdin.write(request)
                    self._process.stdin.flush()
                    if not select.select([self._process.stdout], [], [], REQUEST_TIMEOUT)[0]:
                        raise TimeoutError("offline worker timed out")
                    line = self._process.stdout.readline()
                    if not line:
                        raise RuntimeError("offline worker exited")
                    reply = json.loads(line)
                    if reply.get("ok") is not True:
                        raise RuntimeError(str(reply.get("error", "offline worker failed")))
                    self._used_at = time.monotonic()
                    return reply.get("text", "").strip()
                except (BrokenPipeError, OSError, TimeoutError, json.JSONDecodeError) as error:
                    self._close()
                    if attempt:
                        raise RuntimeError(f"offline worker failed: {error}") from error
            raise RuntimeError("offline worker failed")


class WeTypeEngine:
    def __init__(self, binary: Path, model: Path):
        self.binary, self.model = binary, model

    def ready(self):
        return self.binary.is_file() and self.model.is_dir() and (self.model / "dict.decoder.utf8.txt").is_file()

    def transcribe(self, pcm: bytes):
        if not self.ready():
            raise RuntimeError("VoiceKey WeType model or wtl executable is missing")
        with tempfile.TemporaryDirectory(prefix="whiskey-asr-") as directory:
            wav = Path(directory) / "audio.wav"
            wav.write_bytes(pcm_wav(pcm))
            completed = subprocess.run(
                [str(self.binary), str(self.model), str(wav)],
                capture_output=True, text=True, timeout=REQUEST_TIMEOUT, check=True,
            )
        return completed.stdout.strip().rsplit("\t", 1)[-1].strip()


class DoubaoOnlineEngine:
    """Invoke VoiceKey's separate vk-test CLI; audio is sent to Doubao."""

    def __init__(self, binary: Path, model=None):
        self.binary = binary

    def ready(self):
        return self.binary.is_file()

    def transcribe(self, pcm: bytes):
        if not self.ready():
            raise RuntimeError("VoiceKey vk-test executable is missing")
        with tempfile.TemporaryDirectory(prefix="whiskey-asr-") as directory:
            wav = Path(directory) / "audio.wav"
            wav.write_bytes(pcm_wav(pcm))
            completed = subprocess.run(
                [str(self.binary), "doubao", str(wav)],
                capture_output=True, text=True, timeout=REQUEST_TIMEOUT, check=True,
            )
        final = next((line.removeprefix("FINAL: ") for line in reversed(completed.stdout.splitlines()) if line.startswith("FINAL: ")), None)
        if final is None:
            raise RuntimeError("VoiceKey returned no final transcript")
        return re.sub(r"(?:\s*\(\d+(?:\.\d+)?s\))+$", "", final).strip()


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ("/", "/health"):
                return self._json(404, {"error": "not found"})
            self._json(200 if engine.ready() else 503, {"ready": engine.ready(), "engine": type(engine).__name__})

        def do_POST(self):
            if self.path != "/inference":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_UPLOAD:
                    raise ValueError("audio upload size is invalid")
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ValueError("expected multipart/form-data")
                raw = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + self.rfile.read(length)
                message = BytesParser(policy=policy.default).parsebytes(raw)
                file_part = next((part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "file"), None)
                if file_part is None:
                    raise ValueError("missing file part")
                pcm = pcm16_mono_16k(file_part.get_payload(decode=True))
                text = engine.transcribe(pcm)
                self._json(200, {"text": text})
            except ValueError as error:
                self._json(400, {"error": str(error)})
            except (RuntimeError, subprocess.SubprocessError) as error:
                self._json(503, {"error": str(error)})

        def log_message(self, format_string, *args):
            sys.stderr.write("whiskey: " + format_string % args + "\n")

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Whiskey ASR HTTP adapter")
    parser.add_argument("--engine", choices=("doubao", "wetype", "doubao-online"), required=True)
    parser.add_argument("--bin", type=Path, required=True, help="VoiceKey app executable or wtl executable")
    parser.add_argument("--model", type=Path, help="model.flute or unpacked WeType model directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18793)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        parser.error("only loopback listening is supported")
    if args.engine != "doubao-online" and args.model is None:
        parser.error("--model is required for offline engines")
    engine_type = {"doubao": DoubaoEngine, "wetype": WeTypeEngine, "doubao-online": DoubaoOnlineEngine}[args.engine]
    engine = engine_type(args.bin, args.model)
    if not engine.ready():
        parser.error("selected executable or model files are missing")
    ThreadingHTTPServer((args.host, args.port), make_handler(engine)).serve_forever()


if __name__ == "__main__":
    main()
