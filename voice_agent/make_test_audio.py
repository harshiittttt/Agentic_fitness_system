"""Makes the 'Golu' test lines as PCM16 16 kHz files in D:/voice-proto/audio/ (t_<name>.pcm) with Kokoro, for play_client.py scenario 'golu'.
    D:\\voice-proto\\venv\\Scripts\\python voice_agent\\make_test_audio.py"""
import os

import numpy as np
from kokoro_onnx import Kokoro

MODELS = "D:/voice-proto/models"
OUT = "D:/voice-proto/audio/"
LINES = {
    "start": "Hey coach, let's start today's workout.",
    "plan": "What is the plan for today?",
    "set1": "Sixty kilos, five reps, done.",
    "set2": "Sixty kilos, five reps again.",
    "no_skip": "We are not skipping anything, we are doing every exercise.",
    "no_skip2": "I am not skipping the goblet squat.",
    "nextset": "Okay, next set.",
    "next_ex": "What is the next exercise?",
    "real_skip": "Skip this exercise, my shoulder hurts.",
    "left": "How many sets are left?",
    "wrap": "Okay coach, let's wrap up.",
    "hello": "Hello.",
    "copy": "Copy.",
    "stage": "Boxing, wipes hands, adjust straps.",
    "mumble": "Umm, haan, woh, kya, hmm.",
    "weight_q": "How much weight should I use for the next set?",
    "sets60": "I did sixty kilos for five reps on the deadlift.",
}
# Hindi lines (Devanagari, read by Kokoro's Hindi voice): what the app's Hinglish users say. File names t_hi_<name>.pcm
HINDI = {
    "set": "साठ किलो पाँच बार हो गया",
    "next": "अगला एक्सरसाइज कौन सा है",
    "left": "कितने सेट बाकी हैं",
    "weight": "अगले सेट में वजन बढ़ाऊँ क्या",
    "wait": "रुको थोड़ा आराम करने दो",
    "skip": "यह वाला स्किप कर दो कंधा दुख रहा है",
    "plan": "आज का प्लान क्या है",
}


def main():
    k = Kokoro(f"{MODELS}/kokoro-v1.0.onnx", f"{MODELS}/voices-v1.0.bin")
    for name, text in LINES.items():
        x, sr = k.create(text, voice="am_adam", speed=1.0, lang="en-us")
        n = int(len(x) * 16000 / sr)
        y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        pcm = (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()
        with open(os.path.join(OUT, f"t_{name}.pcm"), "wb") as f:
            f.write(pcm)
        print(name, round(n / 16000, 1), "s")
    for name, text in HINDI.items():
        x, sr = k.create(text, voice="hm_psi", speed=1.0, lang="hi")
        n = int(len(x) * 16000 / sr)
        y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        with open(os.path.join(OUT, f"t_hi_{name}.pcm"), "wb") as f:
            f.write((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
        print("hi", name, round(n / 16000, 1), "s")


if __name__ == "__main__":
    main()
