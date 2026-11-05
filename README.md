# Whiskey

A small loopback HTTP adapter for [VoiceKey](https://github.com/J3n5en/voicekey)'s offline Doubao and WeType engines, plus its online Doubao CLI. It does not bundle VoiceKey, engine libraries, or model files. Audio stays on the local machine when an offline engine is selected; the online engine sends it to Doubao.

## Requirements

- Python 3.10 or newer; no Python packages are required.
- Doubao: Apple silicon Mac, a VoiceKey app executable, and VoiceKey's downloaded Doubao offline files (`model.flute` plus the three engine libraries in the same directory).
- WeType: VoiceKey's `wtl` executable and its unpacked offline model directory.
- Doubao online: VoiceKey's `vk-test` executable, built from its `voicekey-core` package. It uses VoiceKey's unofficial network protocol and may stop working if the upstream service changes.

Install VoiceKey and download the selected offline channel in its settings first. See VoiceKey's README for the model setup and platform support.

## Run

```sh
python3 whiskey.py \
  --engine doubao \
  --bin "/path/to/VoiceKey.app/Contents/MacOS/voicekey" \
  --model "/path/to/VoiceKey/offline/model.flute"
```

For WeType, use `--engine wetype`, set `--bin` to the `wtl` executable built from VoiceKey, and `--model` to its unpacked model directory.

For Doubao online, use `--engine doubao-online --bin /path/to/vk-test`; no model path is needed.

The service listens on `127.0.0.1:18793` by default. `GET /health` reports readiness; `POST /inference` accepts a multipart `file` containing PCM16 WAV and returns `{"text":"..."}`. This matches the request and response shape used by whisper.cpp clients. The Doubao worker stays warm during use and exits after five idle minutes.

For live dictation, send each approximately five-second PCM16 WAV segment to `/inference` while recording continues. Each response contains that segment's final transcript, so a client can update its draft without waiting for the microphone to stop. Send the shorter final segment when recording ends. Requests from one recording should be consumed in order; the server does not retain a conversation or merge text across segments. Online Doubao uses a separate request for each segment, so recognition latency depends on network and upstream response time.

This adapter is independent of VoiceKey and uses its published executable or CLI as an external component. VoiceKey and the model files have their own terms; check upstream before redistribution.
