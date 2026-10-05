#!/usr/bin/env python3
"""Vérifie un audio TTS livré : durée réelle + transcription ASR, comparaison au texte source.

Usage :
  /home/hermes/.hermes/hermes-agent/venv/bin/python tts-verif.py <audio> [texte_source.txt]

Pourquoi : au-delà de ~250 caractères le moteur local tronque puis répète, tout en produisant un
fichier de durée plausible. Seule la relecture ASR du fichier livré le montre.

Dépendances : ffmpeg/ffprobe dans le PATH, faster_whisper (présent dans le venv Hermes, celui du
STT entrant). Modèle surchargeable via TTS_VERIF_MODEL (défaut : base).
"""

import json
import os
import subprocess
import sys
import tempfile


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def to_16k_mono(path: str) -> str:
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="tts-verif-")
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", path, "-ac", "1", "-ar", "16000", wav],
        check=True,
    )
    return wav


def transcribe(path: str) -> str:
    from faster_whisper import WhisperModel  # import tardif : inutile si seule la durée est demandée

    model = WhisperModel(os.environ.get("TTS_VERIF_MODEL", "base"), device="cpu", compute_type="int8")
    segments, _info = model.transcribe(path, language="fr", beam_size=1)
    return " ".join(s.text.strip() for s in segments)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    audio = sys.argv[1]
    source_path = sys.argv[2] if len(sys.argv) > 2 else None
    if not os.path.isfile(audio):
        print(f"introuvable : {audio}")
        return 2

    dur = probe_duration(audio)
    print(f"fichier    : {audio}")
    print(f"duree      : {dur:.1f} s")

    wav = to_16k_mono(audio)
    try:
        text = transcribe(wav)
    finally:
        os.unlink(wav)
    print(f"transcript : {text}")

    if source_path:
        source = open(source_path, encoding="utf-8").read().strip()
        n_src, n_asr = len(source), len(text)
        print(f"source     : {n_src} car -> {n_src / dur:.1f} car/s")
        print(f"asr        : {n_asr} car -> {n_asr / dur:.1f} car/s")
        # Seuil large : l'ASR perd/ajoute des mots, mais une chute de plus de 35 % = texte non restitue.
        if n_asr < 0.65 * n_src:
            print("VERDICT    : NON FIDELE (tronque/repete) -> decouper le texte par phrase")
            return 1
        print("VERDICT    : fidelite plausible (aucun signe de troncature)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
