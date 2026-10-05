#!/usr/bin/env python3
"""Dump des metadonnees d'un GGUF sans charger le modele.

Usage : gguf-meta.py <modele.gguf> [--all]

Sert a verifier ce qu'un backbone TTS sait faire avant de l'annoncer : `neuphonic.input_format`,
presence d'un chat template, tokens d'emotion, architecture, quantification.

Aucune dependance : l'en-tete GGUF est parse a la main. Le sous-module `llama_cpp.gguf` n'existe
pas dans toutes les versions de llama-cpp-python installees, donc ne pas en dependre.
"""
import argparse
import struct
import sys

T_U8, T_I8, T_U16, T_I16, T_U32, T_I32, T_F32, T_BOOL, T_STR, T_ARR, T_U64, T_I64, T_F64 = range(13)
FIXED = {T_U8: ("<B", 1), T_I8: ("<b", 1), T_U16: ("<H", 2), T_I16: ("<h", 2),
         T_U32: ("<I", 4), T_I32: ("<i", 4), T_F32: ("<f", 4), T_BOOL: ("<B", 1),
         T_U64: ("<Q", 8), T_I64: ("<q", 8), T_F64: ("<d", 8)}

def _trunc(v, n=300):
    s = repr(v) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n] + " ...[tronque]"

class Reader:
    def __init__(self, fh):
        self.fh = fh

    def raw(self, n):
        b = self.fh.read(n)
        if len(b) != n:
            raise EOFError("fin de fichier inattendue")
        return b

    def u32(self):
        return struct.unpack("<I", self.raw(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.raw(8))[0]

    def string(self):
        return self.raw(self.u64()).decode("utf-8", "replace")

    def value(self, t, depth=0):
        if t == T_STR:
            return self.string()
        if t == T_ARR:
            if depth:
                raise ValueError("tableau imbrique non supporte")
            etype, count = self.u32(), self.u64()
            return [self.value(etype, depth + 1) for _ in range(count)]
        fmt, size = FIXED[t]
        return struct.unpack(fmt, self.raw(size))[0]

def read_meta(path):
    with open(path, "rb") as fh:
        r = Reader(fh)
        if r.raw(4) != b"GGUF":
            raise ValueError("ce fichier n'est pas un GGUF")
        version, n_tensors, n_kv = r.u32(), r.u64(), r.u64()
        meta = {}
        for _ in range(n_kv):
            key = r.string()
            meta[key] = r.value(r.u32())
    return version, n_tensors, meta

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--all", action="store_true", help="tout afficher (defaut : cles utiles)")
    a = ap.parse_args()

    version, n_tensors, meta = read_meta(a.path)
    print("fichier      : %s" % a.path)
    print("GGUF v%d, %d tenseurs, %d metadonnees" % (version, n_tensors, len(meta)))

    printed = False
    for key, val in sorted(meta.items()):
        if not a.all:
            interesting = (key.startswith("neuphonic")
                           or "emotion" in key.lower()
                           or key in ("tokenizer.chat_template", "general.architecture",
                                      "general.name", "general.file_type"))
            if not interesting:
                continue
        print("%-32s = %s" % (key, _trunc(val)))
        printed = True
    if not printed and not a.all:
        print("(aucune cle utile : relancer avec --all)")

    print("\nverdict :")
    ifmt = meta.get("neuphonic.input_format")
    tpl = meta.get("tokenizer.chat_template")
    if ifmt:
        print("  input_format declare = %s (source de verite du moteur)" % ifmt)
    elif tpl:
        print("  input_format absent, chat template present -> le moteur peut classer en BPE")
    else:
        print("  input_format absent et aucun chat template -> repli phonemes")
        print("  => pas de tag d'emotion (ValueError: Emotion is only supported by BPE models)")
    if not ifmt and not tpl:
        print("  => le phonemiseur suit le backbone (BACKBONE_LANGUAGE_MAP), pas de parametre de ton")
    return 0

if __name__ == "__main__":
    sys.exit(main())
