# OntoBricks — container image for any OCI runtime.
#
# The repo deliberately ships no Dockerfile at its root: OntoBricks is a plain
# ASGI process and the image is the operator's to own. This one lives under
# deploy/azure/ as a working reference, and is runtime-agnostic: the same image
# runs on AKS (manifests in deploy/azure/k8s/), Azure Container Apps, ECS or a
# plain VM. Nothing below is specific to any of them.
#
# Build it from the REPO ROOT, not this directory:
#   az acr build -r <registry> -t ontobricks:$(git rev-parse --short HEAD) \
#       -f deploy/azure/Dockerfile .
#
# `az acr build` runs the build inside ACR, so no local Docker daemon is needed.

# ── Base images, parameterised for networks that cannot reach Docker Hub ─────
# Azure China cannot: registry-1.docker.io times out from both the ACR build
# agent and ACR's own server-side import (HTTP 424). MCR is mirrored there and
# carries the Docker Hub library images, so a China build imports the identical
# base into its own registry first and points these at it:
#
#   az acr import -n <acr> -t base/python:3.11-slim-bookworm \
#       --source mcr.microsoft.com/mirror/docker/library/python:3.11-slim-bookworm
#   az acr import -n <acr> -t base/uv:0.5.11 --source ghcr.io/astral-sh/uv:0.5.11
#   az acr build ... --build-arg BASE_IMAGE=<acr>.azurecr.cn/base/python:3.11-slim-bookworm \
#                    --build-arg UV_IMAGE=<acr>.azurecr.cn/base/uv:0.5.11
#
# The defaults are the public images, so nothing changes for a global build.
ARG BASE_IMAGE=python:3.11-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.11

# uv arrives via a named stage rather than `COPY --from=${UV_IMAGE}`, because
# ACR's classic builder does not expand an ARG inside a --from flag: it fails with
# "invalid from flag value ${UV_IMAGE}". `FROM ${ARG}` is expanded, so naming the
# stage here and copying from the name works on both builders.
FROM ${UV_IMAGE} AS uvbin

FROM ${BASE_IMAGE}

# ── OS packages ─────────────────────────────────────────────────────────────
#  * ca-certificates — TLS to Postgres, Databricks and the LLM provider. Also
#    supplies the DigiCert/Microsoft roots that PGSSLMODE=verify-full needs for
#    Azure Database for PostgreSQL, so no CA has to be baked in separately.
#  * libpq5          — psycopg[binary] bundles its own libpq, but the runtime
#                      loader still wants the system one present for some builds.
#  * curl            — the container health probe below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# uv, pinned. Copied from the official distroless image rather than curl|sh so
# the build is reproducible and needs no network beyond the registry.
COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /app

# ── Dependencies ────────────────────────────────────────────────────────────
# Copied before the source so a code-only change reuses this layer.
#
# `--frozen` installs uv.lock verbatim and never re-resolves. That is the point:
# the lock must already be pinned to https://files.pythonhosted.org. A lock whose
# URLs point at an internal proxy builds fine here and then fails at runtime on
# the first wheel the proxy has not cached — the app reports a successful start
# and crashes ~45s later. Verify before building:
#   rg -c 'pypi-proxy' uv.lock          -> must be 0
#   rg -o 'url = "https?://[^/"]+' uv.lock | sort -u   -> only pythonhosted
#
# The `lakebase` extra is plain psycopg + psycopg-pool (historical name) and is
# required for every Postgres target, so it is never optional.
#
# `pitfalls` adds only nltk, for the WordNet-based ontology checks. It no longer
# pulls sentence-transformers/PyTorch — the semantic checks call an
# OpenAI-compatible embeddings endpoint (ONTOBRICKS_EMBEDDING_MODEL) instead, so
# this extra costs megabytes rather than the 6.02 GB it once did:
#
#   az acr build ... --build-arg EXTRAS="--extra lakebase --extra pitfalls" ...
#
ARG EXTRAS="--extra lakebase"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev ${EXTRAS}

# NLTK corpora, baked in only when the pitfalls extra is actually installed.
#
# They are DATA, not code: `pip install nltk` does not bring them, and the
# library fetches them from raw.githubusercontent.com on first use. A container
# with restricted egress therefore fails on the first pitfalls analysis, at
# runtime, in front of a user. Baking them makes the image self-contained and
# the feature deterministic.
#
# Guarded on nltk being importable so the default (lakebase-only) build stays
# unaffected and does not need the network here either.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN if /app/.venv/bin/python -c "import nltk" 2>/dev/null; then         /app/.venv/bin/python -m nltk.downloader -d "$NLTK_DATA" wordnet vader_lexicon         && /app/.venv/bin/python -c "import nltk; nltk.data.find('corpora/wordnet'); nltk.data.find('sentiment/vader_lexicon')";     else         echo "nltk not installed (EXTRAS=${EXTRAS}); skipping corpora";     fi

# ── Application ─────────────────────────────────────────────────────────────
COPY run.py ./
COPY src/ ./src/
# documentation/ is NOT optional: the Help Center serves markdown from it at
# runtime, so an image without it renders an empty Help Center.
COPY documentation/ ./documentation/

# Run unprivileged. /tmp is where sessions and logs go when containerized.
RUN useradd --create-home --uid 10001 ontobricks \
    && chown -R ontobricks:ontobricks /app
USER ontobricks

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Binds 0.0.0.0 on $PORT and puts sessions/logs under /tmp. Without it
    # uvicorn binds 127.0.0.1 and nothing outside the container can reach it.
    ONTOBRICKS_CONTAINERIZED=true \
    PORT=8000

EXPOSE 8000

# /health is anonymous by design (bypassed by PermissionMiddleware, CSRF and
# request timing) so a probe reaches it without a session cookie. It always
# returns HTTP 200 — read the JSON `status` field, not the status code, so one
# flickering dependency does not pull the app out of rotation. This probe only
# asks "is the process serving?"; use the JSON for real readiness.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null || exit 1

CMD ["python", "run.py"]
