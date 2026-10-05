#!/usr/bin/env python3
"""Contrôle du G2P FR du moteur NeuTTS avant de produire un vocal.

Usage :
    ~/.hermes/neutts/venv/bin/python scripts/g2p-check.py "de 19 h a 23 h" "de 19 heures a 23 heures"

Sans argument : jeu de tests par défaut (heures, abréviations, décimales).
But : voir la séquence phonétique réellement envoyée au modèle. Une lettre isolée
apparaît comme son nom (ˈaʃ pour « h », ˌɛʁdˌevˈe pour « RDV ») -> réécrire le texte
en toutes lettres AVANT de lancer text_to_speech.
"""

import sys

DEFAULTS = [
    "de 19 h a 23 h",
    "de 19 heures a 23 heures",
    "RDV 14h30",
    "rendez-vous a 14 heures 30",
    "1,5 h",
    "une heure et demie",
]


def main() -> int:
    from neutts.phonemizers import BasePhonemizer

    tests = sys.argv[1:] or DEFAULTS
    # fr-fr : même code que BACKBONE_LANGUAGE_MAP pour neutts-nano-french-q4-gguf.
    ph = BasePhonemizer(language_code="fr-fr")
    out = ph.phonemize(tests)
    for src, phones in zip(tests, out):
        flag = "  <-- lettre epelée" if "ˈaʃ" in phones or "ˌɛʁd" in phones else ""
        print(f"{src!r:34} -> {phones.strip()}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
