# OntoBricks — container image for Azure Container Apps (or any OCI runtime).
#
# The repo deliberately ships no Dockerfile at its root: OntoBricks is a plain
# ASGI process and the image is the operator's to own. This one lives under
# deploy/azure/ as a working reference for the Container Apps path.
#
# Build it from the REPO ROOT, not this directory:
#   az acr build -r <registry> -t ontobricks:$(git rev-parse --short HEAD) \
#       -f deploy/azure/Dockerfile .
#
# `az acr build` runs the build inside ACR, so no local Docker daemon is needed.

FROM python:3.11-slim-bookworm

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
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

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
# required for every Postgres target. `pitfalls` powers the ontology pitfalls
# detector; drop it if you do not need that feature.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra lakebase --extra pitfalls

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
