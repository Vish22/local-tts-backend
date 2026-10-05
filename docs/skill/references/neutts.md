# NeuTTS (Neuphonic) sur CPU — notes moteur

## Installation

- `neutts[all]==1.4.1` dans un venv dédié (`~/.hermes/neutts/venv`), installé avec
  `uv --torch-backend=cpu` → roues `torch` / `torchao` / `torchtune` en `+cpu`.
- Modèles HuggingFace **gated** : `neuphonic/neucodec`, `neuphonic/neutts-nano-french-q4-gguf`.
  Accepter les CGU sur la page du modèle, puis poser un jeton **lecture seule** en 600 dans
  `~/.hermes/neutts.env`, sourcé par le wrapper. Sans jeton : 401 au chargement.
- Multilingue : le FR figure dans `BACKBONE_LANGUAGE_MAP` ; les variantes `neutts-nano-french-*`
  sont les plus adaptées (nano = GGUF q4/q8, air = plus gros).

## Incompatibilités connues

- `torchtune 0.6.0` ↔ `torchao ≥ 0.17` : `from torchao.dtypes.nf4tensor import NF4Tensor` échoue
  à l'import. Correctif : shim `torchao/dtypes/nf4tensor.py` (~15 lignes) ré-exportant
  `NF4Tensor`, `linear_nf4`, `to_nf4`.
- `espeak-ng` requis (graphèmes → phonèmes) ; s'il est absent du PATH, une installation locale
  sous `~/.hermes/opt/` suffit si le wrapper l'expose dans le PATH.

## Voix et réglages (genre, accent, vitesse, ton) — ce qui existe vraiment

- **Un seul levier de genre/accent/timbre : le clip de référence.** Clonage par référence :
  `refs/<voix>.wav` + `refs/<voix>.txt` (transcription **exacte** du wav, accents compris).
  Timbre, genre, accent et âge perçu viennent à 100 % de ce couple : il n'existe pas de paramètre
  « plus grave », « féminin » ou « accent anglais ». Recette du clip : mono, 5-15 s, parole nette
  sans musique ni bruit, volume constant ; un `.txt` approximatif fait dériver le clone.
  Déposer le couple suffit : `POST /say {"voice": "<nom>"}` et `client.py --voice <nom>`
  acceptent déjà un nom de voix (défaut codé `juliette` dans `client.py`).
- **Vitesse : aucun paramètre dans le moteur.** `infer()` n'expose ni vitesse ni pitch ;
  `espeak-ng` ne sert qu'à produire la chaîne de phonèmes (stress inclus), ses réglages
  `speed`/`pitch` ne se propagent **pas** à la synthèse. Seul recours : post-traitement ffmpeg
  `atempo` (qualité acceptable ~0,85-1,15×, enchaîner deux filtres au-delà). Ne pas confondre avec
  `tts.keep_warm_seconds` (durée de maintien à chaud, aucun rapport avec la voix).
- **Ton / émotion : réservé aux backbones BPE.** `infer(..., emotion=...)` existe mais
  `_check_emotion` lève `ValueError: Emotion is only supported by BPE models` ; les
  `neutts-nano-french-*` sont en mode **phonèmes** (fr-fr). Leviers indirects seulement :
  ponctuation, casse, et `temperature`/`top_k` de `infer()` qui règlent la variabilité de la
  prosodie — pas un ton choisi.
- **Accent / langue : suit le backbone.** Le phonémiseur est choisi par `BACKBONE_LANGUAGE_MAP`
  (`neutts-nano-french-*` = fr-fr) : un texte anglais lu par le modèle FR sort avec un accent
  français. Autre langue/accent = autre backbone via `NEUTTS_MODEL`, variable **lue au démarrage
  du serveur** ⇒ libérer le serveur (`neutts-release.sh`) avant de changer de modèle.
- **Vérifier une capacité de backbone avant de l'annoncer** : `scripts/gguf-meta.py <modele.gguf>`
  lit l'en-tête GGUF sans charger le modèle. Absence de `neuphonic.input_format` **et** de
  `tokenizer.chat_template` ⇒ repli phonèmes, donc pas d'émotion.
- `say.py` = helper de synthèse (texte → wav mono 24 kHz) ; historiquement appelé directement par
  le wrapper `command`, désormais utilisé comme **chemin de repli** par `client.py`.

## Mesures (CPU, ce déploiement)

