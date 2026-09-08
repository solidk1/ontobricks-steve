"""Every documentation path the app points at must exist.

Why this file exists
--------------------
The Neo4j **setup guide** link in the Settings panel pointed at
``documentation/pr47-neo4j-demo/secret-configuration.md`` for months after that
file moved to ``documentation/neo4j-secret-configuration.md``. It was an
external GitHub URL, so nothing in the app ever tried to open it and no test
looked at it — the operator clicking it got a 404 and no signal reached us.

The Help Center is the sharper case: it serves these files at runtime, so a
deleted or renamed doc turns into a broken page rather than a broken link. That
made deleting a documentation directory riskier than it should be.

Both checks are static and cheap, and they make a doc move or deletion fail
here rather than in front of a user.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_DOCS = _ROOT / "documentation"
_FRONT = _ROOT / "src" / "front"
_HELP = _ROOT / "src" / "api" / "routers" / "internal" / "help.py"


def _help_center_docs() -> list[str]:
    """The markdown filenames the Help Center index declares."""
    src = _HELP.read_text()
    block = src[src.index("_DOC_CATEGORIES") : src.index("_DOC_INDEX")]
    return sorted(set(re.findall(r'["\']([A-Za-z0-9._ /-]+\.md)["\']', block)))


def _frontend_doc_refs() -> list[str]:
    """``documentation/…md`` paths named by any template or script."""
    found: set[str] = set()
    for pattern in ("*.html", "*.js"):
        for path in _FRONT.rglob(pattern):
            found |= set(
                re.findall(r"documentation/[A-Za-z0-9._/-]+\.md", path.read_text())
            )
    return sorted(found)


class TestHelpCenterDocsExist:
    def test_the_index_is_not_empty(self):
        """A parsing change that silently matched nothing would make the
        per-file test below vacuously green."""
        assert len(_help_center_docs()) > 10

    @pytest.mark.parametrize("name", _help_center_docs())
    def test_indexed_doc_is_bundled(self, name):
        assert (_DOCS / name).is_file(), (
            f"Help Center indexes {name!r} but documentation/{name} does not "
            "exist — the page would 404 at runtime"
        )


class TestFrontendDocLinksExist:
    @pytest.mark.parametrize("ref", _frontend_doc_refs())
    def test_referenced_doc_exists(self, ref):
        assert (_ROOT / ref).is_file(), (
            f"a template or script links to {ref!r}, which does not exist. "
            "External GitHub URLs are still repo paths — moving or deleting the "
            "file breaks the link silently."
        )

    def test_no_reference_to_the_deleted_demo_folder(self):
        """``pr47-neo4j-demo/`` held 13MB of one PR's proof artefacts and was
        deleted; the changelogs keep its history, the live app must not cite it."""
        offenders = [r for r in _frontend_doc_refs() if "pr47-neo4j-demo" in r]
        assert not offenders, offenders
