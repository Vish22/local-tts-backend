---
name: local-tts-backend
description: "Use when wiring or sizing a local TTS backend (Hermes)."
version: 1.0.0
author: Hermes Agent (curator)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [tts, voice, hermes, sizing, oom, cpu]
    related_skills: [hermes-local-inference, agent-credentials, proxmox-pve-operations]
---

# TTS auto-hébergé comme provider Hermes (vocal à la demande)

## When to Use

- L'utilisateur demande une réponse **en vocal**, ou de câbler un moteur TTS local dans Hermes.
- Un provider TTS existe déjà mais la synthèse lancée par l'agent échoue ou tue le process.
- Il faut **dimensionner** (RAM/vCPU) la machine qui porte le moteur, ou trancher CPU vs GPU.
- Hors de ces cas : ne rien activer, ne joindre aucun audio.

Classe de tâche : rendre Hermes capable de répondre en **audio** via un moteur TTS local, et
**dimensionner** la machine qui le porte. Voisin : inférence locale LLM (`hermes-local-inference`).

## Règle de production : le vocal est opt-in

Le vocal ne se produit que sur **demande explicite** de l'utilisateur, réponse par réponse. Ne
pas activer de TTS global, ne pas joindre d'audio à une réponse ordinaire. Le provider reste
câblé en permanence : c'est le déclenchement qui est rare.

Envoi : quand l'audio est produit, le livrer en **bulle vocale** — ligne `[[audio_as_voice]]`
seule avant le `MEDIA:<chemin>` (le champ `media_tag` de `text_to_speech` la contient déjà).
Sans elle, le fichier part en pièce jointe audio ordinaire ; le format (`.ogg`/opus) est
nécessaire mais pas suffisant pour obtenir la bulle.

## Câblage : command provider

1. Wrapper exécutable `~/.hermes/bin/<moteur>-say.sh <input_path> <output_path>` ; c'est lui qui
   source `~/.hermes/<moteur>.env` (jeton HF des modèles gated → cf. skill `agent-credentials`).
2. `~/.hermes/config.yaml` : `tts.provider: <nom>` + bloc
   `tts.providers.<nom> = {type: command, command: "<wrapper> {input_path} {output_path}", output_format: wav, timeout: 900, voice_compatible: true}`.
3. Hermes passe au wrapper un **fichier d'entrée** (texte) et un **chemin de sortie** : ne pas
   supposer un stdin `-`. Écrire le format final directement (`output_format: wav`) pour éviter
   un ré-encodage ffmpeg (ogg/opus).
   - Le gabarit ne transmet **que les placeholders qu'on y écrit** : `{voice}`, `{model}`,
     `{speed}` ne sont résolus que s'ils figurent dans la chaîne. Tout réglage du moteur absent du
     gabarit est **inatteignable** depuis Hermes (la voix retombe sur le défaut codé du client) :
     l'exposer par une variable d'env lue par le wrapper plutôt que de compter sur un `{voice}`
     implicite.
4. `hermes config set tts.providers.<nom>` par chemin pointé échoue (`Config key not set`) :
   patcher le YAML, ou `hermes config set --force tts.providers '<JSON>'`.
5. Isoler le moteur dans **son propre venv** (`~/.hermes/<moteur>/venv`) : ses contraintes
   torch/transformers sont plus strictes que celles de Hermes, un env partagé casse l'un ou
   l'autre au premier upgrade.
6. Preuve : wrapper lancé à la main (rc=0, fichier audio non vide, durée cohérente), **puis**
   un appel réel via l'outil Hermes — la synthèse directe qui marche ne prouve pas le provider.

## Texte passé au moteur : tout en toutes lettres (le G2P lit les lettres)

Le G2P est espeak-ng (`fr-fr`) : une **lettre isolée est lue comme son nom**, pas comme une
unité. Mesuré avec `scripts/g2p-check.py` sur le moteur en place :

