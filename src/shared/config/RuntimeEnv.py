"""Runtime environment predicates.

``DatabricksAuth.is_databricks_app()`` — a single probe of the
Apps-injected ``DATABRICKS_APP_PORT`` — used to answer seven unrelated
questions across 54 call sites:

1. what port to listen and self-call on;
2. whether the filesystem is ephemeral (session dir, log dir);
3. whether cookies must be TLS-only;
4. whether authentication and RBAC are enforced;
5. whether Databricks credentials resolve implicitly;
6. whether a setting is externally injected and so read-only in the UI;
7. what resource caps to apply to graph traversal.

This class owns 1–4. Question 5 belongs to
:class:`back.core.databricks.DatabricksConnector`, 6 to
:class:`shared.config.settings.Settings`, and 7 is plain configuration.

Conflating 3 with 4 was the dangerous case: cookie security and access
control are independent, and a deployment outside Databricks Apps
silently got neither.

**Transitional behaviour.** Every predicate here falls back to
:func:`_legacy_apps_mode` when its own variable is unset, so introducing
this class changes no behaviour. :func:`_legacy_apps_mode` is the *only*
reader of ``DATABRICKS_APP_PORT`` in the codebase — a test in
``tests/units/core/test_runtime_env.py`` enforces that — which is what
lets the Apps deploy be removed by editing one function.
"""

from __future__ import annotations

import os

DEFAULT_PORT = 8000

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


def _legacy_apps_mode() -> bool:
    """Return *True* when running inside Databricks Apps.

    The sole reader of ``DATABRICKS_APP_PORT``. Every predicate below
    defers to it when its own variable is unset, which is what makes the
    split behaviour-preserving. Delete this in P7 along with the Apps
    deploy, and each caller's default becomes its documented value.
    """
    return os.getenv("DATABRICKS_APP_PORT") is not None


def _env_flag(name: str, default: bool) -> bool:
    """Read *name* as a boolean, returning *default* when unset.

    Unrecognised values are treated as *default* rather than guessed at,
    so a typo cannot silently flip a security-relevant flag.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return default


class RuntimeEnv:
    """Where the process runs and what that implies.

    Every method reads the environment on each call rather than caching:
    the deployed process never changes these mid-flight, and tests
    (and ``scripts/start.sh``) set them after import.
    """

    # ------------------------------------------------------------------
    # 1. Port
    # ------------------------------------------------------------------

    @staticmethod
    def port(default: int = DEFAULT_PORT) -> int:
        """Port to bind, and to reach this process on for self-calls.

        ``PORT`` is the portable spelling and wins. ``DATABRICKS_APP_PORT``
        is honoured while the Apps deploy still exists.
        """
        for name in ("PORT", "DATABRICKS_APP_PORT"):
            raw = os.getenv(name)
            if not raw:
                continue
            try:
                return int(raw.strip())
            except ValueError:
                continue
        return default

    @staticmethod
    def self_base_url() -> str:
        """Base URL for HTTP calls this process makes back to itself."""
        return f"http://localhost:{RuntimeEnv.port()}"

    # ------------------------------------------------------------------
    # 2. Filesystem
    # ------------------------------------------------------------------

    @staticmethod
    def is_containerized() -> bool:
        """Whether the writable filesystem is ephemeral and restricted.

        Drives session- and log-directory placement (``/tmp`` rather than
        the working directory). Set ``ONTOBRICKS_CONTAINERIZED`` in the
        image.
        """
        return _env_flag("ONTOBRICKS_CONTAINERIZED", _legacy_apps_mode())

    # ------------------------------------------------------------------
    # 3. Cookie security
    # ------------------------------------------------------------------

    @staticmethod
    def secure_cookies() -> bool:
        """Whether to set ``Secure`` on cookies.

        True when TLS terminates in front of this process. Deliberately
        independent of :meth:`auth_enabled`: a TLS-terminating proxy and
        access control are separate facts, and a deployment can have
        either without the other.
        """
        return _env_flag("ONTOBRICKS_SECURE_COOKIES", _legacy_apps_mode())

    # ------------------------------------------------------------------
    # 4. Authentication
    # ------------------------------------------------------------------

    @staticmethod
    def auth_enabled() -> bool:
        """Whether identity and RBAC are enforced. **Defaults to on.**

        When false, ``PermissionMiddleware`` passes every request through as
        admin. That is right for local development and catastrophic in a
        deployment, so the default fails *closed*: a deployment that forgets to
        configure anything is locked rather than wide open. Set
        ``ONTOBRICKS_AUTH_ENABLED=false`` explicitly for local development.

        This became the default once OIDC login existed for a locked-down
        deployment to satisfy (see ``api/routers/internal/auth.py``).
        """
        return _env_flag("ONTOBRICKS_AUTH_ENABLED", True)


def default_session_dir() -> str:
    """Session store path appropriate to the runtime filesystem."""
    return (
        "/tmp/ontobricks_session"
        if RuntimeEnv.is_containerized()
        else "./fastapi_session"
    )


def default_log_dir(explicit: str | None = None) -> str | None:
    """Preferred log directory, or *None* to let the caller decide.

    Returns *None* off-container so :class:`LogManager` keeps its
    working-directory default and its own fallback chain.
    """
    if explicit:
        return explicit
    if os.getenv("LOG_DIR"):
        return os.getenv("LOG_DIR")
    return "/tmp/logs" if RuntimeEnv.is_containerized() else None
