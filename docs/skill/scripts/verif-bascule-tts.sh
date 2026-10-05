#!/bin/sh
# Vérifie qu'une chaîne TTS bascule bien sur le moteur DISTANT, et qu'aucun moteur local résiduel
# ne répond à sa place (c'est ce moteur local qui renvoie un 401 quand le jeton distant est attendu).
#
# Usage : verif-bascule-tts.sh [port]        (défaut 8130)
# Sortie : 0 = bascule prouvée ; 1 = moteur local résiduel ou distant non chargé ; 2 = NEUTTS_URL absent.
#
# Le fichier d'env est lu DANS le script (jamais sur la ligne de commande, où le garde-fou d'égress
# bloque tout segment qui le lit) et le jeton n'est jamais affiché : /health passe sans jeton.
set -u
PORT="${1:-8130}"
ENVF="${NEUTTS_ENV:-$HOME/.hermes/neutts.env}"

URL=$(sed -n 's/^NEUTTS_URL=//p' "$ENVF" 2>/dev/null | head -1)
if [ -z "$URL" ]; then
  echo "ÉCHEC : NEUTTS_URL absent de $ENVF -> client.py visera 127.0.0.1:$PORT (moteur local)."
  exit 2
fi

if ss -lnpt 2>/dev/null | grep -qE "[:.]$PORT([[:space:]]|$)"; then
  echo "ÉCHEC : un moteur écoute LOCALEMENT sur $PORT -> la bascule n'a pas eu lieu (401 probable)."
  ss -lnpt 2>/dev/null | grep -E "[:.]$PORT([[:space:]]|$)"
  echo "Le tuer (kill <pid>) puis relancer la mesure."
  exit 1
fi
echo "OK : aucun moteur local sur $PORT."

H=$(curl -s -m 5 "$URL/health") || { echo "ÉCHEC : pas de réponse de $URL/health"; exit 1; }
echo "distant $URL : $H"
case "$H" in
  *'"loaded":true'*|*'"loaded": true'*) echo "OK : moteur distant chargé, la chaîne passe par $URL." ;;
  *) echo "ÉCHEC : moteur distant non chargé (loaded != true) — attendre la fin du preload."; exit 1 ;;
esac
