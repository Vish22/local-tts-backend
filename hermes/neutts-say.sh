#!/bin/sh
# Hermes TTS command provider -> NeuTTS (local, French) via serveur résident.
# Le serveur (serve.py) est démarré à la demande et s'arrête seul après inactivité.
# Usage: neutts-say.sh <text_file> <output_path> [model_repo]
set -eu

ESPEAK_LIB=/home/hermes/.hermes/opt/espeak-ng/usr/lib/x86_64-linux-gnu
export LD_LIBRARY_PATH="$ESPEAK_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export ESPEAK_DATA_PATH="$ESPEAK_LIB/espeak-ng-data"
export PATH="/home/hermes/.hermes/opt/espeak-ng/usr/bin:$PATH"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export TOKENIZERS_PARALLELISM=false

# Jeton HF lecture seule (mode 600), requis par les dépôts Neuphonic gated.
if [ -f /home/hermes/.hermes/neutts.env ]; then
  . /home/hermes/.hermes/neutts.env
fi
: "${HF_TOKEN:=}"
export HF_TOKEN

TEXT_FILE="$1"
OUT="$2"
MODEL="${3:-neuphonic/neutts-nano-french-q4-gguf}"
export NEUTTS_MODEL="$MODEL"

exec /home/hermes/.hermes/neutts/venv/bin/python /home/hermes/.hermes/neutts/client.py \
  --text-file "$TEXT_FILE" --out "$OUT" --model "$MODEL"
