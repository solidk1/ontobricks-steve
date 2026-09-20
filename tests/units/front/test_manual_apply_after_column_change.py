"""Assigning a column must enable Manual Apply.

``MappingManual.setupSaveButton`` wires the Apply button to ``input`` events on the
panel's SQL text fields only. Assigning an ID or Label column is a dropdown click,
not a text edit, so Apply stayed disabled: users had to touch the SQL to enable it,
and applying without that saved empty id/label values. Entity TriplesMaps were then
omitted from R2RML exports and relationships rendered ``{None}`` templates
(upstream GitHub #158 / #159).

Reimplemented from upstream 0754053a rather than applied. That commit hooks
``claimMappingPanel``, part of a Mapping Designer redesign this fork does not
carry; the assignment handler is the same intent and a closer cause.

Static checks, because the frontend has no JS test runner — the same approach as
the sibling ``test_manual_mapping_column_state.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DESIGN_JS = (
    Path(__file__).resolve().parents[3]
    / "src/front/static/mapping/js/mapping-design.js"
)
_MANUAL_JS = (
    Path(__file__).resolve().parents[3]
    / "src/front/static/mapping/js/mapping-manual.js"
)


def _source() -> str:
    return _DESIGN_JS.read_text()


class TestTheHelperExists:
    def test_it_is_defined(self):
        assert "function enableManualApplyAfterColumnChange()" in _source()

    def test_it_enables_the_manual_apply_button(self):
        src = _source()
        body = src[src.index("function enableManualApplyAfterColumnChange()") :]
        body = body[: body.index("\n}")]
        assert "manualSavePanelBtn" in body
        assert "disabled = false" in body

    def test_it_respects_the_read_only_version_guard(self):
        """A published version must stay read-only; the same guard
        ``setupSaveButton`` uses."""
        src = _source()
        body = src[src.index("function enableManualApplyAfterColumnChange()") :]
        body = body[: body.index("\n}")]
        assert "isActiveVersion" in body

    def test_it_no_ops_when_the_panel_is_closed(self):
        """Called from the designer, which is reachable with the manual panel
        absent; a null dereference there would break the dropdown."""
        src = _source()
        body = src[src.index("function enableManualApplyAfterColumnChange()") :]
        body = body[: body.index("\n}")]
        assert re.search(r"if\s*\(!saveBtn\)\s*return", body)


class TestItIsCalledOnAssignment:
    def _assignment_handler(self) -> str:
        src = _source()
        start = src.index("if (action === 'id') EntityPanelState.idColumn = column;")
        return src[start : start + 600]

    def test_the_handler_calls_it(self):
        assert "enableManualApplyAfterColumnChange()" in self._assignment_handler()

    def test_it_runs_after_the_state_is_written(self):
        """Enabling before the assignment lands would arm Apply for a no-op."""
        handler = self._assignment_handler()
        assert handler.index("EntityPanelState.attributeMappings[item.dataset.attr]") < handler.index(
            "enableManualApplyAfterColumnChange()"
        )


class TestWhyThisWasNeeded:
    def test_sql_input_listeners_alone_cannot_cover_assignments(self):
        """Records the gap: setupSaveButton binds only to text inputs, so a
        dropdown assignment could never reach it."""
        manual = _MANUAL_JS.read_text()
        setup = manual[manual.index("setupSaveButton: function()") :]
        setup = setup[: setup.index("\n    },")]
        assert "addEventListener('input'" in setup
        assert "EntityPanelState" not in setup, (
            "if setupSaveButton learns about column state, revisit this test"
        )