- `19 h` → `diznˈœf ˈaʃ` (le « h » devient *ache*) ; `19 heures` → `diznˈœf ˈœʁ`. Idem `14h30`
  (`katˈɔʁz ˈaʃ tʁˈɑ̃t`) et `2 h` (`ˈaʃ`) alors que `2 heures` donne bien `døz ˈœʁ` — le pluriel
  ne s'entend que si le mot est écrit entier.
- Même cause pour les abréviations et symboles : `RDV` est épelé (`ɛʁdˌevˈe`), `1,5` devient
  « un virgule cinq » (`viʁɡyl`), pas « un et demi ».

Règle : **écrire le texte vocal comme il doit être prononcé**, jamais en forme abrégée.
`19 h 30` → `19 heures 30` ; `RDV` → `rendez-vous` ; `env.` → `environ` ; `1,5 h` →
`une heure et demie` ; `du 22 au 26/10` → `du 22 au 26 octobre`. Après synthèse d'un contenu
chiffré ou abrégé, contrôler le rendu : `scripts/g2p-check.py "<phrase>"` (aucun modèle à
charger, quelques secondes) — si le phonème contient `ˈaʃ`, la lettre est prononcée.

## Longueur du texte : plafond de fidélité (~250 caractères)

Le moteur ne signale pas qu'il abandonne : au-delà d'environ 300 caractères il **tronque puis
répète**, et le fichier produit garde une durée plausible. Mesuré sur un même texte, mêmes
réglages, relecture ASR systématique :

- 201 car → 14,5 s, rendu **fidèle** (transcription = texte source).
- 391 car → 26,2 s : l'ASR ne retrouve que les 2 premières phrases, puis des répétitions.
- 811 car → 20,3 s : **seules les 2 premières phrases sont réellement prononcées**.
- 1622 car → 9,6 s d'audio : presque tout est perdu.

Règles :

- **Découper par phrase à ~200-250 caractères max**, puis concaténer les WAV avant l'envoi —
  jamais un seul `POST /say` sur un texte long : un provider `type: command` n'a aucun plafond
  de longueur, rien ne protège la synthèse. Découpage **câblé** : `~/.hermes/bin/agenda-vocal.py`
  (chaîne agenda→vocal, cf. section dédiée).
- **Conversion durée** : ~14 car/s (≈145 mots/min, débit naturel) ⇒ 200 car ≈ 15 s, 850 car ≈
  1 min. Coût machine ≈ 1,2 × la durée audio à chaud (RTF), hors chargement du moteur.
- **Vérifier avant d'affirmer** : `ffprobe` sur le **fichier livré** (durée réelle) puis relecture
  ASR avec `scripts/tts-verif.py <audio> [texte_source]` (compare transcription et source →
  détecte troncature et répétition). Un vocal « de 20 s » peut ne contenir que le début du texte.
- Le champ `X s` de `serve.log` est le **temps d'inférence**, pas la durée audio (écart réel :
  811 car → 27,4 s d'inférence pour 20,3 s d'audio). Ne jamais répondre à « combien de temps dure
  un vocal » avec cette valeur : prendre la durée du fichier.

## Dimensionner : mesurer le pic, pas la taille des modèles

- `/usr/bin/time -v <wrapper> in.txt out.wav` → `Maximum resident set size`. C'est cette valeur
  qui décide ; la taille du cache HF (`du -sh`) trompe (mmap, pages partagées).
- Repère mesuré (NeuTTS nano FR, CPU) : **~2,7 Go de pic** pour ~7,5 s d'audio, ~10× le temps
  réel sur 2-4 vCPU. **Une fois servi par un serveur résident, compter ~4,5 Go de RSS** (dont un
  tiers de modèles mmap récupérables) : c'est le coût du choix « chaud ». Détails moteur :
  `references/neutts.md` ; mesures et runbook : `references/exploitation.md`.
- Dimensionner `baseline Hermes (~1,1-1,4 Go) + pic + marge pour le page cache des poids`. Une
  limite mémoire sous le pic ne se contourne pas : c'est la limite qu'on monte.

