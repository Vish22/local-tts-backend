# Moteur TTS Neutts en Docker Compose — 10.1.33.71 « portgpu »

Serveur HTTP résident (`serve.py` v3 durci) + client, avec la voix de référence `juliette`.
Même code et mêmes versions que l'environnement validé localement le 05/10/2026 (11/11 contrôles OK),
puis durci par l'audit sécurité/optimisation du même jour (voir § Sécurité).

## Ce que fait l'image

- Python 3.13 (venv `/opt/venv`), torch 2.14.1+cpu / torchaudio 2.11.0+cpu, `neutts[llama]==1.4.1`.
- `requirements.txt` = **gel exact du venv validé** (109 paquets, aucune plage de versions, aucun build) :
  les versions « +cpu » (torch, torchaudio, torchao, torchtune) viennent de l'index PyTorch CPU, les autres de PyPI.
- `llama-cpp-python==0.3.36` en **roue précompilée** (index abetlen) : pas de cmake/gcc, build ≈ 3–5 min.
- Empreinte : venv ≈ 950 Mo → image ≈ 1,5 Go (CPU).
- Routes : `GET /health` (sans jeton), `POST /say`, `GET /audio/<nom>`, `POST /release`.
- `NEUTTS_IDLE_TIMEOUT=0` : moteur chargé une fois pour toutes → plus de cold start (~55 s supprimées).
- Plafonds de service : 4000 caractères par requête, corps JSON 64 Kio, 16 connexions simultanées,
  inférence sérialisée (le modèle n'est pas réentrant) ; au-delà, réponse `413` / refus immédiat.
- Côté client : le WAV rapatrié est borné (`NEUTTS_MAX_AUDIO`, défaut 64 Mio) et un moteur distant
  qui redémarre est attendu jusqu'à `NEUTTS_REMOTE_WAIT` s (défaut 90) avant tout repli sur `say.py` :
  un reboot du moteur résident ne déclenche donc pas de cold start local.
- Écritures confinées à `/data` (`HOME`, `XDG_CACHE_HOME`, `TORCH_HOME`, sorties WAV, journal) :
  le conteneur tourne en `read_only` avec `/tmp` en tmpfs.
- Modèles HF dans `./data/hf` (≈ 3,5 Go téléchargés au 1er démarrage, ensuite conservés) :
  les données vivent dans `./data` sur l'hôte (bind mount), pas dans un volume Docker opaque —
  inspection, sauvegarde et purge se font avec de simples outils fichiers.

## Déploiement (en root ou avec docker, sur portgpu)

```sh
cd neutts-docker
mkdir -p data && sudo chown -R 10001:10001 data && chmod 750 data
        # AVANT le premier up : ./data est un bind monté par-dessus /data, il n'hérite donc
        # PAS des permissions de l'image. Le conteneur écrit en uid 10001 (neutts) : sans ce
        # chown, le démarrage échoue sur /data/hf ou /data/serve.log (permission denied).
cp .env.example .env && chmod 600 .env      # renseigner NEUTTS_TOKEN (openssl rand -hex 32) et HF_TOKEN
docker compose build                        # ≈ 3-5 min, aucune compilation
# (la base python:3.13-slim est épinglée par digest : c'est voulu, la mise à jour est explicite.
#  Pour la rafraîchir : docker buildx imagetools inspect python:3.13-slim, reporter le digest
#  dans l'ARG BASE_IMAGE du Dockerfile, puis rebuild.)
docker compose up -d
docker compose logs -f                      # 1er démarrage : téléchargement ~3,5 Go + chargement (5-20 min)
curl -s http://10.1.33.71:8130/health       # attendre "loaded":true (l'image passe healthy ensuite)
```

Test de synthèse (le jeton est requis) :

```sh
TOKEN=$(grep -E '^NEUTTS_TOKEN=' .env | cut -d= -f2-)
curl -s -X POST http://10.1.33.71:8130/say -H "X-Neutts-Token: $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"text":"Bonjour Vishaal, ceci est un test du moteur résident.","voice":"juliette"}'
# -> {"ok":true,"seconds":4.6,"bytes":... ,"name":"say_....wav","chars":..} : premier appel ≈ 4-6 s, suivants identiques (moteur chaud)
docker compose exec neutts python client.py --status
```

## Exploitation

```sh
docker compose logs --tail 100 neutts
docker compose restart neutts
docker compose build && docker compose up -d             # rebuild après modification du code
docker compose down                                      # ./data est conservé
docker compose down -v                                  # supprime aussi les modèles (~3,5 Go à re-télécharger)
```

Changer la voix sans rebuild : déposer `mavoix.txt` (transcription exacte) et `mavoix.wav` dans
`ctx/refs/` (`chmod 644`, lisibles par l'utilisateur 10001), puis `NEUTTS_VOICE` et redémarrer.

## Variante GPU : non livrée

Le chemin CPU est retenu : l'inférence y est déjà à ~4-5 s par phrase, soit la latence utile,
et il ne demande ni `nvidia-container-toolkit` ni base d'image CUDA.

Une variante CUDA n'est pas fournie parce qu'elle ne peut pas être validée ici et que ses
prérequis sont contraignants : les paquets épinglés sont des roues `cp313` (Python 3.13) alors
que les images `nvidia/cuda:*` livrent Python 3.12 — il faudrait une base Python 3.13 + runtime
CUDA, puis la roue `llama-cpp-python` `cu124`. Le `Dockerfile` garde les `ARG BASE_IMAGE` et
`ARG LLAMA_INDEX` surchargeables si le besoin se confirme un jour :

```sh
docker build --build-arg BASE_IMAGE=<python3.13+cuda> \
             --build-arg LLAMA_INDEX=https://abetlen.github.io/llama-cpp-python/whl/cu124 -t neutts-serve:cu124 .
```

## Sécurité

Défense en profondeur, du réseau au processus :

- **Exposition réseau** : le port est publié sur `10.1.33.71` uniquement (`NEUTTS_BIND_HOST`) : les ports
  publiés par Docker contournent `ufw` (chaîne `DOCKER` évaluée avant `ufw`), le bind LAN est donc la
  vraie barrière. Ne jamais mettre `0.0.0.0` dans `NEUTTS_BIND_HOST`.
- **Authentification** : `NEUTTS_TOKEN` est obligatoire ; `/say`, `/audio` et `/release` répondent `401`
  sans jeton (`hmac.compare_digest`, comparaison à temps constant). Seul `/health` est ouvert
  (supervision). Si l'écoute n'est pas loopback et qu'aucun jeton n'est fourni, `serve.py` **refuse de
  démarrer** (fail-closed, code 2) : le cas « moteur exposé sans jeton » ne peut pas arriver par oubli.
- **Entrées** : texte limité à `NEUTTS_MAX_CHARS` (413), corps JSON à `NEUTTS_MAX_BODY` (413),
  `NEUTTS_MAX_CONN` connexions simultanées, inférence sérialisée. Le nom de fichier demandé par
  `/audio` est résolu dans `NEUTTS_OUT_DIR` : les noms relatifs seuls sont acceptés pour une requête
  distante, toute remontée (`../`) reste confinée au répertoire de sortie (400 sinon).
- **Chaîne d'approvisionnement** : base `python:3.13-slim` **épinglée par digest**
  (`docker buildx imagetools inspect python:3.13-slim` pour la rafraîchir), gel `requirements.txt`
  installé avec `pip --require-hashes` (tout artefact dont le SHA256 ne correspond pas au gel est
  rejeté, quel que soit l'index qui le sert) et `--only-binary=llama-cpp-python` (pas de compilation).
- **Défense côté client** : le client ne fait jamais confiance au serveur qu'il interroge — le WAV
  rapatrié est plafonné (`NEUTTS_MAX_AUDIO`, 64 Mio par défaut), le schéma d'URL est restreint à
  `http`/`https`, un refus `4xx` remonte tel quel (sans repli) et un `5xx`/réseau ne replie qu'après
  `NEUTTS_REMOTE_WAIT`. Le jeton n'est jamais journalisé, seulement envoyé en en-tête `X-Neutts-Token`.
- **Conteneur** : `read_only`, `/tmp` en tmpfs 256 Mo, `cap_drop: [ALL]`,
  `no-new-privileges: true`, `pids_limit`, utilisateur non privilégié (uid 10001), `init: true`.
  Aucune écriture hors de `/data` (bind `./data` sur l'hôte).
- **Fail-closed** : sans `NEUTTS_TOKEN`, une écoute non-loopback **refuse de démarrer**
  (le conteneur sort en erreur) ; `NEUTTS_ALLOW_NO_TOKEN=1` lève ce refus — ne pas l'employer en LAN.
  De même, `POST /release` n'est accepté que depuis la loopback ; `NEUTTS_ALLOW_RELEASE=1` l'ouvre.
- **Secrets** : aucun jeton dans l'image ni dans le dépôt. `.env` (mode 600, exclu par `.dockerignore`)
  est le porteur des jetons. Le `HF_TOKEN` n'est utilisé qu'au téléchargement des modèles ; il n'est
  jamais écrit dans un fichier de l'image et peut être retiré de `.env` après le premier démarrage.
- **Jeton hors environnement** (optionnel) : `NEUTTS_TOKEN` apparaît dans `docker inspect`. Pour
  l'éviter, stocker le jeton dans un fichier `chmod 600` et l'injecter comme secret :

```yaml
secrets:
  neutts_token:
    file: /etc/neutts.jeton        # 600, root, sur portgpu
services:
  neutts:
    secrets: [neutts_token]
    environment:
      NEUTTS_TOKEN_FILE: /run/secrets/neutts_token   # et retirer la ligne NEUTTS_TOKEN
```

  `serve.py` lit alors le jeton depuis le fichier après avoir réappliqué l'environnement (le fichier
  est monté après le démarrage du conteneur ; un fichier illisible laisse le jeton vide, donc
  fail-closed).

## Intégration Hermes (côté LXC 147)

Dans `~/.hermes/neutts.env` :

```sh
NEUTTS_URL=http://10.1.33.71:8130
NEUTTS_TOKEN=<même jeton que .env sur portgpu>
```

Facultatif : `NEUTTS_MAX_AUDIO` (plafond du WAV rapatrié, défaut 67108864) et `NEUTTS_REMOTE_WAIT`
(secondes d'attente du moteur distant avant repli, défaut 90).

`agenda-vocal.py` / `client.py` basculent alors automatiquement en mode distant :
plus de démarrage de moteur local, plus de cold start.

## Validation du gel (exécutée avant livraison)

`requirements.txt` a été installé réellement dans un venv Python 3.13 vierge, en mode vérifié
(`--require-hashes --no-build-isolation --only-binary=llama-cpp-python`, index CPU PyTorch + abetlen) :

- 110 exigences installées, **aucun rejet d'empreinte** (en mode vérifié, toute divergence sha256
  ou tout paquet sans empreinte fait échouer pip) ;
- `pip check` : aucune dépendance cassée ;
- `import torch, torchaudio, llama_cpp, soundfile, librosa, phonemizer, neucodec` → OK
  (torch 2.14.1+cpu, torchaudio 2.11.0+cpu, llama_cpp_python 0.3.36) ;
  **importer la chaîne réellement parcourue au chargement**, pas seulement les bibliothèques
  feuilles : c'est un import tardif (`neucodec → torchtune → torchao`) qui a fait échouer le premier
  déploiement, alors que l'installation, `pip check` et les imports directs étaient tous verts ;
- venv final 1,1 Go (torch seul : 373 Mo) ⇒ compter ≈ 1,4 Go pour l'image. Ce poids vient de la
  chaîne amont `neutts → neucodec → torchtune/torchao/transformers/datasets`, pas d'un choix
  d'image : ne pas tenter de l'alléger sans forker `neucodec`.

### Contrainte de version à ne pas casser

`torchtune 0.6.0` importe `torchao.dtypes.nf4tensor`, module **supprimé dans torchao 0.18.0** : le pin
est donc `torchao==0.17.0+cpu`. Remonter torchao casse le chargement du moteur (échec du
préchargement, `loaded` reste faux) **sans faire échouer l'installation**. Changer une version :

1. éditer `requirements.txt` (nom + `--hash=sha256:` de la nouvelle roue) ;
2. `docker compose build && docker compose up -d` — le cache pip BuildKit évite de retélécharger
   les ~1,3 Go de dépendances ;
3. contrôler `curl -s http://10.1.33.71:8130/health | jq .loaded` puis `/data/serve.log`.

Dépendances système : `espeak-ng` (binaire appelé par phonemizer) et `libsndfile1` (soundfile).
`espeak-ng-data` est tiré par `libespeak-ng1` (Depends dur, vérifié par `apt-get install -s` sur
Debian 13) : `--no-install-recommends` ne prive donc pas le moteur de ses données de langue.
