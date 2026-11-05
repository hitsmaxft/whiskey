import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import wave
from array import array
from http.server import ThreadingHTTPServer

from whiskey import DoubaoOnlineEngine, make_handler, pcm16_mono_16k


def wav_bytes(rate=16000, channels=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(array("h", [1000, -1000] * 160).tobytes())
    return out.getvalue()


class FakeEngine:
    def ready(self):
        return True

    def transcribe(self, pcm):
        assert pcm
        return "离线识别成功"


class ServerTests(unittest.TestCase):
    def test_resamples_stereo_pcm_to_mono_16k(self):
        pcm = pcm16_mono_16k(wav_bytes(rate=32000, channels=2))
        self.assertEqual(len(pcm), 80 * 2)
        samples = array("h")
        samples.frombytes(pcm)
        self.assertEqual(set(samples), {0})

    def test_inference_returns_text(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeEngine()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            boundary = "test-boundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n"
            ).encode() + wav_bytes() + f"\r\n--{boundary}--\r\n".encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/inference", body,
                {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(json.load(response), {"text": "离线识别成功"})
        finally:
            server.shutdown()
            server.server_close()

    def test_online_cli_extracts_final_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "vk-test"
            executable.write_text("#!/bin/sh\nprintf '… partial\\nFINAL: 完整文本 (1.23s)\\n'\n")
            executable.chmod(0o700)
            self.assertEqual(DoubaoOnlineEngine(executable).transcribe(b"\0" * 320), "完整文本")

    def test_online_cli_does_not_return_timing_when_speech_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "vk-test"
            executable.write_text("#!/bin/sh\nprintf 'FINAL: (3.31s) (3.26s)\\n'\n")
            executable.chmod(0o700)
            self.assertEqual(DoubaoOnlineEngine(executable).transcribe(b"\0" * 320), "")


if __name__ == "__main__":
    unittest.main()