- Synthèse directe : rc=0, 24 kHz, ~7,5 s d'audio, **~2,7 Go de pic RSS**, ~10× le temps réel.
- Via l'outil Hermes sur un conteneur de 4 GiB : SIGKILL (`exit 137`), somme baseline + pic
  (la seule cause : la limite mémoire). **Sur 8 GiB : OK** (10,2 s d'audio, 61 Ko, bulle vocale
  Telegram). Le CT qui porte Hermes a été passé à 8 Go pour ça.
- Rendu : écrire du WAV directement (`output_format: wav`) + `voice_compatible: true`.
- Cold start : un command provider relance **son** process à chaque appel ⇒ rechargement des
  modèles (~2,7 Go, 60-90 s) avant le premier mot. `max_text_length` (défaut 5000) découpe
  au-delà en plusieurs invocations = plusieurs rechargements.

## warm_command / release_command (leases) — vérifié dans le code

- Mécanisme : `tools/tts_tool_lifecycle.py`. Un « lease » = jeton nommé enregistré en mémoire
  par une surface qui active la sortie vocale (`cli:voice-tts`, desktop, TUI). Acquérir la
  première fois appelle `warm_tts_provider()` ; relâcher le dernier démarre un compte à rebours
  `tts.keep_warm_seconds` (**défaut 60 s**) puis décharge ; une acquisition pendant la fenêtre
  annule la décharge. Les leases sont comptés (set) : une surface ne peut pas décharger le
  moteur qu'une autre utilise encore.
- Provider `type: command` : `warm_command` / `release_command` sont de simples gabarits shell
  (`{voice}`/`{model}`/`{speed}`, mêmes règles env/timeout que `command`), lancés sur un thread
  daemon. Hermes ne possède **aucun** cache de modèle pour un command provider ⇒ le warm ne sert
  à rien tant que la commande ne réveille pas un **serveur résident** ; le wrapper `command`
  doit alors utiliser ce serveur au lieu de relancer `say.py`.
- Surfaces câblées : desktop (`POST /api/audio/tts-lease` `{lease,active}`), CLI
  (`hermes_cli/cli_voice_mixin.py`), TUI (`tui_gateway/methods_voice.py`).
- **Piège Telegram** : `gateway/` ne référence ni lease ni warm (0 occurrence) — `/voice on|tts`
  en chat ne fait que basculer l'auto-TTS par conversation, sans lease ⇒ `warm_command` n'est
  jamais déclenché depuis Telegram. Le compromis warm/release intégré n'agit donc que sur
  desktop/CLI/TUI.
- Alternative intégrée au code Hermes (leases `warm_command`/`release_command`) : elle n'agit que
  sur desktop/CLI/TUI. Pour un usage Telegram, le serveur résident à la demande ci-dessous donne
  le même effet sans patcher le gateway.

## Architecture retenue : serveur résident à la demande (implémentée 2026-10-03)

- `~/.hermes/neutts/serve.py` : HTTP sur **127.0.0.1:8130** (jamais 0.0.0.0), moteur chargé une
  seule fois. `GET /health`, `POST /say {text,out,voice}`, `POST /release`.
  Watchdog d'inactivité `NEUTTS_IDLE_TIMEOUT` (**défaut 300 s**) ; une requête en cours bloque
  l'arrêt ; verrou global sur l'inférence (moteur non réentrant).
- `~/.hermes/neutts/client.py` : réveille le serveur s'il est absent, attend la readiness
  (`NEUTTS_START_TIMEOUT`, défaut 240 s), POST `/say` ; **repli automatique** sur `say.py`
  (cold start) si le serveur ne vient pas ou sert un autre modèle ⇒ aucune régression possible.
  Options `--status`, `--release`.
- `~/.hermes/bin/neutts-say.sh` = gabarit `command` du provider → `client.py` ;
  `~/.hermes/bin/neutts-release.sh` = libération immédiate (`status` pour interroger).
- Mesures : 1er appel à froid **39 s** (dont **22 s** de chargement du moteur) ; appel chaud
  **6,8 s** pour 87 chars (5,6 s d'audio, WAV 24 kHz mono) ; ~1,2× le temps réel en wall.
- Mémoire résidente : **RSS ~4,5 Go**, dont ~2,5 Go de modèles mmap (récupérables sous pression)
  et ~2 Go anonymes. C'est le vrai coût du compromis : sur un LXC de 8 Go le swap peut mordre,
  donc régler `NEUTTS_IDLE_TIMEOUT` plutôt que d'ajouter de la RAM.
- Pièges : `soundfile` refuse d'écrire sur un `.wav.part` (extension inconnue) →
  `sf.write(..., format="WAV")` ; ne pas journaliser à la fois sur stderr et sur le fichier de
  log quand le wrapper redirige les deux au même endroit (lignes doublées).
