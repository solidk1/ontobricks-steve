"""Constants for Unity Catalog REST / Files API calls.

The Files API paths live in :mod:`back.core.databricks.constants` alongside the
``API_PREFIX`` they are built from. This module used to re-derive them from that
same prefix, so the identical two lines existed twice in one package. It now
re-exports, keeping the ``back.core.databricks.uc`` import path that
``VolumeFileService`` and ``uc/__init__`` rely on.
"""

from back.core.databricks.constants import (
    API_PREFIX,
    FS_DIRS_PATH,
    FS_FILES_PATH,
    _REQUEST_TIMEOUT,
)

__all__ = ["API_PREFIX", "FS_FILES_PATH", "FS_DIRS_PATH", "_REQUEST_TIMEOUT"]
