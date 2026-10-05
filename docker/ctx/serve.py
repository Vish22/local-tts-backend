#!/usr/bin/env python3
"""Serveur TTS NeuTTS résident — local (127.0.0.1) ou distant (LAN).

Endpoints (JSON) :
  GET  /health        -> {"ok", "model", "loaded", "uptime_s", "idle_s", "in_flight", "out_keep_s"}
                        (aucun chemin de fichier : route non authentifiee)
  POST /say           <- {"text": "...", "out": "/chemin/local.wav"?, "voice": "juliette"?}
                      -> {"ok": true, "name": "say_....wav", "seconds": 2.4, "bytes": 123456,
                          "chars": 42, "path": "/var/lib/neutts/out/say_....wav"}
  GET  /audio/<name>  -> le WAV correspondant (nom simple uniquement, pas de chemin)
  POST /release       -> arrêt propre immédiat (loopback, ou NEUTTS_ALLOW_RELEASE=1)

Un appelant distant ne peut pas nommer un chemin de sortie : /say sans "out" (ou avec un
chemin hors de NEUTTS_OUT_DIR) écrit dans NEUTTS_OUT_DIR et renvoie "name" ; le WAV se
récupère ensuite par GET /audio/<name>. Un appelant local garde l'ancien comportement
("out" absolu respecté), ce qui reste compatible avec les scripts existants.

Variables d'environnement :
  NEUTTS_HOST          adresse d'écoute (défaut 127.0.0.1 ; 0.0.0.0 = accessible LAN)
  NEUTTS_PORT          port d'écoute (défaut 8130)
  NEUTTS_IDLE_TIMEOUT  secondes d'inactivité avant sortie (défaut 300) ; 0 = jamais
  NEUTTS_MODEL         dépôt backbone (défaut neuphonic/neutts-nano-french-q4-gguf)
  NEUTTS_CODEC         dépôt codec (défaut neuphonic/neucodec)
  NEUTTS_VOICE         voix par défaut (défaut juliette)
  NEUTTS_REFS          dossier des références de voix (défaut <ce dossier>/refs)
  NEUTTS_LOG           journal (défaut <ce dossier>/serve.log)
  NEUTTS_OUT_DIR       dossier de sortie (défaut <ce dossier>/out)
  NEUTTS_TOKEN         si défini, exige l'en-tête X-Neutts-Token (sauf /health).
                       Absent + écoute non-loopback = refus de démarrer (fail-closed).
  NEUTTS_TOKEN_FILE    lire le jeton depuis ce fichier s'il n'est pas dans l'environnement
                       (secret Docker : /run/secrets/..., ou fichier chmod 600)
  NEUTTS_MAX_CHARS     plafond de caractères par /say (défaut 4000)
  NEUTTS_MAX_BODY      plafond du corps JSON en octets (défaut 65536)
  NEUTTS_MAX_CONN      connexions simultanées max (défaut 16)
  NEUTTS_OUT_KEEP_S    durée de conservation des WAV de sortie (défaut 86400 s)
  NEUTTS_ALLOW_NO_TOKEN  =1 pour accepter une écoute LAN sans jeton (déconseillé)
  NEUTTS_ALLOW_RELEASE =1 autorise POST /release hors loopback (défaut : refusé)
  NEUTTS_ENV_FILE      fichier d'environnement supplémentaire à charger (facultatif)
"""
import hmac
import json
import os
import re
import signal
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

HOST = os.environ.get("NEUTTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NEUTTS_PORT", "8130"))
IDLE_TIMEOUT = float(os.environ.get("NEUTTS_IDLE_TIMEOUT", "300"))
DEFAULT_MODEL = os.environ.get("NEUTTS_MODEL", "neuphonic/neutts-nano-french-q4-gguf")
DEFAULT_CODEC = os.environ.get("NEUTTS_CODEC", "neuphonic/neucodec")
DEFAULT_VOICE = os.environ.get("NEUTTS_VOICE", "juliette")
REFS = Path(os.environ.get("NEUTTS_REFS", str(HERE / "refs")))
LOG = Path(os.environ.get("NEUTTS_LOG", str(HERE / "serve.log")))
OUT_DIR = Path(os.environ.get("NEUTTS_OUT_DIR", str(HERE / "out")))
TOKEN = os.environ.get("NEUTTS_TOKEN", "")
OUT_KEEP_S = float(os.environ.get("NEUTTS_OUT_KEEP_S", "86400"))  # purge des WAV de sortie
MAX_CHARS = int(os.environ.get("NEUTTS_MAX_CHARS", "4000"))       # plafond de texte par requête
MAX_BODY = int(os.environ.get("NEUTTS_MAX_BODY", "65536"))        # plafond du corps JSON (octets)
MAX_CONN = int(os.environ.get("NEUTTS_MAX_CONN", "16"))           # connexions simultanées
ALLOW_NO_TOKEN = os.environ.get("NEUTTS_ALLOW_NO_TOKEN") == "1"   # écoute LAN sans jeton (déconseillé)
ALLOW_RELEASE = os.environ.get("NEUTTS_ALLOW_RELEASE") == "1"     # /release hors loopback (déconseillé)

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}\.wav$")