## Diagnostic `exit 137` (SIGKILL / OOM)

- Dans un conteneur : `free -m`, `head -3 /proc/meminfo` (`MemTotal` virtualisé par lxcfs =
  limite du CT), `/sys/fs/cgroup/memory.max` (peut afficher `max` **dans** le CT alors que la
  limite vit sur le cgroup ancêtre `lxc/<vmid>`) ; la valeur `memory` de la config PVE est la
  référence.
- Signature typique : la synthèse **réussit seule** et **échoue via l'outil Hermes** — agent,
  gateway et modèle occupent déjà ~1,4 Go, le pic ne rentre plus. Le coupable est la somme.
- Le swap est un faux recours pour des poids torch (thrash) : ne pas le proposer.

## GPU : ne rien promettre sans preuve

- **Pas nécessaire** : un moteur quantifié tourne sur CPU ; le GPU ne se justifie que pour le
  débit.
- Des nœuds `/dev/nvidia*` présents ne veulent rien dire : si la roue installée est `+cpu`
  (`pip show torch`), aucun CUDA.
- Avant de miser sur une carte ancienne, vérifier les compute capabilities de la roue : les
  builds CUDA récents ne compilent plus les architectures < sm_75 (Maxwell/Pascal/Volta, ex.
  Quadro P2000 = sm_61). Changer de roue torch pour une vieille carte casse torchtune/torchao.
- Déporter la synthèse sur un hôte GPU (service HTTP, modèle résident, le LXC ne fait qu'un
  appel) est la réponse quand la latence CPU (~10× le temps réel) est le vrai problème : c'est
  un micro-service de plus, à proposer seulement si le vocal devient fréquent.
- **Trancher sur la VRAM libre, pas sur la présence d'un GPU** :
  `nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv`. Une voie CUDA a besoin
  d'environ **3,5 Go** (backbone GGUF q4 0,19 + `w2v-bert` ~1,2 en fp16 + `neucodec` ~1,3 +
  ~0,6-0,8 de contexte CUDA). Sur une carte 8 Go déjà occupée à ~7,7 Go par un LLM
  (`gpu_memory_utilization=0.941`), la bascule est **impossible sans dégrader le LLM** : le
  problème n'est pas le support (le paquet est CUDA-prêt : `use_gpu = backbone_device != "cpu"`
  → `n_gpu_layers=-1`, et le codec fait `.to(device)`), c'est la carte partagée.
