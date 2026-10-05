#!/bin/sh
# Libère tout de suite le serveur TTS résident (sinon il s'arrête seul après 5 min d'inactivité).
# Usage : neutts-release.sh   |   neutts-release.sh status
set -eu
PY=/home/hermes/.hermes/neutts/venv/bin/python
CLIENT=/home/hermes/.hermes/neutts/client.py
case "${1:-release}" in
  status) exec "$PY" "$CLIENT" --status ;;
  *)      exec "$PY" "$CLIENT" --release ;;
esac