_lock = threading.Lock()          # sérialise l'inférence (modèle non réentrant)
_state_lock = threading.Lock()
_load_lock = threading.Lock()     # un seul chargement du moteur (préchargement vs /say)
_preloading = threading.Event()   # vrai pendant le chargement initial (protège du watchdog idle)
_in_flight = 0
_last_used = time.time()
_started = time.time()
_engine = None
_ref_cache = {}
_shutdown = threading.Event()


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    # Le wrapper peut rediriger stderr vers ce même fichier : ne pas doubler les lignes.
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    try:
        if os.isatty(sys.stderr.fileno()):
            sys.stderr.write(line)
            sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass


def _bootstrap_env():
    """Rend le process autonome : espeak-ng local + jeton HF, comme le wrapper.

    Le paquet neutts embarque son propre libespeak-ng + espeak-ng-data ; les variables
    ci-dessous ne servent qu'aux installations qui utilisent un espeak-ng externe.
    """
    for base in (Path("/home/hermes/.hermes/opt/espeak-ng/usr"),
                 Path("/opt/neutts/espeak-ng/usr")):
        lib = base / "lib/x86_64-linux-gnu"
        if lib.is_dir():
            os.environ["LD_LIBRARY_PATH"] = "%s:%s" % (lib, os.environ.get("LD_LIBRARY_PATH", ""))
            os.environ["ESPEAK_DATA_PATH"] = str(lib / "espeak-ng-data")
            os.environ["PATH"] = "%s:%s" % (base / "bin", os.environ.get("PATH", ""))
            break
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    # Fichiers d'environnement : chemin explicite, puis neutts.env à côté du dossier
    # d'installation (hôte Hermes), puis /etc/neutts.env. Aucun chemin absolu d'hôte en dur :
    # dans l'image (HERE=/opt/neutts) seul /etc/neutts.env peut exister.
    env_files = [Path(x) for x in (os.environ.get("NEUTTS_ENV_FILE", ""),) if x]
    env_files += [HERE.parent / "neutts.env", Path("/etc/neutts.env")]
    for env_file in env_files:
        if not env_file.is_file():
            continue
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


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


def load_engine():
    """Charge backbone + codec (une seule fois, thread-safe)."""
    global _engine
    if _engine is not None:
        return _engine
    with _load_lock:
        if _engine is None:
            t0 = time.time()
            from neutts import NeuTTS

            log("chargement moteur %s ..." % DEFAULT_MODEL)
            _engine = NeuTTS(backbone_repo=DEFAULT_MODEL, backbone_device="cpu",
                             codec_repo=DEFAULT_CODEC, codec_device="cpu")
            log("moteur chargé en %.1f s (pic mémoire non mesuré ici)" % (time.time() - t0))
    return _engine


def _reference(voice):
    """(codes de référence, texte de référence) mis en cache par voix."""
    if voice in _ref_cache:
        return _ref_cache[voice]
    wav = REFS / ("%s.wav" % voice)
    txt = REFS / ("%s.txt" % voice)
    if not wav.is_file() or not txt.is_file():
        raise FileNotFoundError("référence voix introuvable : %s" % wav)
    codes = load_engine().encode_reference(str(wav))
    text = txt.read_text(encoding="utf-8-sig").strip()
    _ref_cache[voice] = (codes, text)
    return _ref_cache[voice]


def _prune_out_dir():
    try:
        now = time.time()
        for old in list(OUT_DIR.glob("*.wav")) + list(OUT_DIR.glob("*.wav.part")):
            if now - old.stat().st_mtime > OUT_KEEP_S:
                old.unlink()
    except OSError:
        pass