- **Avant de vouloir le GPU, vérifier si le CPU est seulement sous-employé.** Le backbone
  llama.cpp est déjà fileté par le paquet (`neutts` : `n_threads = cpu_count//2`,
  `n_threads_batch = cpu_count`) : le levier est ailleurs, dans `OMP_NUM_THREADS` (torch :
  w2v-bert + neucodec), souvent resté à 4. Mesuré sur i7-13850HX (20 c / 28 t) : 4 → 12
  divise le **premier** `/say` par deux (13,5 → 7,8 s : c'est l'encodage de la référence qui
  profite des threads) mais **ne change pas** le RTF de régime (~0,6 une fois la référence en
  cache). Passage par `OMP_NUM_THREADS` du compose : **sans rebuild** (modifier le compose de
  l'hôte, puis `docker compose up -d` — le rebuild n'est PAS nécessaire, la variable est lue à
  l'exécution).

## Pièges

- Sans `espeak-ng` (graphèmes → phonèmes), le moteur échoue tard, après chargement des modèles.
  Le vérifier avant d'annoncer un provider fonctionnel.
- Un pic qui passe en test manuel n'est pas un pic qui passe en production : la gateway et
  l'agent tournent en parallèle du processus de synthèse.
- Chaque synthèse repaie le chargement des modèles : au-delà de quelques réponses vocales par
  jour, le cold start est le vrai coût (pas le calcul) → proposer un service résident plutôt
  qu'un GPU. **Implémenté ici** : voir la section suivante.

## Service résident à la demande (montage retenu)

- Pourquoi : les leases `warm_command`/`release_command` de Hermes ne sont pris par aucune surface
  Telegram (0 occurrence dans `gateway/`), donc inopérants sur ce canal ; et un provider
  `type: command` n'a pas de cache de modèle côté Hermes. Le service résident donne le même effet
  sans toucher au gateway.
- Chaîne : outil `text_to_speech` → `neutts-say.sh` → `client.py` → `serve.py` sur
  **127.0.0.1:8130** (ou l'hôte visé par `NEUTTS_URL`), avec **repli** automatique sur la synthèse
  directe. Le cold start (~55-60 s, poids relus du disque) est le premier poste de latence :
  `NEUTTS_IDLE_TIMEOUT=0` (jamais d'extinction) ou un moteur résident sur un hôte dédié.
- Cycle : 1er vocal d'une session = +20-25 s (chargement) ; suivants = synthèse seule (~7 s) ;
  extinction seule après `NEUTTS_IDLE_TIMEOUT` (300 s d'inactivité).
- Même moteur **déjà chaud** (`loaded:true`, `idle_timeout=0`), le **premier** `/say` paie en plus
  l'encodage de la voix de référence : mesuré 13,5 s pour 2,7 s d'audio, contre RTF ≈ 1,0 sur les
  appels suivants. `_preload()` ne synthétise rien et `NeuTTS` n'expose pas de `warmup()` (seul
  `NeuTTS2E` en a un) — mais ne pas proposer un rebuild pour ce gain : il ne se paie qu'une fois
  par démarrage, ce que `NEUTTS_IDLE_TIMEOUT=0` rend rare. Ce 13,5 s **n'est pas** le RTF de
  régime : référence encodée et mise en cache (`_ref_cache`), le moteur descend à **RTF ≈ 0,6**
  (mesuré 11,6 s pour 21,2 s d'audio). Un RTF ≈ 1,0 ou au-dessus signale un appel qui paie encore
  l'encodage de référence — ni un plafond matériel, ni un manque de threads.
- **Après modification de `serve.py`, libérer le serveur** (`neutts-release.sh`) : le client
  réutilise un serveur déjà en écoute et servirait l'ancien code.
- Fichiers, runbook, mesures, décisions et pièges : `references/exploitation.md` ; détails moteur
  (installation, incompatibilités, leases) : `references/neutts.md`.
- **Réglages de voix** (genre, accent, vitesse, ton) : section « Voix et réglages » de
  `references/neutts.md`. En bref : le **clip de référence** est le seul levier de genre/accent ;
  la vitesse n'existe pas dans le moteur (post-traitement ffmpeg `atempo`) ; l'émotion est réservée
  aux backbones BPE, donc exclue d'un modèle FR en phonèmes. Vérifier une capacité de backbone avec
  `scripts/gguf-meta.py <modele.gguf>` (lecture d'en-tête GGUF, sans charger le modèle).
  Répondre à ce genre de question **par levier**, avec ce qui est prouvé, ce qui est absent et ce
  qu'il faut câbler — pas une liste d'options théoriques.

## Chaîne agenda → vocal en un seul appel (rapidité)

`~/.hermes/bin/agenda-vocal.py` fait tout en **un appel outil** : lecture CalDAV (import direct du
CLI `~/.local/bin/caldav`), mise en phrases, découpage ≤250 car, synthèse bloc par bloc, puis
concaténation ffmpeg en un `.ogg` unique. Motif : la chaîne « LLM → `text_to_speech` » coûte 4-5
tours de modèle et ~16 k car de skill CalDAV à chaque demande de calendrier.

- Usage : `agenda-vocal.py [--days 7] [--from AAAA-MM-JJ] [--cal all|VishBot] [--out f.ogg]
  [--no-tts]` ; sortie `TEXTE=` par bloc + `OUT=` (chemin audio) sur la dernière ligne.
- Mesuré (4 événements → 2 blocs) : **73 s moteur froid, 23-30 s moteur chaud** ; la lecture CalDAV
  coûte 0,9 s — donc **tout le temps de réponse est du TTS**, pas de l'agenda.
- Mesuré avec le moteur **résident sur un hôte dédié** (`OMP_NUM_THREADS=12`, mêmes 4 événements →
  2 blocs, 273 car → 21,2 s d'audio) : **12,8 s au total**, dont 0,86 s de CalDAV ⇒ ~11,6 s de
  synthèse, **RTF ≈ 0,6** référence déjà encodée. Preuve à exiger que c'est bien l'hôte distant qui
  a servi : `ss -lnpt | grep -w 8130` **vide** côté client **et** `/health` distant qui repart
  (`idle_s` ≈ 0, `uptime_s` croissant) — `scripts/verif-bascule-tts.sh` enchaîne les trois contrôles.
- `client.py` lit `NEUTTS_URL` (défaut `http://127.0.0.1:8130`) et `NEUTTS_TOKEN` dans **son propre**
  environnement. Poser la variable dans `neutts.env` ne suffit donc **pas** si le script appelant
  reconstruit l'env de l'enfant : `agenda-vocal.py` n'y recopiait que `HF_TOKEN` → `client.py`
  retombait sur `127.0.0.1:8130`, **démarrait un moteur local** et récoltait un **HTTP 401** (jeton du
  moteur local ≠ jeton du distant). Un 401 se diagnostique donc côté **client**, pas côté serveur :
  vérifier la liste des variables réellement propagées à l'enfant, puis `ss -lnpt | grep -w 8130` —
  un écouteur local prouve que la bascule n'a pas eu lieu ; le tuer (il tient ~2 Go de RSS) avant de
  remesurer, sinon c'est lui qui répond.
- Lancer `agenda-vocal.py` avec l'interpréteur de son venv (`~/.local/share/caldav/venv/bin/python`,
  ou via son shebang) : il importe `caldav`/`dateutil`, absents du `python3` ambiant.
- Heures : toujours « 19 heures / 19 heures 30 », jamais « 19h » ni « 19 h » (G2P, cf. section
dédiée) ; ne pas repasser par un re-découpage du texte joint (perte de la ponctuation interne).

## Installer le moteur résident sur un hôte distant (« portgpu »)

`~/.hermes/neutts/install-portgpu.sh` : installateur **autonome**, exécuté par l'utilisateur en root
sur l'hôte cible (pas de déploiement SSH par l'agent). Il embarque `serve.py`, `client.py` et la voix
de référence (`refs/juliette.{txt,wav}`, WAV en gzip+base64) avec leurs SHA256 → les fichiers posés
sont **exactement ceux validés localement**, pas une version réécrite.

- Contrat de variables : `PREFIX` (`/opt/neutts`), `STATE` (`/var/lib/neutts`), `BIND` (`0.0.0.0`),
  `PORT` (`8130`), `IDLE` (`0` = jamais d'extinction — c'est le but : supprimer le cold start),
  `SVC_USER` (`neutts`, utilisateur système sans shell), `HF_TOKEN` ; dossier d'unité `./.neutts` ;
  `--purge` désinstalle ; `SELFTEST=1` réécrit le payload dans `$PREFIX` et vérifie les SHA256 sans
  rien installer sur le système.
- `BIND=0.0.0.0` n'est tolérable pour l'installateur **natif** que parce qu'ufw filtre bien ce processus
  (restreindre l'accès à l'hôte appelant) ; la règle « jamais `0.0.0.0` » vise **Docker**, dont les
  publications de ports insèrent leurs règles avant ufw. Ne pas transposer un réglage de l'un à l'autre.
- **Aucun secret dans le script** : le jeton HF est lu depuis `NEUTTS_ENV`/`~/.hermes/neutts.env`,
  sinon saisi en entrée masquée (`read -rs`) ; ne jamais l'écrire dans un fichier versionné ni
  l'afficher.
- Côté Hermes, il suffit de viser l'hôte : `NEUTTS_URL=http://<hôte>:8130` (et `NEUTTS_TOKEN` si
  jeton) — `client.py` rapatrie le WAV via `GET /audio/<nom>`, aucun autre changement.
- `serve.py` v2 dépend de noms d'env précis : `NEUTTS_HOST/PORT/OUT_DIR/MODEL/CODEC/REFS/TOKEN/LOG/
  IDLE_TIMEOUT` ; réponse de `POST /say` = `{ok,seconds,bytes,out,path,name,chars}` ; `GET /health`
  passe sans jeton, `/say` et `/audio` exigent `X-Neutts-Token` si `NEUTTS_TOKEN` est défini.
## Variante Docker Compose (même moteur, conteneurisé)

Paquet de référence : `~/.hermes/neutts/docker/` (Dockerfile, `requirements.txt`, `docker-compose.yml`,
`.env.example`, `ctx/serve.py`, `ctx/client.py`, `ctx/refs/`, `README.md`, `SHA256SUMS`), livré aussi en
tarball `neutts-docker.tar.gz`. Le contexte de build embarque les fichiers **testés** (`ctx/`) — jamais
une réécriture — et `requirements.txt` est le **gel exact du venv validé** (`pip freeze` + `importlib.metadata`),
jamais des plages de versions : un conteneur doit reproduire l'env prouvé, pas le réinventer.

- `llama-cpp-python` n'a **pas de roue sur PyPI** : install depuis l'index abetlen
  (`--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu`) avec
  `--only-binary=llama-cpp-python` → échec net plutôt qu'une compilation source de 20 min.
  torch/torchaudio/torchao/torchtune « +cpu » viennent de `https://download.pytorch.org/whl/cpu`.
- **Un pin `torchtune` en cache un autre** : `torchtune 0.6.0` fait `from torchao.dtypes.nf4tensor
  import NF4Tensor`, module **supprimé dans torchao 0.18.0** — et `neucodec` ne borne pas `torchao`,
  donc le gel prend la dernière version et l'import casse **au chargement du moteur**, pas à
  l'installation (`pip check` vert, `/health` répond `loaded:false`). Fix : `torchao==0.17.0+cpu`
  (roue pure-python `py3-none-any`, hash à recalculer pour `--require-hashes`). Avant de bouger un pin
  `torch/torchaudio/torchao`, vérifier dans la roue que le module importé existe encore
  (`python -c "import zipfile,glob;print([n for n in zipfile.ZipFile(glob.glob('/tmp/torchao-*.whl')[0]).namelist() if 'nf4tensor' in n])"`).
- Un run de build échoué ne coûte pas la retéléchargement : monter un cache BuildKit
  (`RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked` + `PIP_NO_CACHE_DIR=0` le temps de
  la commande, l'ENV de l'image restant à `1` pour ne pas gonfler l'image) — sans lui, chaque
  correction de pin repaye ~1,3 Go de roues.
- Une base `nvidia/cuda:*` livre **Python 3.12** : toute sélection de paquets figée en `cp313` y échoue.
  Ne pas proposer de surcouche GPU sans base Python 3.13 + runtime CUDA ; un moteur quantifié CPU
  (~4-5 s par phrase) rend le GPU inutile ici.
- Les **ports publiés par Docker contournent ufw** (chaîne `DOCKER` évaluée avant) : publier
  explicitement `${NEUTTS_BIND_HOST:-<IP LAN>}:8130:8130`, jamais `0.0.0.0`.
- `NEUTTS_IDLE_TIMEOUT=0` dans le conteneur : c'est tout l'intérêt (cold start supprimé).
- Persistance par **bind relatif `./data:/data`** (cache HF ~3,5 Go, WAV, `serve.log`) : inspectable,
  sauvegardable et purgable avec de simples outils fichiers. Un bind **n'hérite pas** des permissions
  de l'image — contrairement au volume nommé, initialisé depuis le `chown` du Dockerfile — donc créer le
  dossier avec l'uid du conteneur **avant le premier `up`** (`mkdir -p data && sudo chown -R
  10001:10001 data && chmod 750 data`), sinon le démarrage meurt en permission denied sur `/data/hf` ou
  `/data/serve.log`. Éviter un `./data` sur NFS/CIFS (POSIX, `chown`, verrouillage).
- Quand la persistance est portée par un bind explicite, **retirer `VOLUME ["/data"]` du Dockerfile** :
  sinon chaque exécution hors Compose crée un volume anonyme orphelin qui masque le bind. Toute retouche
  du Dockerfile impose un **rebuild de l'image**, pas un simple `up`.
- `HEALTHCHECK` sur `/health` avec `--start-period` large (téléchargement des modèles au 1er démarrage,
  plusieurs minutes) ; `/health` reste la seule route sans jeton. Suivre ce 1er démarrage en **attaché**,
  mais faire les vérifications (`curl /health`, `docker inspect -f '{{.State.Health.Status}}'`) dans un
  **second terminal** : `Ctrl-C` sur l'attach **arrête** le conteneur — ne le faire qu'une fois
  `loaded:true` vu, puis repasser en arrière-plan (`docker compose up -d`, `restart: unless-stopped`).
- Au démarrage, les `W…`/`FutureWarning` de torch (`register_constant()` sur une sous-classe Enum,
  `torch.jit.script` déprécié) viennent des imports `neucodec → torchtune/torchao` : **bruit d'import,
  pas un échec de démarrage**. Juger la disponibilité sur `/health` (`loaded`) et le `HEALTHCHECK`, jamais
  sur l'absence de warning dans les logs.
- **Livraison en tarball + `SHA256SUMS`** : toute retouche d'un fichier du paquet périme le manifeste —
  le régénérer (en l'**excluant lui-même**, sinon `sha256sum -c` échoue sur l'entrée auto-référente),
  reconstruire l'archive, puis prouver par **extraction dans un dossier propre** + `sha256sum -c`
  (toutes les lignes `OK`). Annoncer le sha256 et la taille du tarball : c'est ce qui rend l'étape de
  transfert vérifiable sur l'hôte cible.
- Secrets : uniquement via `.env` (mode 600, listé dans `.dockerignore`) et `env_file`/`environment` du
  Compose — jamais dans l'image ni dans un `ARG` (les `ARG` sont visibles dans l'historique d'image).
- **Preuve du gel = installation réelle, pas audit théorique** : installer `requirements.txt` dans un
  venv vierge de la version visée (`pip install --require-hashes --no-build-isolation
  --only-binary=llama-cpp-python` + index CPU), puis `pip check` et surtout un import de la **chaîne
  réellement parcourue au chargement** (`import neucodec`, qui tire `torchtune → torchao`) — pas
  seulement des modules feuilles : c'est précisément un import tardif que l'installation n'a pas vu
  passer. En mode `--require-hashes`, tout paquet absent du gel ou toute empreinte divergente fait
  échouer pip : un run vert = les empreintes sont validées empiriquement (recouper les sha256 à la
  main est un pis-aller).
- **Dépendances Debian : vérifier ce que `--no-install-recommends` retire vraiment** avec
  `apt-get install -s --no-install-recommends <paquets>` (simulation, sans root) : `espeak-ng` seul ne
  tire `espeak-ng-data` que via `Depends` de `libespeak-ng1` — sans les données de langue le G2P échoue
  **après** chargement des modèles. Vérifier la liste `Inst` de la simulation, ne pas supposer.
- **Taille d'image attendue : ≈ 1,4 Go** (venv 1,1 Go, torch seul 373 Mo) et c'est **imposé en amont**
  (`neutts → neucodec → torchtune/torchao/transformers/datasets`) : ne pas chercher à l'alléger (fork de
  `neucodec` ou `--no-deps` = casse garantie) — le dire au lieu de promettre une image légère.
- Dans un `RUN`, un `#` **en fin de ligne** est interprété par le shell (Docker ne commente qu'en début
  de ligne) : poser les commentaires sur leur propre ligne au-dessus de l'instruction.
