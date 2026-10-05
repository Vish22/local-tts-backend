#!/home/hermes/.local/share/caldav/venv/bin/python
"""CalDAV -> vocal en un seul appel : lit l'agenda et rend un audio court.

Pourquoi : la chaîne « LLM -> text_to_speech » coûte 4 à 5 tours de modèle ; ici
tout se joue en un appel outil (1 lecture CalDAV + 1 POST /say au serveur TTS).

Règles de restitution (mesurées sur le moteur NeuTTS français) :
  * au-delà de ~250 caractères par requête, la fin du texte est perdue
    (vérifié par ASR : 252/357 car restitués) -> découpage + concaténation ;
  * les heures sont écrites « 19 h 30 » et non « 19h30 » pour l'ASR/prononciation.

Usage :
    agenda-vocal.py [--days 7] [--from YYYY-MM-DD] [--cal all|VishBot]
                    [--out FICHIER.ogg] [--no-tts] [--max-chars 250]
Sortie : chemin de l'audio sur la dernière ligne (préfixée OUT=), texte parlé
sur les lignes TEXTE=.
"""
import argparse
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CALDAV_CLI = "/home/hermes/.local/bin/caldav"
NEUTTS_DIR = Path("/home/hermes/.hermes/neutts")
NEUTTS_PY = NEUTTS_DIR / "venv/bin/python"
CLIENT = NEUTTS_DIR / "client.py"
FFMPEG = "/home/hermes/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg"
ESPEAK_LIB = "/home/hermes/.hermes/opt/espeak-ng/usr/lib/x86_64-linux-gnu"
AUDIO_DIR = Path("/home/hermes/.hermes/cache/audio")
DEFAULT_MODEL = "neuphonic/neutts-nano-french-q4-gguf"
MAX_CHARS = 250

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def load_caldav():
    loader = importlib.machinery.SourceFileLoader("caldav_cli", CALDAV_CLI)
    spec = importlib.util.spec_from_loader("caldav_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def jour(d, auj):
    delta = (d - auj).days
    if delta == 0:
        return "aujourd'hui"
    if delta == 1:
        return "demain"
    return "%s %d" % (JOURS[d.weekday()], d.day)


def heure(t):
    # G2P espeak : « h » isolé est lu « ache » — toujours « heures » en toutes lettres.
    return "%d heures" % t.hour if t.minute == 0 else "%d heures %02d" % (t.hour, t.minute)


def phrase(o, auj):
    """Une phrase courte par occurrence."""
    titre = (o.get("summary") or "(sans titre)").strip().rstrip(".")
    s, e = o["start"], o["end"]
    if o.get("allday"):
        fin = e - timedelta(days=1)  # DTEND exclusif sur les journées entières
        if fin.date() == s.date():
            return "%s : %s." % (jour(s.date(), auj), titre)
        return "du %s au %s : %s." % (jour(s.date(), auj), jour(fin.date(), auj), titre)
    if e.date() == s.date():
        return "%s à %s, %s." % (jour(s.date(), auj), heure(s), titre)
    return "du %s à %s au %s à %s, %s." % (jour(s.date(), auj), heure(s),
                                           jour(e.date(), auj), heure(e), titre)


def decouper(phrases, max_chars):
    """Regroupe les phrases en blocs <= max_chars."""
    blocs, cur = [], ""
    for p in phrases:
        if cur and len(cur) + 1 + len(p) > max_chars:
            blocs.append(cur)
            cur = p
        else:
            cur = p if not cur else cur + " " + p
    if cur:
        blocs.append(cur)
    return blocs


def tts_env():
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ESPEAK_LIB + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env["ESPEAK_DATA_PATH"] = ESPEAK_LIB + "/espeak-ng-data"
    env["PATH"] = "/home/hermes/.hermes/opt/espeak-ng/usr/bin:" + env.get("PATH", "")
    env["OMP_NUM_THREADS"] = env.get("OMP_NUM_THREADS", "4")
    env["TOKENIZERS_PARALLELISM"] = "false"
    # NEUTTS_URL / NEUTTS_TOKEN ciblent le moteur TTS distant (portgpu 10.1.33.71) :
    # sans eux, client.py retombe sur 127.0.0.1:8130 et démarre (ou interroge) un
    # moteur local, d'où un HTTP 401. HF_TOKEN : dépôt gated, utile au seul
    # démarrage à froid. Rien n'est jamais affiché.
    envf = Path("/home/hermes/.hermes/neutts.env")
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in ("HF_TOKEN", "NEUTTS_URL", "NEUTTS_TOKEN", "NEUTTS_TOKEN_FILE") and k not in env:
                env[k] = v.strip().strip('"').strip("'")
    return env


def synthetiser(blocs, out, model, voix):
    """Synthétise chaque bloc (serveur TTS) et concatène en un seul .ogg."""
    tmp = AUDIO_DIR / ("tmp_" + out.stem)
    tmp.mkdir(parents=True, exist_ok=True)
    parts, env = [], tts_env()
    for i, bloc in enumerate(blocs):
        tf = tmp / ("bloc%d.txt" % i)
        tf.write_text(bloc, encoding="utf-8")
        wav = tmp / ("bloc%d.wav" % i)
        t0 = time.time()
        r = subprocess.run([str(NEUTTS_PY), str(CLIENT), "--text-file", str(tf),
                            "--out", str(wav), "--model", model, "--voice", voix],
                           env=env, capture_output=True, text=True)
        if r.returncode != 0 or not wav.exists():
            sys.stderr.write(r.stderr[-500:] + "\n")
            raise SystemExit("échec synthèse bloc %d" % i)
        print("BLOC %d %d car %.1f s" % (i, len(bloc), time.time() - t0))
        parts.append(wav)
    if len(parts) == 1:
        cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(parts[0]),
               "-c:a", "libopus", "-b:a", "48k", "-ac", "1", str(out)]
    else:
        lst = tmp / "liste.txt"
        lst.write_text("".join("file '%s'\n" % p for p in parts), encoding="utf-8")
        cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-c:a", "libopus", "-b:a", "48k", "-ac", "1", str(out)]
    subprocess.run(cmd, check=True)
    for p in parts:
        p.unlink(missing_ok=True)
    (tmp / "liste.txt").unlink(missing_ok=True)
    for tf in tmp.glob("bloc*.txt"):
        tf.unlink(missing_ok=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="horizon en jours (défaut 7)")
    ap.add_argument("--from", dest="d0", help="début YYYY-MM-DD (défaut aujourd'hui)")
    ap.add_argument("--cal", default="all", help="agenda (défaut all)")
    ap.add_argument("--out", help="fichier audio de sortie (.ogg)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--voice", default="juliette")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    ap.add_argument("--no-tts", action="store_true", help="n'écrit que le texte parlé")
    a = ap.parse_args()

    t_start = time.time()
    cd = load_caldav()
    auj = datetime.now(cd.TZ).date()
    d0 = datetime.strptime(a.d0, "%Y-%m-%d").date() if a.d0 else auj
    start = datetime.combine(d0, datetime.min.time(), cd.TZ)
    end = datetime.combine(d0 + timedelta(days=a.days), datetime.min.time(), cd.TZ)

    noms = sorted(cd.calendars()) if a.cal == "all" else [a.cal]
    t_cal = time.time()
    rows = cd.gather(noms, start, end)
    print("CALDAV %d événement(s) en %.2f s" % (len(rows), time.time() - t_cal))

    if not rows:
        texte = "Aucun événement prévu jusqu'au %s." % (d0 + timedelta(days=a.days)).strftime("%d/%m")
        phrases = [texte]
    else:
        entete = "Tes prochains rendez-vous." if len(rows) > 1 else "Ton prochain rendez-vous."
        # Ponctuation conservée : on garde la liste de phrases, on ne re-découpe pas le texte.
        phrases = [entete] + [p[0].upper() + p[1:] for p in (phrase(o, auj) for o in rows)]
        texte = " ".join(phrases)

    blocs = decouper(phrases, a.max_chars)
    for b in blocs:
        print("TEXTE=%s" % b)

    if a.no_tts:
        print("TOTAL %.1f s (sans TTS)" % (time.time() - t_start))
        return 0

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(a.out) if a.out else AUDIO_DIR / ("agenda_%s.ogg" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    synthetiser(blocs, out, a.model, a.voice)
    print("OUT=%s" % out)
    print("TOTAL %.1f s (%d bloc(s), %d car)" % (time.time() - t_start, len(blocs), len(texte)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