def _resolve_out(requested, local=False):
    """Chemin de sortie serveur.

    Un chemin explicite n'est accepté que depuis le loopback (client local de confiance) :
    un appelant distant ne choisit jamais le fichier écrit (sinon un porteur du jeton
    pourrait écraser n'importe quel fichier accessible en écriture par l'utilisateur du service).
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if requested and local:
        try:
            cand = Path(requested)
            if cand.is_absolute():
                return cand
        except OSError:
            pass
    name = "say_%d_%d_%04d.wav" % (int(time.time()), os.getpid(), int(time.time() * 997) % 10000)
    return OUT_DIR / name


def synthesize(text, out_path, voice):
    with _lock:
        engine = load_engine()
        codes, ref_text = _reference(voice)
        t0 = time.time()
        wav = engine.infer(text, codes, ref_text)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
        try:
            import soundfile as sf

            sf.write(str(tmp), wav, 24000, format="WAV")
        except ImportError:
            _write_wav(str(tmp), wav, 24000)
        os.replace(str(tmp), str(out))
        return time.time() - t0, out.stat().st_size


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "neutts-serve/3.3"
    timeout = 30  # une connexion keep-alive inactive ne bloque pas un thread indéfiniment

    def log_message(self, fmt, *args):  # silence le log HTTP par défaut
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _authorized(self):
        if not TOKEN:
            return True
        given = self.headers.get("X-Neutts-Token", "")
        return hmac.compare_digest(given.encode("utf-8"), TOKEN.encode("utf-8"))

    def _deny(self):
        self._json(401, {"ok": False, "error": "jeton absent ou invalide"})

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/health":  # volontairement sans jeton (supervision)
            return self._json(200, {"ok": True, "model": DEFAULT_MODEL,
                                    "codec": DEFAULT_CODEC, "voice": DEFAULT_VOICE,
                                    "loaded": _engine is not None,
                                    "preloading": _preloading.is_set(),
                                    "idle_s": round(time.time() - _last_used, 1),
                                    "uptime_s": round(time.time() - _started, 1),
                                    "idle_timeout": IDLE_TIMEOUT,
                                    "in_flight": _in_flight,
                                    "out_keep_s": OUT_KEEP_S})  # reseau : pas de chemin de fichier
        if not self._authorized():
            return self._deny()
        if not route.startswith("/audio/"):
            return self._json(404, {"ok": False, "error": "not found"})
        name = route[len("/audio/"):]
        if not NAME_RE.match(name):
            return self._json(400, {"ok": False, "error": "nom invalide"})
        path = OUT_DIR / name
        if not path.is_file():
            return self._json(404, {"ok": False, "error": "introuvable"})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global _in_flight, _last_used
        route = self.path.split("?")[0]
        if not self._authorized():
            return self._deny()
        if route == "/release":
            if not (ALLOW_RELEASE or self._local()):
                return self._json(403, {"ok": False,
                                        "error": "release refuse depuis un appelant distant"})
            with _state_lock:
                _last_used = time.time()
            self._json(200, {"ok": True, "action": "release"})
            log("release demandé -> arrêt")
            threading.Thread(target=_shutdown.set, daemon=True).start()
            return
        if route != "/say":
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY:
                return self._json(413, {"ok": False, "error": "corps invalide (> %d octets ou < 0)" % MAX_BODY})
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                return self._json(400, {"ok": False, "error": "corps JSON : objet attendu"})
            raw_text = payload.get("text")
            if not isinstance(raw_text, str):
                return self._json(400, {"ok": False, "error": "text est requis (chaîne)"})
            text = raw_text.strip()
            requested = payload.get("out") or ""
            if not isinstance(requested, str):
                return self._json(400, {"ok": False, "error": "out invalide"})
            voice = payload.get("voice") or DEFAULT_VOICE
            if not isinstance(voice, str):
                return self._json(400, {"ok": False, "error": "voice invalide"})
            if not text:
                return self._json(400, {"ok": False, "error": "text est requis"})
            if len(text) > MAX_CHARS:
                return self._json(413, {"ok": False, "error": "text > %d caractères" % MAX_CHARS,
                                        "chars": len(text), "max_chars": MAX_CHARS})
            if not re.match(r"^[A-Za-z0-9_-]{1,40}$", voice):
                return self._json(400, {"ok": False, "error": "voice invalide"})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json(400, {"ok": False, "error": "json invalide : %s" % exc})

        with _state_lock:
            _in_flight += 1
            _last_used = time.time()
        try:
            _prune_out_dir()
            out = _resolve_out(requested, local=self._local())
            seconds, size = synthesize(text, out, voice)
        except Exception as exc:  # noqa: BLE001
            log("erreur synthèse : %r" % exc)
            # Pas de str(exc) vers le réseau : peut contenir des chemins de fichiers.
            return self._json(500, {"ok": False,
                                    "error": "échec de synthèse (%s) — voir journal serveur"
                                             % type(exc).__name__})
        finally:
            with _state_lock:
                _in_flight -= 1
                _last_used = time.time()
        log("say %d chars -> %s en %.1f s (%d octets)" % (len(text), out, seconds, size))
        self._json(200, {"ok": True, "seconds": round(seconds, 2), "bytes": size,
                         "out": str(out), "path": str(out), "name": out.name,
                         "chars": len(text)})


def _idle_watchdog():
    while not _shutdown.wait(5):
        if IDLE_TIMEOUT <= 0:
            continue
        with _state_lock:
            idle = time.time() - _last_used
            busy = _in_flight > 0
        if not busy and not _preloading.is_set() and idle > IDLE_TIMEOUT:
            log("inactivité %.0f s > %.0f s -> arrêt" % (idle, IDLE_TIMEOUT))
            _shutdown.set()
            return


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer avec borne de connexions simultanées."""

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(max(1, MAX_CONN))
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(timeout=5):
            try:
                request.close()  # trop de connexions : refuser plutôt qu'empiler des threads
            except OSError:
                pass
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _preload():
    """Charge le moteur en tâche de fond : /health répond pendant le chargement."""
    _preloading.set()
    try:
        load_engine()
        try:  # pré-chauffe l'import (coûteux au premier appel) hors du chemin de requête
            import soundfile  # noqa: F401
        except ImportError:
            pass
    except Exception as exc:  # noqa: BLE001
        log("échec du préchargement : %r" % exc)
    finally:
        _preloading.clear()


