#!/usr/bin/env python3
"""NeuTTS synthesis helper (French) for Hermes command provider.

Usage:
    python say.py --text-file t.txt --out out.wav [--model REPO] [--device cpu]
"""
import argparse
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "neuphonic/neutts-nano-french-q4-gguf"
DEFAULT_CODEC = "neuphonic/neucodec"


def _write_wav(path, samples, sample_rate=24000):
    import numpy as np

    if not isinstance(samples, np.ndarray):
        samples = np.array(samples, dtype=np.float32)
    pcm = (np.clip(samples.flatten(), -1.0, 1.0) * 32767).astype(np.int16)
    data_size = len(pcm) * 2
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", data_size) + pcm.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--codec", default=DEFAULT_CODEC)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--ref-audio", default=str(HERE / "refs" / "juliette.wav"))
    ap.add_argument("--ref-text", default=str(HERE / "refs" / "juliette.txt"))
    a = ap.parse_args()

    text = Path(a.text_file).read_text(encoding="utf-8").strip()
    if not text:
        print("Error: empty text", file=sys.stderr)
        return 1
    ref_text = Path(a.ref_text).read_text(encoding="utf-8-sig").strip()

    from neutts import NeuTTS

    tts = NeuTTS(
        backbone_repo=a.model,
        backbone_device="gpu" if a.device == "cuda" else a.device,
        codec_repo=a.codec,
        codec_device=a.device,
    )
    wav = tts.infer(text, tts.encode_reference(a.ref_audio), ref_text)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import soundfile as sf

        sf.write(str(out), wav, 24000)
    except ImportError:
        _write_wav(str(out), wav, 24000)
    print(f"OK: {out} ({len(text)} chars)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
