"""Speech-to-text accuracy and speed on the synthetic test lines (English: Kokoro am_adam, Hindi: Kokoro hm_psi), for each Whisper size you pass.
    D:\\voice-proto\\venv\\Scripts\\python voice_agent\\stt_bench.py small medium
Stop the test voice agent first (the 4 GB GPU cannot hold two agents plus a medium model). Prints what Whisper wrote after the app's own fixes, the model's
confidence, and how long it took, then the GPU memory used."""
import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np  # noqa: E402

import server as S  # noqa: E402  (patches faster_whisper.WhisperModel with the prompted, confidence-checking wrapper)
import faster_whisper  # noqa: E402

AUDIO = "D:/voice-proto/audio/"
EXPECT = {  # what was said (the Hindi ones in the Roman letters the backend parses)
    "t_set1": "60 kilos 5 reps", "t_plan": "what is the plan for today", "t_left": "how many sets are left", "t_weight_q": "how much weight should i use for the next set",
    "t_sets60": "i did 60 kilos for 5 reps on the deadlift", "t_no_skip": "we are not skipping anything", "t_hi_set": "saath kilo paanch baar ho gaya",
    "t_hi_next": "agla exercise kaun sa hai", "t_hi_left": "kitne set baaki hain", "t_hi_weight": "agle set mein vajan badhaun kya", "t_hi_wait": "ruko thoda aaram karne do",
    "t_hi_skip": "yeh wala skip kar do kandha dukh raha hai", "t_hi_plan": "aaj ka plan kya hai",
}


def main(sizes):
    files = [AUDIO + n + ".pcm" for n in EXPECT if os.path.exists(AUDIO + n + ".pcm")]
    for size in sizes:
        ct = "float16"
        m = faster_whisper.WhisperModel(size, device="cuda", compute_type=ct)
        warm = np.zeros(16000, dtype=np.float32)
        m.transcribe(warm, language="en")
        print(f"\n===== whisper {size} ({ct})")
        tot = 0.0
        for f in files:
            audio = np.frombuffer(open(f, "rb").read(), dtype="<i2").astype(np.float32) / 32768.0
            t = time.time()
            segs, info = m.transcribe(audio, language=None)
            ms = (time.time() - t) * 1000
            tot += ms
            txt = " ".join(s.text.strip() for s in segs)
            st = S.STT_STATS["last"]
            print(f"{os.path.basename(f)[:-4]:12s} {ms:6.0f} ms  lang={info.language}/{info.language_probability:.2f} logprob={st['logprob']}  -> {txt!r}   (said: {EXPECT[os.path.basename(f)[:-4]]})")
        print(f"mean {tot / len(files):.0f} ms per clip")
        os.system("nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader")
        S._whisper_cache.clear()
        del m


if __name__ == "__main__":
    main(sys.argv[1:] or ["small"])
