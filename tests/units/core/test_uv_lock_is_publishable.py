"""``uv.lock`` must reference public PyPI, not an internal mirror.

Why this file exists
--------------------
I have poisoned this lockfile twice in one release. Both times the cause was the
same and it is environmental, not carelessness about which command to run:
``~/.config/uv/uv.toml`` on a Databricks machine sets

    [[index]]
    url = "https://pypi-proxy.dev.databricks.com/simple/"
    default = true

so **any** ``uv lock`` rewrites every URL to that host. The lockfile still
installs perfectly on the machine that produced it, and `uv sync --frozen`
succeeds locally, which is exactly why it gets committed. It then fails inside the
container on the first wheel the proxy has not cached — the app reports a
successful start and dies ~45s later, which reads as an application bug rather
than a dependency one.

The fix after re-locking is to rewrite the host back (the proxy mirrors PyPI, so
paths and hashes are identical) and verify a sample of hashes against real
downloads. This test is the thing that catches it if I forget.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[3] / "uv.lock"

#: The only host wheels and sdists may come from. A lock that names anything else
#: is either mirror-poisoned or points somewhere a container cannot reach.
_ALLOWED_HOST = "https://files.pythonhosted.org"


def _urls() -> list[str]:
    return re.findall(r'url = "(https?://[^"]+)"', _LOCK.read_text())


class TestNoInternalMirror:
    def test_lock_exists(self):
        assert _LOCK.is_file(), "uv.lock is what the container installs from"

    def test_no_proxy_host_anywhere(self):
        text = _LOCK.read_text()
        assert "pypi-proxy" not in text, (
            "uv.lock references an internal PyPI mirror. Rewrite the host to "
            f"{_ALLOWED_HOST} and re-verify hashes; the container cannot reach "
            "the mirror and will fail on the first uncached wheel."
        )

    def test_every_url_is_public_pypi(self):
        offenders = sorted({u for u in _urls() if not u.startswith(_ALLOWED_HOST)})
        assert not offenders, f"non-PyPI URLs in uv.lock: {offenders[:5]}"

    def test_there_are_urls_to_check(self):
        """Guards against the regex silently matching nothing, which would make
        the assertions above vacuously true."""
        assert len(_urls()) > 500


class TestNoPyTorch:
    """The semantic pitfall checks use an embeddings endpoint now.

    torch entered the graph through ``sentence-transformers`` and took the image
    from 0.50 GB to 6.02 GB. It also made the lock unresolvable: torch 2.14
    depends on a ``cuda-toolkit[cublas]==13.0.3`` that does not exist, for the
    Python >= 3.15 end of ``requires-python``, so ``uv lock`` had no solution at
    all while a ``torch>=2.13.0`` constraint stood.
    """

    @pytest.mark.parametrize(
        "package", ["torch", "sentence-transformers", "transformers", "triton"]
    )
    def test_package_is_absent(self, package):
        assert f'name = "{package}"\n' not in _LOCK.read_text(), (
            f"{package} is back in uv.lock; the semantic pitfall checks should "
            "call ONTOBRICKS_EMBEDDING_MODEL instead of embedding in-process"
        )

    def test_no_dead_torch_constraint_in_pyproject(self):
        pyproject = (_LOCK.parent / "pyproject.toml").read_text()
        assert '"torch>=' not in pyproject, (
            "a torch constraint bounds nothing now and blocks re-resolution"
        )
