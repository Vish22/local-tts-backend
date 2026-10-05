#!/usr/bin/env python3
"""Client du serveur TTS neutts : local (résident, réveillé si besoin) ou distant (portgpu).

Usage :
    client.py --text-file t.txt --out out.wav [--voice juliette] [--model REPO]
    client.py --status        # état du serveur (JSON)
    client.py --release       # arrêt propre du serveur

Jeton : NEUTTS_TOKEN, ou NEUTTS_TOKEN_FILE (contenu d'un fichier chmod 600, secret Docker).
Refus 4xx du serveur (texte trop long, jeton invalide...) : erreur explicite, sans repli
coûteux ; 5xx ou réseau : repli say.py (cold start) comme avant.

Cible : NEUTTS_URL (défaut http://127.0.0.1:8130 ; schémas http/https uniquement). Si l'URL
n'est pas locale, le serveur distant écrit le WAV chez lui et le client le rapatrie par
GET /audio/<name> : le chemin de sortie du client n'est jamais interprété par un hôte distant.
Un WAV rapatrié est refusé au-delà de NEUTTS_MAX_AUDIO octets (défaut 64 Mio).

Repli : si le serveur ne répond pas (ou sert un autre modèle), la synthèse passe par say.py
comme avant — aucune régression possible, seulement le cold start.

Serveur distant : le client attend son démarrage jusqu'à NEUTTS_REMOTE_WAIT secondes
(défaut 90) avant de replier — un moteur résident redémarré (reboot, mise à jour) ne
provoque donc pas de repli coûteux.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("NEUTTS_PORT", "8130"))
# Serveur TTS : local par défaut, ou distant (ex. http://10.1.33.71:8130) via NEUTTS_URL.
BASE = os.environ.get("NEUTTS_URL", "http://127.0.0.1:%d" % PORT).rstrip("/")
MAX_AUDIO = int(os.environ.get("NEUTTS_MAX_AUDIO", str(64 * 1024 * 1024)))


def _read_token():
    """Jeton d'accès : NEUTTS_TOKEN, sinon contenu de NEUTTS_TOKEN_FILE (fichier 600)."""
    tok = os.environ.get("NEUTTS_TOKEN", "")
    if not tok:
        path = os.environ.get("NEUTTS_TOKEN_FILE", "")
        if path:
            try:
                tok = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                sys.stderr.write("neutts: jeton illisible dans %s (%s)\n" % (path, exc))
    return tok


TOKEN = _read_token()
START_TIMEOUT = float(os.environ.get("NEUTTS_START_TIMEOUT", "240"))
INFER_TIMEOUT = float(os.environ.get("NEUTTS_INFER_TIMEOUT", "1800"))
SERVER = HERE / "serve.py"
LOG = HERE / "serve.log"
DEFAULT_MODEL = "neuphonic/neutts-nano-french-q4-gguf"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}  # 0.0.0.0 n'est pas une cible cliente
SCHEME = urllib.parse.urlsplit(BASE).scheme
REMOTE = (urllib.parse.urlsplit(BASE).hostname or "") not in LOCAL_HOSTS
REMOTE_WAIT = float(os.environ.get("NEUTTS_REMOTE_WAIT", "90"))


def _headers(extra=None):
    head = {"Content-Type": "application/json"}
    if TOKEN:
        head["X-Neutts-Token"] = TOKEN
    if extra:
        head.update(extra)
    return head


def _post(path, payload=None, timeout=5.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _get(path, timeout=3.0):
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=_headers()),
                                timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _get_bytes(path, timeout=60.0):
    """WAV distant, borné : un hôte distant ne peut pas saturer la mémoire du client."""
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=_headers()),
                                timeout=timeout) as resp:
        data = resp.read(MAX_AUDIO + 1)
    if len(data) > MAX_AUDIO:
        raise ValueError("WAV distant > %d octets (refuse)" % MAX_AUDIO)
    return data


def _http_error_text(exc):
    """Message d'erreur renvoyé par le serveur (corps JSON), sinon l'exception."""
    try:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body).get("error") or body[:200]
        except ValueError:
            return body[:200]
    except Exception:
        return str(exc)


