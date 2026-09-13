---
name: deploy
description: Use when the user asks to deploy, ship, release, or push OntoBricks. OntoBricks is a container-ready ASGI process with no in-repo deploy automation — this skill sequences the pre-flight checks (tests, lockfile) and the post-deploy verification, and tells you what the container needs.
---

# Deploy OntoBricks

**There is no in-repo deploy automation, and no `make deploy` target.** The
Databricks Apps path, the asset bundle (`databricks.yml`, `app.yaml.template`)
and `scripts/deploy*` were removed in v0.7.1. If you find yourself reaching for
`databricks bundle deploy`, stop — that is not how this ships any more.

OntoBricks is a container-ready ASGI process:

```bash
ONTOBRICKS_CONTAINERIZED=true python run.py     # == make prod
```

`ONTOBRICKS_CONTAINERIZED=true` is what makes uvicorn bind `0.0.0.0` on `$PORT`
and put sessions and logs under `/tmp`. Without it the process binds `127.0.0.1`
and nothing outside the container can reach it. Deploying means building an image
around that entrypoint and running it on the user's platform (Azure Container
Apps, ECS, Kubernetes, …). This repo deliberately ships no Dockerfile.

## Pre-flight

1. `git status` — clean tree, or the user has acknowledged uncommitted changes.
2. `uv run --frozen pytest -q -m "not scenario"` — green. **Do not ship on red.**
   **Always keep `--frozen`**: a bare `uv run` re-resolves against the internal
   proxy and rewrites every URL in `uv.lock`, so running the tests is itself
   enough to poison the lock *after* you have already checked it.
3. **`uv.lock` is deploy-safe.** The image installs verbatim from the lock, so
   check the following (all read-only — none of them rewrite the file):
   - **All URLs on the public CDN:** `rg -c 'pypi-proxy\.dev\.databricks\.com' uv.lock`
     must report no matches, and `rg -o 'url = "https?://[^/"]+' uv.lock | sort -u`
     should show only `https://files.pythonhosted.org`. A proxied lock fails to
     download any wheel the proxy has not cached, and the app then crashes
     shortly after reporting a successful start.
   - **In sync with `pyproject.toml`:** `UV_INDEX_URL=https://pypi.org/simple uv lock --check`
     exits 0. Check against the **public** index, not the shell default —
     `UV_INDEX_URL`/`PIP_INDEX_URL` usually point at the proxy, which yields a
     false "lockfile needs to be updated" warning.
   - **Re-check after the test run**, immediately before building. `git status`
     must show `uv.lock` clean at that moment.
   - If the lock is dirty or proxied: `git checkout -- uv.lock`, or re-lock
     against the public index with `UV_INDEX_URL=https://pypi.org/simple uv lock`.
     See `.cursor/09-package-management.mdc`.

## What the image must contain

Beyond the source and `uv.lock`:

- **`documentation/`** — the Help Center serves markdown from it at runtime. An
  image without it renders an empty Help Center.
- **The Azure root CA**, if `PGSSLMODE=verify-full`.
- `psycopg`, via `uv sync --frozen --extra lakebase` (the extra is named
  `lakebase` for historical reasons; it is plain `psycopg` + `psycopg-pool` and
  is required for every Postgres target).

## Required environment

`.env.example` is the full reference. The ones a deploy fails without:

| Variable | Why |
|---|---|
| `PGHOST` / `PGDATABASE` / `PGUSER` | The registry. `ONTOBRICKS_PG_AUTH` picks how the password is produced (`entra` \| `lakebase` \| `password`). |
| `ONTOBRICKS_CONTAINERIZED=true` | Binds `0.0.0.0`; otherwise unreachable. |
| `PORT` | The listen port. |
| `SECRET_KEY` | Session signing. |
| `ONTOBRICKS_OIDC_CLIENT_ID` / `_REDIRECT_URI` | `ONTOBRICKS_AUTH_ENABLED` defaults to **true** (fail closed), so without these nobody can log in. |
| `ONTOBRICKS_BOOTSTRAP_ADMIN` | Seeds the first admin. Without it a fresh deployment has nobody able to grant access. |
| `ONTOBRICKS_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | AI features. Nothing is inferred — a configured Databricks workspace is not an LLM provider. |

**Single replica.** APScheduler runs in-process and sessions are on local disk.
More than one instance duplicates every scheduled build; scaling to zero stops
the scheduler entirely. On Azure Container Apps that means
`minReplicas: 1, maxReplicas: 1`; on Kubernetes, `replicas: 1` **and**
`strategy: Recreate` (a `RollingUpdate` briefly runs two pods). The defaults of
both violate this. `deploy/azure/k8s/base/` (plus `overlays/` per cloud) holds AKS manifests that encode it, guarded
by `tests/units/deploy/test_aks_manifests.py`.

**Lockfile, before any build.** `uv.lock` must reference
`files.pythonhosted.org` only. A Databricks machine's `~/.config/uv/uv.toml` sets
an internal mirror as the default index, so *any* `uv lock` rewrites every URL;
`uv sync --frozen` then succeeds locally and the container dies ~45s after a
"successful" start on the first uncached wheel. Rewrite the host back and verify
hashes against real downloads. `tests/units/core/test_uv_lock_is_publishable.py`
fails if a poisoned lock is committed.

## Post-deploy

1. **`GET /health`** — the single readiness endpoint. It is anonymous (in the
   bypass list of `PermissionMiddleware`, `CSRFMiddleware` and
   `RequestTimingMiddleware`), so a load balancer or k8s probe can call it
   without a session cookie. There is **no** `/healthz` and no
   `/health/detailed`; the latter was folded into `/health`.
2. Walk the probes it returns. Each is `{name, label, status, detail,
   duration_ms}` and the top-level `status` is the worst severity. Expect
   `postgres`, `postgres.permissions`, `graphdb.postgres`, `registry.cfg`,
   `registry.volume_read`, `registry.volume_write`, `registry.uc_schema_ddl`,
   `runtime`, `filesystem.*`, plus `databricks.auth` / `databricks.warehouse` /
   `databricks.cloudfetch` when the connector is configured.
3. **Re-check about a minute later.** A dependency-download failure surfaces as a
   crash well after the platform reports a healthy start, so one immediate 200
   does not prove the deploy is good.
4. If the registry is empty, tell the user to open **Settings → Registry →
   Initialize** in the UI.
5. Confirm login works and that `ONTOBRICKS_BOOTSTRAP_ADMIN` was granted
   (**Settings → App access**).

## Release flow

When the user says "release vX.Y.Z" rather than "deploy":

1. `uv run --frozen pytest -q -m "not scenario"`
2. Bump `version` in `pyproject.toml`
3. Commit, tag `vX.Y.Z`, push with `--tags`
4. Build and roll out the image
5. Update the changelog (`changelog` skill) and write the release notes per
   `.cursorrules` §"When asking to create a Release Notes"

## Don't

- Don't suggest `make deploy`, `databricks bundle deploy`, `app.yaml`, or
  `bootstrap-perms`. None of them exist.
- Don't ship on red tests.
- Don't run a bare `uv lock` / `uv sync` / `uv run` without `--frozen` in the dev
  shell — it rewrites every `uv.lock` URL to the internal proxy and breaks the
  next build. Only re-lock via `UV_INDEX_URL=https://pypi.org/simple uv lock`
  after a real dependency change.
- Don't commit a `uv.lock` containing `pypi-proxy.dev.databricks.com` URLs.
- Don't claim the deploy is healthy on a single immediate `/health` 200.