def _reload_env_settings():
    """Réapplique l'environnement APRÈS _bootstrap_env().

    Les constantes ci-dessus sont évaluées à l'import du module : sans cette relecture, un
    NEUTTS_* posé par /etc/neutts.env ou ~/.hermes/neutts.env (jeton, écoute, plafonds)
    serait ignoré — le fail-closed refuserait alors de démarrer à tort. Sans effet en
    conteneur, où l'environnement est déjà complet au lancement.
    """
    g = globals()
    for name, var, cast in (("HOST", "NEUTTS_HOST", str), ("PORT", "NEUTTS_PORT", int),
                            ("IDLE_TIMEOUT", "NEUTTS_IDLE_TIMEOUT", float),
                            ("OUT_KEEP_S", "NEUTTS_OUT_KEEP_S", float),
                            ("DEFAULT_MODEL", "NEUTTS_MODEL", str),
                            ("DEFAULT_CODEC", "NEUTTS_CODEC", str),
                            ("DEFAULT_VOICE", "NEUTTS_VOICE", str),
                            ("MAX_CHARS", "NEUTTS_MAX_CHARS", int),
                            ("MAX_BODY", "NEUTTS_MAX_BODY", int),
                            ("MAX_CONN", "NEUTTS_MAX_CONN", int)):
        raw = os.environ.get(var)
        if raw:
            try:
                g[name] = cast(raw)
            except ValueError:
                log("valeur ignorée pour %s=%r" % (var, raw))
    for name, var in (("REFS", "NEUTTS_REFS"), ("LOG", "NEUTTS_LOG"), ("OUT_DIR", "NEUTTS_OUT_DIR")):
        raw = os.environ.get(var)
        if raw:
            g[name] = Path(raw)
    tok = os.environ.get("NEUTTS_TOKEN", "")
    tok_file = os.environ.get("NEUTTS_TOKEN_FILE", "")
    if not tok and tok_file:
        try:
            tok = Path(tok_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            log("jeton illisible (%s) : %s" % (tok_file, exc))
    g["TOKEN"] = tok
    g["ALLOW_NO_TOKEN"] = os.environ.get("NEUTTS_ALLOW_NO_TOKEN") == "1"
    g["ALLOW_RELEASE"] = os.environ.get("NEUTTS_ALLOW_RELEASE") == "1"


def main():
    _bootstrap_env()
    _reload_env_settings()
    if not TOKEN and not ALLOW_NO_TOKEN and HOST not in ("127.0.0.1", "localhost", "::1"):
        msg = ("REFUS : NEUTTS_TOKEN absent alors que l'écoute est %s "
               "(fail-closed). Définir NEUTTS_TOKEN, ou NEUTTS_ALLOW_NO_TOKEN=1 pour l'assumer." % HOST)
        log(msg)
        sys.stderr.write(msg + "\n")
        return 2
    log("démarrage serveur neutts sur %s:%d (idle %s s, sortie %s)" % (HOST, PORT, IDLE_TIMEOUT, OUT_DIR))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _shutdown.set())
    httpd = Server((HOST, PORT), Handler)
    threading.Thread(target=_idle_watchdog, name="idle-watchdog", daemon=True).start()
    threading.Thread(target=_preload, name="preload", daemon=True).start()
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    _shutdown.wait()
    log("arrêt du serveur")
    httpd.shutdown()
    httpd.server_close()  # libère le socket (relance immédiate possible sur le même port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