def health(timeout=3.0):
    try:
        return _get("/health", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def model_matches(model):
    """Le serveur tourne-t-il, et sert-il bien le modèle demandé ?"""
    info = health()
    if not info:
        return False
    return not model or info.get("model") == model


def wait_remote(model, budget):
    """Attend qu'un serveur distant soit prêt (démarrage après reboot).

    Rend False immédiatement si un serveur répond mais sert un autre modèle
    (attendre n'y changerait rien) ou si le budget est épuisé.
    """
    t0 = time.time()
    while budget > 0 and time.time() - t0 < budget:
        if model_matches(model):
            sys.stderr.write("neutts: serveur distant prêt après %.0f s\n" % (time.time() - t0))
            return True
        if health():
            return False
        time.sleep(2)
    return False


def start_server():
    with open(LOG, "ab") as log:
        subprocess.Popen([sys.executable, str(SERVER)], cwd=str(HERE),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)


def ensure_server(model):
    if model_matches(model):
        return True
    if health():
        sys.stderr.write("neutts: un serveur sert un autre modèle -> repli direct\n")
        return False
    start_server()
    t0 = time.time()
    while time.time() - t0 < START_TIMEOUT:
        if model_matches(model):
            sys.stderr.write("neutts: serveur démarré en %.0f s\n" % (time.time() - t0))
            return True
        time.sleep(2)
    sys.stderr.write("neutts: serveur indisponible après %.0f s -> repli direct\n" % START_TIMEOUT)
    return False


def fallback(text_file, out, model):
    """Ancien comportement : process neuf, cold start."""
    cmd = [sys.executable, str(HERE / "say.py"), "--text-file", str(text_file),
           "--out", str(out), "--model", model]
    return subprocess.call(cmd)


def say_remote(text, out, voice):
    """Serveur distant : synthèse chez lui, WAV rapatrié ici."""
    res = _post("/say", {"text": text, "voice": voice}, timeout=INFER_TIMEOUT)
    if not res.get("ok"):
        sys.stderr.write("neutts: erreur serveur (%s)\n" % res.get("error"))
        return False
    name = res.get("name")
    if not name:
        sys.stderr.write("neutts: serveur distant sans nom de sortie -> repli direct\n")
        return False
    data = _get_bytes("/audio/%s" % name)
    dest = Path(out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(str(tmp), str(dest))
    sys.stderr.write("OK: %s (%d chars, %.1f s, %.1f ko)\n"
                     % (out, res.get("chars", 0), res.get("seconds", 0.0), len(data) / 1024.0))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file")
    ap.add_argument("--out")
    ap.add_argument("--voice", default="juliette")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--release", action="store_true")
    a = ap.parse_args()

    if SCHEME not in ("http", "https"):
        sys.stderr.write("neutts: NEUTTS_URL doit commencer par http:// ou https:// (%r)\n" % BASE)
        return 2
    if a.status:
        print(json.dumps(health() or {"ok": False, "error": "serveur absent"}, indent=2))
        return 0
    if a.release:
        try:
            print(json.dumps(_post("/release", {})))
        except (urllib.error.URLError, OSError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
        return 0
    if not a.text_file or not a.out:
        ap.error("--text-file et --out sont requis")

    text = Path(a.text_file).read_text(encoding="utf-8").strip()
    if not text:
        sys.stderr.write("Error: empty text\n")
        return 1

    if REMOTE:
        if not model_matches(a.model) and not wait_remote(a.model, REMOTE_WAIT):
            sys.stderr.write("neutts: serveur distant injoignable (%s) -> repli direct\n" % BASE)
            return fallback(a.text_file, a.out, a.model)
        try:
            if say_remote(text, a.out, a.voice):
                return 0
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                sys.stderr.write("neutts: refusé par le serveur (HTTP %d : %s)\n"
                                 % (exc.code, _http_error_text(exc)))
                return 2
            sys.stderr.write("neutts: échec via serveur distant (%s) -> repli direct\n" % exc)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            sys.stderr.write("neutts: échec via serveur distant (%s) -> repli direct\n" % exc)
        return fallback(a.text_file, a.out, a.model)

    if not ensure_server(a.model):
        return fallback(a.text_file, a.out, a.model)

    try:
        res = _post("/say", {"text": text, "out": str(Path(a.out).resolve()), "voice": a.voice},
                    timeout=INFER_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            sys.stderr.write("neutts: refusé par le serveur (HTTP %d : %s)\n"
                             % (exc.code, _http_error_text(exc)))
            return 2
        sys.stderr.write("neutts: échec via serveur (%s) -> repli direct\n" % exc)
        return fallback(a.text_file, a.out, a.model)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stderr.write("neutts: échec via serveur (%s) -> repli direct\n" % exc)
        return fallback(a.text_file, a.out, a.model)
    if not res.get("ok"):
        sys.stderr.write("neutts: erreur serveur (%s) -> repli direct\n" % res.get("error"))
        return fallback(a.text_file, a.out, a.model)
    sys.stderr.write("OK: %s (%d chars, %.1f s)\n" % (a.out, res.get("chars", 0),
                                                      res.get("seconds", 0.0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
