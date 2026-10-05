# Moteur TTS français auto-hébergé (NeuTTS nano FR, CPU) + câblage Hermes

Moteur de synthèse vocale **local**, en français, tournant **sur CPU**, exposé en **service HTTP
résident** (plus de *cold start*) et consommé par un client qui sait basculer automatiquement en
mode distant. Le dépôt contient le paquet Docker Compose **exact** validé en production (gel de
dépendances vérifié par empreintes) et l'outillage d'intégration côté agent.

État de la livraison : paquet Docker validé sur 11/11 contrôles, gel `requirements.txt` installé
réellement en venv vierge en mode `--require-hashes`, puis durci par audit (voir `docker/README.md`
§ Sécurité). Mesures : ~4-5 s par phrase, **RTF ≈ 0,6** à chaud (référence de voix encodée et en
cache), venv ≈ 950 Mo → image ≈ 1,5 Go, RSS ≈ 4,6 Go moteur chaud.

## Contenu du dépôt

```
docker/                 paquet Docker Compose — copie exacte du paquet testé (SHA256SUMS valide)
  Dockerfile            python:3.13-slim épinglée par digest, venv /opt/venv, roues précompilées
  docker-compose.yml    publication du port liée à une IP LAN, bind ./data:/data, conteneur read_only
  .env.example          gabarit (jeton de service, jeton HF, IP de bind, réglages)
  requirements.txt      gel exact du venv validé : 110 exigences / 124 roues, toutes empreintes sha256
  ctx/serve.py          serveur HTTP résident (routes /health, /say, /audio/<nom>, /release)
  ctx/client.py         client : bascule distant/local, plafond audio, attente du moteur qui redémarre
  ctx/refs/             voix de référence « juliette » (.txt + .wav) utilisée par le moteur
  SHA256SUMS            manifeste du paquet (vérifiable : `sha256sum -c SHA256SUMS`)
docs/skill/             la fiche d'exploitation d'origine (SKILL.md + références + scripts de contrôle)
hermes/                 intégration côté agent : wrappers, client
install/install-portgpu.sh   installateur natif autonome (systemd, sans Docker) pour un hôte dédié
```

## Démarrage rapide (Docker)

```sh
git clone https://github.com/Vish22/local-tts-backend.git
cd local-tts-backend/docker

mkdir -p data && sudo chown -R 10001:10001 data && chmod 750 data
        # ./data est un bind monté par-dessus /data : il n'hérite PAS des permissions de l'image.
        # Le conteneur écrit en uid 10001 → sans ce chown, le démarrage échoue (permission denied).
cp .env.example .env && chmod 600 .env
        # renseigner NEUTTS_TOKEN (openssl rand -hex 32) et, si les modèles sont gated, HF_TOKEN
        # remplacer NEUTTS_BIND_HOST par l'IP LAN de l'hôte (jamais 0.0.0.0)

docker compose build          # ≈ 3-5 min, aucune compilation
docker compose up -d
docker compose logs -f        # 1er démarrage : ~3,5 Go de modèles + chargement (5-20 min)

curl -s http://<ip-lan>:8130/health      # attendre "loaded":true
```

Test de synthèse :

```sh
TOKEN=$(grep -E '^NEUTTS_TOKEN=' .env | cut -d= -f2-)
curl -s -X POST http://<ip-lan>:8130/say -H "X-Neutts-Token: $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"text":"Bonjour, ceci est un test du moteur résident.","voice":"juliette"}'
```

Détails complets (exploitation, sécurité, changement de voix, variante GPU non livrée) :
`docker/README.md`.

## Variante native (sans Docker)

`install/install-portgpu.sh` pose le même moteur en service systemd sur un hôte dédié, avec
`serve.py`/`client.py` et la voix de référence embarqués (gzip+base64) **et vérifiés par SHA256**.
Exécution en root sur l'hôte cible, contrat de variables via l'environnement (`PREFIX`, `STATE`,
`BIND`, `PORT`, `IDLE`, `SVC_USER`, `HF_TOKEN`), `SELFTEST=1` pour ne vérifier que le payload,
`--purge` pour désinstaller. Aucun secret n'est écrit dans le script.

## Intégration côté agent (Hermes)

`hermes/neutts-say.sh` est le wrapper déclaré comme provider TTS (`type: command`), `hermes/say.py`
le repli en synthèse directe, `hermes/agenda-vocal.py` la chaîne « agenda CalDAV → vocal » en un
seul appel (mise en phrases, découpage, synthèse par blocs, concaténation). Côté client, seules deux
variables sont nécessaires :

```sh
NEUTTS_URL=http://<hôte>:8130
NEUTTS_TOKEN=<même jeton que .env sur l'hôte>
```

## Vérifier l'intégrité du paquet

```sh
cd docker && sha256sum -c SHA256SUMS      # toutes les lignes doivent être « OK »
```

## Points de sécurité retenus

- Port publié **lié à une IP LAN** : les ports publiés par Docker contournent `ufw` (chaîne `DOCKER`
  évaluée avant), le bind est donc la vraie barrière. Jamais `0.0.0.0`.
- `NEUTTS_TOKEN` obligatoire : `/say`, `/audio` et `/release` répondent `401` sans jeton
  (comparaison à temps constant) ; `/health` seul est ouvert pour la supervision. Une écoute
  non-loopback sans jeton **refuse de démarrer** (fail-closed).
- Conteneur `read_only`, `/tmp` en tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, uid non
  privilégié 10001, `pids_limit`, écritures confinées à `/data`.
- Chaîne d'approvisionnement : base épinglée par digest, `pip --require-hashes`, roues binaires
  uniquement. Aucun secret dans l'image ni dans ce dépôt.

## Contraintes connues (à ne pas découvrir en production)

- **Longueur** : le moteur tronque silencieusement au-delà de ~250-300 caractères → découper par
  phrase et concaténer les WAV ; ne jamais envoyer un texte long en un seul `POST /say`.
- **G2P** : espeak-ng lit les lettres isolées comme leur nom (`19 h` → « dix-neuf ache ») → écrire
  le texte vocal en toutes lettres (`19 heures 30`).
- **Pin à ne pas casser** : `torchtune 0.6.0` importe `torchao.dtypes.nf4tensor`, module supprimé
  dans torchao 0.18.0 → `torchao==0.17.0+cpu`. Remonter torchao casse le chargement du moteur
  **sans faire échouer l'installation**.
- **Poids** : ~1,5 Go d'image et ~950 Mo de venv, imposés par la chaîne amont
  `neutts → neucodec → torchtune/torchao` — ne pas chercher à l'alléger, fork de `neucodec` exclu.

## Notes

- Le clip de référence `ctx/refs/juliette.wav` (+ sa transcription) est l'échantillon d'exemple
  fourni avec le moteur, repris tel quel : fourni comme échantillon de démonstration.
- Licence du dépôt : MIT (voir `LICENSE`). Les dépendances tierces conservent leurs licences
  respectives (torch, transformers, neucodec, llama.cpp, espeak-ng…).
