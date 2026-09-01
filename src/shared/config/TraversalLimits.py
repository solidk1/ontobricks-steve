"""Resource caps for Digital Twin graph traversal.

``api/routers/internal/dtwin.py`` used to pick these four caps with
``3 if is_databricks_app() else 5`` and friends — the Apps container is
memory- and time-constrained, a developer's laptop is not. That made a
platform probe decide a performance question, so the caps could not be
tuned for any other runtime.

The split is preserved exactly (a constrained runtime keeps the tighter
caps) but each value is now individually overridable, so an operator who
sizes their own container can raise them without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_CONSTRAINED = {
    "max_depth": 3,
    "entity_cap": 3_000,
    "batch_size": 250,
    "fetch_timeout_s": 40.0,
}

_ROOMY = {
    "max_depth": 5,
    "entity_cap": 50_000,
    "batch_size": 1_000,
    "fetch_timeout_s": 120.0,
}


def _override(name: str, fallback: float) -> float:
    """Read ``ONTOBRICKS_DTWIN_<NAME>``, falling back on absence or garbage."""
    raw = os.getenv(f"ONTOBRICKS_DTWIN_{name.upper()}")
    if not raw:
        return fallback
    try:
        return float(raw.strip())
    except ValueError:
        return fallback


@dataclass(frozen=True)
class TraversalLimits:
    """Caps applied to a single filter-expand traversal."""

    max_depth: int
    entity_cap: int
    batch_size: int
    fetch_timeout_s: float

    @classmethod
    def resolve(cls) -> TraversalLimits:
        """Build the caps for the current runtime.

        Defaults follow :meth:`RuntimeEnv.is_containerized`; every field
        can be overridden with ``ONTOBRICKS_DTWIN_MAX_DEPTH``,
        ``…_ENTITY_CAP``, ``…_BATCH_SIZE``, ``…_FETCH_TIMEOUT_S``.
        """
        from shared.config.RuntimeEnv import RuntimeEnv

        base = _CONSTRAINED if RuntimeEnv.is_containerized() else _ROOMY
        return cls(
            max_depth=int(_override("max_depth", base["max_depth"])),
            entity_cap=int(_override("entity_cap", base["entity_cap"])),
            batch_size=int(_override("batch_size", base["batch_size"])),
            fetch_timeout_s=_override("fetch_timeout_s", base["fetch_timeout_s"]),
        )
