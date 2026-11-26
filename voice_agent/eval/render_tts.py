"""Renders caller lines to PCM16 16 kHz with Kokoro. Run with the VOICE venv (it has kokoro_onnx), not the project python:
    D:\\voice-proto\\venv\\Scripts\\python.exe voice_agent\\eval\\render_tts.py <jobs.json>
jobs.json: [{"key": "...", "text": "...", "voice": "am_adam", "lang": "en-us", "speed": 1.0, "out": "D:/voice-proto/audio/eval/<key>.pcm"}]
audio.py calls this for the lines that are not cached yet (render once, replay forever)."""
import json
import os
import sys

import numpy as np

MODELS = os.environ.get("VOICE_MODELS", "D:/voice-proto/models")


def main(path: str) -> None:
    from kokoro_onnx import Kokoro
    jobs = json.load(open(path, encoding="utf-8"))
    k = Kokoro(f"{MODELS}/kokoro-v1.0.onnx", f"{MODELS}/voices-v1.0.bin")
    for j in jobs:
        x, sr = k.create(j["text"], voice=j["voice"], speed=j.get("speed", 1.0), lang=j.get("lang", "en-us"))
        n = int(len(x) * 16000 / sr)
        y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        os.makedirs(os.path.dirname(j["out"]), exist_ok=True)
        open(j["out"], "wb").write((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
        print("rendered", j["key"], round(n / 16000, 1), "s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
