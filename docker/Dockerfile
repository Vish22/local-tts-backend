# syntax=docker/dockerfile:1
#
# Moteur TTS Neutts résident — image maison (serve.py v3.3 durci, HTTP /health /say /audio /release)
#
# Durcissement (audit 05/10/2026) :
#  - aucune écriture hors /data : HOME, XDG_CACHE_HOME et TORCH_HOME pointent sur /data,
#    ce qui permet de lancer le conteneur en read_only (cf. docker-compose.yml) ;
#  - dépendances installées en mode vérifié (--require-hashes) : un paquet modifié sur un
#    index fait échouer le build au lieu d'être installé silencieusement ;
#  - base épinglée par digest (manifest multi-arch) : le tag python:3.13-slim peut bouger.
# Versions identiques à l'environnement validé le 05/10/2026 (11/11 contrôles OK).
#
# CPU uniquement : aucune compilation, roue py3-none x86_64 fournie par l'index abetlen.
# GPU : non livré (cf. README). BASE_IMAGE et LLAMA_INDEX restent surchargeables pour une
#       variante CUDA, mais elle exige une base Python 3.13 disposant du runtime CUDA.
#       (nvidia/cuda:12.4.1-runtime-ubuntu24.04 fournit Python 3.12 -> les versions
#        épinglées cp313 échouent ; ne pas l'utiliser tel quel.)

# Digest de la liste de manifestes python:3.13-slim (2026-10-04). Pour rafraîchir :
#   docker buildx imagetools inspect python:3.13-slim
ARG BASE_IMAGE=python:3.13-slim@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f
FROM ${BASE_IMAGE}

ARG LLAMA_INDEX=https://abetlen.github.io/llama-cpp-python/whl/cpu
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/data \
    HF_HOME=/data/hf \
    XDG_CACHE_HOME=/data/cache \
    TORCH_HOME=/data/torch \
    OMP_NUM_THREADS=4 \
    TOKENIZERS_PARALLELISM=false \
    NEUTTS_HOST=0.0.0.0 \
    NEUTTS_PORT=8130 \
    NEUTTS_IDLE_TIMEOUT=0 \
    NEUTTS_VOICE=juliette \
    NEUTTS_REFS=/opt/neutts/refs \
    NEUTTS_OUT_DIR=/data/out \
    NEUTTS_LOG=/data/serve.log \
    NEUTTS_OUT_KEEP_S=86400 \
    NEUTTS_MAX_CHARS=4000 \
    NEUTTS_MAX_BODY=65536 \
    NEUTTS_MAX_CONN=16

# libgomp1  : runtime OpenMP de la roue llama-cpp-python
# libsndfile1: soundfile (lecture/écriture WAV)
# espeak-ng : phonémisation de secours si le espeak embarqué par neutts n'est pas trouvé
# ca-certificates : TLS vers l'index PyPI et Hugging Face
# (curl volontairement absent : le controle de sante passe par urllib de Python)
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng libespeak-ng1 libsndfile1 libgomp1 ca-certificates \
        python3-venv \
    && mkdir -p /data/out /data/hf /data/cache /data/torch \
    && rm -rf /var/lib/apt/lists/*

# Bootstrap epingle : pas de --require-hashes ici (ces outils s'installent avant le gel).
RUN python3 -m venv "$VIRTUAL_ENV" \
    && pip install --upgrade pip==26.2.1 setuptools==84.0.0 wheel==0.48.0

# Toutes les dépendances épinglées (gel du venv validé) en une passe :
#  - llama-cpp-python 0.3.36 : pas de roue sur PyPI -> roue précompilée de l'index abetlen (pas de cmake/gcc)
#  - torch/torchaudio/torchao/torchtune « +cpu » : index PyTorch CPU (absents de PyPI)
#  - torchao 0.17.0+cpu IMPÉRATIF : torchtune 0.6.0 fait `from torchao.dtypes.nf4tensor import ...`,
#    module supprimé dans torchao 0.18.0. Le gel d'origine avait pris 0.18.0 (neucodec ne borne pas
#    torchao) -> l'import cassait au chargement du moteur (ModuleNotFoundError au préchargement),
#    sans que l'installation ni `pip check` ne signalent quoi que ce soit. Ne pas remonter ce pin
#    sans vérifier que `torchao/dtypes/nf4tensor.py` existe encore dans la roue.
#  --only-binary=llama-cpp-python interdit tout repli sur la source (échec net plutôt que build de 20 min)
#  --require-hashes : chaque artefact installé doit correspondre à une empreinte du gel ; en mode
#    verifie, pip n'accepte un paquet que si son SHA256 est dans requirements.txt, quel que soit
#    l'index qui le sert (pas de confusion d'index / de substitution de paquet).
COPY requirements.txt /tmp/requirements.txt
# Cache BuildKit des roues : un rebuild (correction d'un pin, changement de version) ne retélécharge
# pas les ~1,3 Go de dépendances. `PIP_NO_CACHE_DIR=1` (ENV de l'image) est neutralisé le temps de
# cette commande : le cache vit hors de l'image et ne la gonfle donc pas.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    PIP_NO_CACHE_DIR=0 pip install --require-hashes --only-binary=llama-cpp-python \
        --extra-index-url "$LLAMA_INDEX" \
        --extra-index-url "$TORCH_INDEX" \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# serveur/client éprouvés + voix de référence
COPY ctx/serve.py ctx/client.py /opt/neutts/
COPY ctx/refs/ /opt/neutts/refs/

# Pas de home hors volume : -M + HOME=/data (toute écriture atterrit dans le volume, /data),
# ce qui rend possible le read_only du docker-compose.
RUN useradd -u 10001 -M -d /data -s /usr/sbin/nologin neutts \
    && chown -R neutts:neutts /data /opt/neutts

LABEL org.opencontainers.image.title="neutts-serve" \
      org.opencontainers.image.version="3.3" \
      org.opencontainers.image.description="Moteur TTS Neutts resident (CPU), serve.py v3.3 durci"

USER neutts
WORKDIR /opt/neutts
EXPOSE 8130
# Pas de VOLUME ici : la persistance est portée par le bind ./data du docker-compose.yml.
# Un VOLUME créerait en plus un volume anonyme orphelin à chaque exécution hors compose.
# NB : tout ce que le conteneur écrit va sous /data (HOME, HF_HOME, XDG_CACHE_HOME, TORCH_HOME).

# /health est volontairement sans jeton ; loaded=false tant que le moteur n'est pas chargé.
# start-period généreux : 1er démarrage = téléchargement des modèles HF (~3,5 Go) + chargement.
HEALTHCHECK --interval=30s --timeout=10s --start-period=2400s --retries=3 \
    CMD python -c "import json,os,urllib.request,sys; \
        p=os.environ.get('NEUTTS_PORT','8130'); \
        d=json.load(urllib.request.urlopen('http://127.0.0.1:%s/health'%p,timeout=8)); \
        sys.exit(0 if d.get('loaded') else 1)"

CMD ["python", "/opt/neutts/serve.py"]
